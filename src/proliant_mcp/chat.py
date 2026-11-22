"""Shared machinery for tools that talk to the user with pop-ups (MCP elicitation).

A tool's work (a sign-in, adding a server, an import) runs as a background task that posts
questions. ``run_flow`` shows them: as live pop-ups on 2025-11-25-era connections, or as
``InputRequiredResult`` rounds (the client re-calls the tool with the answers) on
2026-07-28 connections.

Passwords are never collected in a chat form: the MCP spec forbids it and forms cannot mask
input. ``Prompter.secret`` serves a one-time page on 127.0.0.1 with a masked field and opens
it with URL-mode elicitation (or links it from a form on clients without URL mode). The
password goes from the browser straight to this server, never through the AI client or model.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import mcp_types as types
from fastmcp import Context
from fastmcp.exceptions import ToolError

ASK_TIMEOUT = 300.0
FLOW_TTL = 900.0


class FlowError(RuntimeError):
    """A problem to report to the user (cancelled, timed out, bad input)."""


class PopupCancelled(FlowError):
    """A pop-up was declined or cancelled, often automatically by the client (e.g. VS Code Autopilot)."""


def popup_failed(exc: BaseException, unsupported: str) -> bool:
    """True when *exc* ended a flow because pop-ups are unsupported or were cancelled."""
    if str(exc) == unsupported:
        return True
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, PopupCancelled):
            return True
        seen = seen.__cause__
    return False


@dataclass
class _Question:
    params: types.ElicitRequestFormParams | types.ElicitRequestURLParams
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    answer: asyncio.Future[Any] = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    withdrawn: asyncio.Event = field(default_factory=asyncio.Event)

    def withdraw(self) -> None:
        self.withdrawn.set()
        if not self.answer.done():
            self.answer.cancel()


def form_params(message: str, properties: dict[str, Any], required: list[str]) -> types.ElicitRequestFormParams:
    return types.ElicitRequestFormParams(
        message=message, requested_schema={"type": "object", "properties": properties, "required": required}
    )


@dataclass(frozen=True)
class SecretPrompt:
    """What the secure password page and its pop-up say."""

    message: str  # what to type and for whom, in a sentence
    title: str = "Enter your password"
    who: str = ""
    label: str = "Password"
    button: str = "Continue"
    note: str = "The password is used once and is not shared with the AI."
    input_type: str = "password"  # "email" for the sign-in email page
    # Extra fields shown above the password: (name, label, kind, default, required); kind is
    # "text" or "select:value=Label|value=Label". Submitted values land in _PasswordPage.values.
    fields: tuple[tuple[str, str, str, str, bool], ...] = ()


def _page(title: str, body: str, refresh: bool = False) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        + ("<meta http-equiv='refresh' content='2'>" if refresh else "")
        + 
        f"<title>{html.escape(title)}</title><style>"
        ".choice{display:flex;align-items:center;gap:.6rem;border:1px solid #ccd;border-radius:6px;padding:.6rem .8rem;margin:.4rem 0;cursor:pointer}"
        ".choice input{width:auto;margin:0}.num{font-size:2.2rem;letter-spacing:.1rem}"
        "body{font-family:system-ui,sans-serif;background:#f4f5f6;margin:0;display:flex;justify-content:center}"
        "main{background:#fff;margin-top:12vh;padding:2rem 2.5rem;border-radius:10px;box-shadow:0 2px 12px #0002;width:22rem}"
        "h1{font-size:1.25rem;margin:0 0 1rem}.who{color:#425563}.err{color:#c8102e}"
        "input,select{width:100%;box-sizing:border-box;padding:.6rem;font-size:1rem;margin:.4rem 0 1rem}"
        "button{background:#01a982;color:#fff;border:0;border-radius:6px;padding:.6rem 1.4rem;font-size:1rem;cursor:pointer}"
        ".pw{position:relative}.pw input{padding-right:2.8rem}input::-ms-reveal{display:none}"
        ".pw button{position:absolute;right:.25rem;top:.65rem;background:none;color:#425563;padding:.3rem .45rem;line-height:0}"
        f"</style></head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"
    )


def _field_html(name: str, label: str, kind: str, default: str, required: bool) -> str:
    n, req = html.escape(name), " required" if required else ""
    out = f"<label for='{n}'>{html.escape(label)}</label>"
    if kind.startswith("select:"):
        options = "".join(
            f"<option value='{html.escape(v)}'{' selected' if v == default else ''}>{html.escape(t)}</option>"
            for v, _, t in (o.partition("=") for o in kind[7:].split("|"))
        )
        return out + f"<select id='{n}' name='{n}'{req}>{options}</select>"
    return out + f"<input id='{n}' name='{n}' type='text' value='{html.escape(default)}'{req}>"


_SVG = "<svg width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' aria-hidden='true'"
_EYE = (
    f"{_SVG} id='eye'><path d='M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z'/><circle cx='12' cy='12' r='3'/></svg>"
    f"{_SVG} id='eye-off' style='display:none'><path d='M1 12s4-7 11-7c2.2 0 4.1.7 5.7 1.7M23 12s-4 7-11 7c-2.2 0-4.1-.7-5.7-1.7'/>"
    "<path d='M9.9 9.9a3 3 0 0 0 4.2 4.2'/><path d='M2 2l20 20'/></svg>"
)

# Show/hide toggle for the password field. The page allows no other script (see the CSP in _PasswordPage).
_REVEAL_JS = (
    "var b=document.getElementById('reveal'),i=document.getElementById('password'),"
    "e=document.getElementById('eye'),o=document.getElementById('eye-off');"
    "b.onclick=function(){var s=i.type==='password';i.type=s?'text':'password';"
    "e.style.display=s?'none':'';o.style.display=s?'':'none';"
    "b.setAttribute('aria-label',s?'Hide password':'Show password');b.title=b.getAttribute('aria-label');"
    "b.setAttribute('aria-pressed',s?'true':'false');i.focus();};"
)


def _password_form(prompt: SecretPrompt, error: str | None, nonce: str = "", step: str = "") -> str:
    err = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    extra = "".join(_field_html(*f) for f in prompt.fields)
    is_password = prompt.input_type == "password"
    field = (
        f"<input id='password' name='password' type='{html.escape(prompt.input_type)}' "
        f"autocomplete='{'current-password' if is_password else 'username'}' autofocus required>"
    )
    if is_password and nonce:
        field = (
            f"<div class='pw'>{field}<button type='button' id='reveal' aria-label='Show password' "
            f"title='Show password' aria-pressed='false'>{_EYE}</button></div>"
            f"<script nonce='{nonce}'>{_REVEAL_JS}</script>"
        )
    return _page(
        prompt.title,
        f"<p class='who'>{html.escape(prompt.who)}</p>{err}"
        f"<form method='post' autocomplete='off'>{extra}"
        + (f"<input type='hidden' name='step' value='{html.escape(step)}'>" if step else "")
        + f"<label for='password'>{html.escape(prompt.label)}</label>{field}"
        f"<button type='submit'>{html.escape(prompt.button)}</button></form>"
        f"<p><small>{html.escape(prompt.note)}</small></p>",
    )


_USED = _page("Already used", "<p>This page was already used. You can close this tab.</p>")


class _PasswordPage:
    """One-time local page with a masked field. Bound to 127.0.0.1, random port and path,
    accepts one POST, and never logs the request body."""

    def __init__(self, prompt: SecretPrompt, error: str | None) -> None:
        loop = asyncio.get_running_loop()
        self.token = secrets.token_urlsafe(24)
        self.password: asyncio.Future[str] = loop.create_future()
        self.values: dict[str, str] = {}
        page = self

        def deliver(value: str) -> None:
            if not page.password.done():
                page.password.set_result(value)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: str, nonce: str = "") -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                script = f"; script-src 'nonce-{nonce}'" if nonce else ""
                self.send_header("Content-Security-Policy", f"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'{script}")
                self.end_headers()
                self.wfile.write(data)

            def _form(self, error_text: str | None) -> None:
                nonce = secrets.token_urlsafe(16) if prompt.input_type == "password" else ""
                self._send(200, _password_form(prompt, error_text, nonce), nonce)

            def _ours(self) -> bool:
                return urllib.parse.urlsplit(self.path).path == f"/{page.token}"

            def do_GET(self) -> None:
                if not self._ours():
                    return self._send(404, "Not found")
                if page.password.done():
                    return self._send(410, _USED)
                self._form(error)

            def do_POST(self) -> None:
                if not self._ours() or page.password.done():
                    return self._send(410, _USED)
                length = min(int(self.headers.get("Content-Length") or 0), 8192)
                fields = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
                value = fields.get("password", [""])[0]
                values = {f[0]: fields.get(f[0], [""])[0].strip() for f in prompt.fields}
                missing = [f[1] for f in prompt.fields if f[4] and not values[f[0]]]
                if missing or not value:
                    return self._form(f"Enter {', '.join(missing) or prompt.label}.")
                page.values = values
                loop.call_soon_threadsafe(deliver, value)
                self._send(200, _page(f"{prompt.label} received", "<p>Received. Go back to your AI chat; you can close this tab.</p>"))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/{self.token}"
        threading.Thread(target=self._server.serve_forever, name="proliant-mcp-password-page", daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _csp(body: str, nonce: str) -> str:
    script = f"; script-src 'nonce-{nonce}'" if f"nonce='{nonce}'" in body else ""
    return f"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'{script}"


def _send_html(handler: BaseHTTPRequestHandler, status: int, body: str, nonce: str = "", location: str = "") -> None:
    data = body.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Content-Security-Policy", _csp(body, nonce))
    if location:
        handler.send_header("Location", location)
    handler.end_headers()
    handler.wfile.write(data)


def open_in_browser(url: str) -> bool:
    """Best effort: open *url* in the user's default browser. Never starts a console browser,
    which would write to this server's stdio. Set PROLIANT_MCP_NO_BROWSER=1 to turn it off."""
    if os.environ.get("PROLIANT_MCP_NO_BROWSER"):
        return False
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url], **quiet)
        elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            subprocess.Popen(["xdg-open", url], **quiet)
        else:
            return False
    except OSError:
        return False
    return True


class FlowPage:
    """One local page that walks the user through a whole flow without pop-ups (email, Okta
    Verify number, password, workspace...). The same link stays valid from the first step to the
    last: waiting steps refresh themselves, and forms post back to it. Bound to 127.0.0.1 with a
    random port and path; request bodies are never logged."""

    def __init__(self, title: str, max_body_bytes: int = 8192) -> None:
        self._loop = asyncio.get_running_loop()
        self.token = secrets.token_urlsafe(24)
        self.title = title
        self._render: Callable[[str], str] = lambda nonce: _page(title, "<p>Starting...</p>", refresh=True)
        self._step = ""
        self._answer: asyncio.Future[dict[str, str]] | None = None
        self._fields: tuple[str, ...] = ()
        self._required: dict[str, str] = {}  # field -> label, checked here too (not only by the browser)
        self._with_error: Callable[[str], Callable[[str], str]] | None = None
        page = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _ours(self) -> bool:
                return urllib.parse.urlsplit(self.path).path == f"/{page.token}"

            def do_GET(self) -> None:
                if not self._ours():
                    return _send_html(self, 404, "Not found")
                nonce = secrets.token_urlsafe(16)
                _send_html(self, 200, page._render(nonce), nonce)

            def do_POST(self) -> None:
                if not self._ours():
                    return _send_html(self, 404, "Not found")
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return _send_html(self, 400, "Invalid request length")
                if length < 0 or length > max_body_bytes:
                    return _send_html(self, 413, "Request too large")
                form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
                values = {k: v[0] for k, v in form.items()}
                step, answer = page._step, page._answer
                missing = [label for f, label in page._required.items() if not (values.get(f) if f == "password" else values.get(f, "").strip())]
                if answer is not None and values.get("step", step) == step and missing and page._with_error:
                    page._render = page._with_error(f"Enter {', '.join(missing)}.")
                elif answer is not None and values.get("step", step) == step:
                    page._render = lambda nonce: _page(page.title, "<p>Checking...</p>", refresh=True)
                    page._loop.call_soon_threadsafe(page._deliver, answer, {f: values.get(f, "") for f in page._fields})
                _send_html(self, 303, "", location=f"/{page.token}")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/{self.token}"
        threading.Thread(target=self._server.serve_forever, name="proliant-mcp-flow-page", daemon=True).start()
        self.opened = open_in_browser(self.url)

    def link(self) -> str:
        """Start of a next_step: make sure the assistant shows the link (the user cannot see tool results)."""
        opened = "It was opened in the user's browser; show the link anyway in case it did not. " if self.opened else ""
        port = self._server.server_address[1]
        remote = (
            "If the user cannot open it because this MCP server runs on another computer (a remote gateway: 127.0.0.1 is that "
            f"computer, not theirs), do not keep waiting: they can forward the port with `ssh -L {port}:127.0.0.1:{port} "
            "<user>@<server-host>` and open the same link on their own computer. Never ask for a password in the chat. "
        )
        return f"Show the user this link in your reply, as a clickable link, before calling any tool: {self.url} . {opened}{remote}"

    @staticmethod
    def _deliver(answer: asyncio.Future[dict[str, str]], values: dict[str, str]) -> None:
        if not answer.done():
            answer.set_result(values)

    def expect(self, step: str, render: Callable[[str], str], fields: tuple[str, ...]) -> asyncio.Future[dict[str, str]]:
        """Show a form; the returned future gets its submitted *fields*."""
        if self._answer is not None and not self._answer.done():
            self._answer.cancel()
        self._step, self._fields = step, fields
        self._answer = self._loop.create_future()
        self._render = render
        self._required, self._with_error = {}, None
        return self._answer

    def ask_secret(self, step: str, prompt: SecretPrompt, error: str | None) -> asyncio.Future[dict[str, str]]:
        """A one-field form (password with the show/hide eye, or email) plus *prompt.fields*."""
        names = ("password", *(f[0] for f in prompt.fields))

        def form(err: str | None) -> Callable[[str], str]:
            return lambda nonce: _password_form(prompt, err, nonce if prompt.input_type == "password" else "", step)

        answer = self.expect(step, form(error), names)
        self._required = {f[0]: f[1] for f in prompt.fields if f[4]} | {"password": prompt.label}
        self._with_error = form
        return answer

    def ask_choice(self, step: str, question: str, options: list[str]) -> asyncio.Future[dict[str, str]]:
        """Radio buttons to click, then Continue."""
        def render(nonce: str) -> str:
            rows = "".join(
                f"<label class='choice'><input type='radio' name='choice' value='{html.escape(o)}' required"
                f"{' checked' if i == 0 else ''}>{html.escape(o)}</label>"
                for i, o in enumerate(options)
            )
            return _page(self.title, f"<p>{html.escape(question)}</p><form method='post'>"
                         f"<input type='hidden' name='step' value='{html.escape(step)}'>{rows}"
                         "<button type='submit'>Continue</button></form>")
        return self.expect(step, render, ("choice",))

    def show(self, body_html: str, refresh: bool = True) -> None:
        """A step with nothing to fill in (e.g. "tap 21 in Okta Verify"); refreshes itself while *refresh*."""
        if self._answer is not None and not self._answer.done():
            self._answer.cancel()
        self._answer = None
        self._render = lambda nonce: _page(self.title, body_html, refresh=refresh)

    def finish(self, body_html: str, close_after: float = 120.0) -> None:
        """Final message; the page stays up a little so the user sees it, then closes."""
        self.show(body_html, refresh=False)
        self._loop.call_later(close_after, self.close)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._server.shutdown()
            self._server.server_close()


class _Flow:
    """One interaction in progress: the background task and the questions it is waiting on."""

    def __init__(self, url_mode: bool) -> None:
        self.id = secrets.token_urlsafe(16)
        self.url_mode = url_mode
        self.started = time.monotonic()
        self.questions: asyncio.Queue[_Question] = asyncio.Queue()
        self.pending: dict[str, _Question] = {}
        self.notes: list[str] = []
        self.on_url_done: Callable[[str], Awaitable[None]] | None = None
        self.task: asyncio.Task[dict[str, Any]] | None = None

    async def ask(self, params: types.ElicitRequestFormParams | types.ElicitRequestURLParams) -> _Question:
        question = _Question(params)
        self.pending[question.id] = question
        await self.questions.put(question)
        return question

    def answer(self, question_id: str, result: Any) -> None:
        question = self.pending.pop(question_id, None)
        if question and not question.answer.done():
            question.answer.set_result(result)

    async def next_question(self) -> _Question | None:
        """The next question still waiting for an answer, or None once the task has ended."""
        assert self.task is not None
        while True:
            getter = asyncio.ensure_future(self.questions.get())
            done, _ = await asyncio.wait({getter, self.task}, return_when=asyncio.FIRST_COMPLETED)
            if getter not in done:
                getter.cancel()
                return None
            question = getter.result()
            if not question.withdrawn.is_set():
                return question


def accepted(result: Any) -> dict[str, Any]:
    if getattr(result, "action", None) != "accept":
        raise PopupCancelled("Cancelled")
    return dict(getattr(result, "content", None) or {})


class Prompter:
    """Ask the user things from inside a flow."""

    def __init__(self, flow: _Flow) -> None:
        self.flow = flow

    def say(self, message: str) -> None:
        self.flow.notes.append(message)

    async def _wait(self, awaitable: Awaitable[Any], timeout: float, what: str) -> Any:
        try:
            return await asyncio.wait_for(awaitable, timeout)
        except TimeoutError as exc:
            raise FlowError(f"No {what} within {int(timeout // 60)} minutes") from exc

    async def form(self, message: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        question = await self.flow.ask(form_params(message, properties, required))
        return accepted(await self._wait(question.answer, ASK_TIMEOUT, "answer"))

    async def answer_or(self, question: _Question, work: asyncio.Future[Any], timeout: float) -> tuple[bool, Any]:
        """Wait for the person's answer or for *work* to finish, whichever is first.
        Returns (True, work result) or (False, the answer)."""
        done, _ = await asyncio.wait({question.answer, work}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            question.withdraw()
            return True, work.result()
        if question.answer in done:
            return False, question.answer.result()
        question.withdraw()
        raise FlowError(f"No answer within {int(timeout // 60)} minutes")

    async def secret(self, prompt: SecretPrompt, error: str | None = None) -> str:
        """A password typed on the one-time local page (never through the chat)."""
        page = _PasswordPage(prompt, error)
        try:
            prefix = f"{error} " if error else ""
            if self.flow.url_mode:
                params: types.ElicitRequestFormParams | types.ElicitRequestURLParams = types.ElicitRequestURLParams(
                    message=f"{prefix}{prompt.message} The page is served by the proliant-mcp server on this computer; "
                    "the password is not shared with the AI.",
                    url=page.url,
                    elicitation_id=page.token,
                )
            else:
                params = form_params(
                    f"{prefix}Open {page.url} in your browser. {prompt.message} (A page served by the proliant-mcp server "
                    "on this computer; the password is not shared with the AI.) Then submit this form.",
                    {"done": {"type": "boolean", "title": "I typed the password on that page", "default": True}},
                    ["done"],
                )
            question = await self.flow.ask(params)
            submitted, value = await self.answer_or(question, page.password, ASK_TIMEOUT)
            if not submitted:
                accepted(value)
                value = await self._wait(asyncio.shield(page.password), ASK_TIMEOUT, "password")
            if self.flow.url_mode and self.flow.on_url_done:
                with contextlib.suppress(Exception):
                    await self.flow.on_url_done(page.token)
            return value
        finally:
            page.close()


_flows: dict[str, _Flow] = {}


# ── Flows without pop-ups (com_login_start, add_server_start) ─────────────────
#
# For clients that cannot show pop-ups (Claude Desktop, some Codex versions, harnesses that
# auto-cancel them). The work runs in a background task that publishes each step for the user
# (a number to tap, a password page link, a choice); every tool call returns at once with the
# current step for the assistant to relay, within the 60 s default tool timeout of clients like Codex.


class Relay:
    """State of one flow without pop-ups, read by a pair of start/status tools."""

    def __init__(self, failed: str, retry: str) -> None:
        self.failed = failed  # prefix of the error when the task fails, e.g. "GreenLake sign-in failed: "
        self.retry = retry  # next step after a failure, e.g. "Call com_login_start to try again."
        self.state: dict[str, Any] = {"status": "starting"}
        self.version = 0
        self.seen = 0
        self.changed = asyncio.Event()
        self.notes: list[str] = []
        self.choice: asyncio.Future[str] | None = None
        self.task: asyncio.Task[dict[str, Any]] | None = None
        self.label = ""  # what the flow is about, e.g. the host being added
        self.reported = False  # the assistant has seen how it ended

    def publish(self, status: str, next_step: str, **fields: Any) -> None:
        self.state = {"status": status, **fields, "next_step": next_step}
        self.version += 1
        self.changed.set()

    def current(self) -> dict[str, Any]:
        assert self.task is not None
        if self.task.done():
            self.reported = True
            if self.task.cancelled():
                return {"status": "failed", "error": "Cancelled"}
            exc = self.task.exception()
            if exc is not None:
                return {"status": "failed", "error": f"{self.failed}{exc}", "next_step": self.retry}
            return self.task.result()
        self.seen = self.version
        return {**self.state, "notes": list(self.notes)} if self.notes else dict(self.state)

    async def wait_for_news(self, timeout: float) -> None:
        """Until a step the caller has not seen yet is published, the task ends, or *timeout*."""
        assert self.task is not None
        deadline = asyncio.get_running_loop().time() + timeout
        while self.version == self.seen and not self.task.done():
            self.changed.clear()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            waiter = asyncio.ensure_future(self.changed.wait())
            await asyncio.wait({waiter, self.task}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()

    async def status(self, wait: float, call_again: str) -> dict[str, Any]:
        """What a status tool returns: the next step, the result, or that nothing changed yet."""
        assert self.task is not None
        if not self.task.done():
            await self.wait_for_news(wait)
        news = self.version != self.seen
        result = self.current()
        if not self.task.done() and not news:
            result["waiting"] = True
            result["next_step"] = f"Still waiting for the user. {result.get('next_step', '')} {call_again}"
        return result

    async def secret(self, prompt: SecretPrompt, error: str | None, step: str, timeout: float = ASK_TIMEOUT) -> str:
        """Publish a one-time password page as an ``enter_password`` step; *step* may use {url}."""
        page = _PasswordPage(prompt, error)
        try:
            self.publish("enter_password", f"{error + ' ' if error else ''}{step.replace('{url}', page.url)}", url=page.url)
            return await asyncio.wait_for(page.password, timeout)
        except TimeoutError as exc:
            raise FlowError(f"No password entered within {int(timeout // 60)} minutes") from exc
        finally:
            page.close()


def _elicitation_modes(ctx: Context) -> tuple[bool, bool]:
    params = ctx.session.client_params
    cap = params.capabilities.elicitation if params is not None else None
    if cap is None:
        return False, False
    url = cap.url is not None
    return cap.form is not None or not url, url


def _start(url_mode: bool, runner: Callable[[_Flow], Awaitable[dict[str, Any]]]) -> _Flow:
    now = time.monotonic()
    for key, old in list(_flows.items()):
        if now - old.started > FLOW_TTL:
            if old.task:
                old.task.cancel()
            _flows.pop(key, None)
    flow = _Flow(url_mode)
    flow.task = asyncio.create_task(runner(flow))
    _flows[flow.id] = flow
    return flow


def _outcome(flow: _Flow) -> dict[str, Any]:
    _flows.pop(flow.id, None)
    assert flow.task is not None
    try:
        return flow.task.result()
    except asyncio.CancelledError as exc:
        raise ToolError("Cancelled") from exc
    except (FlowError, TimeoutError) as exc:
        raise ToolError(str(exc)) from exc


async def _elicit(ctx: Context, question: _Question) -> Any:
    params = question.params
    if isinstance(params, types.ElicitRequestURLParams):
        return await ctx.session.elicit_url(params.message, params.url, params.elicitation_id, related_request_id=ctx.request_id)
    return await ctx.session.elicit_form(params.message, params.requested_schema, related_request_id=ctx.request_id)


async def _run_with_popups(ctx: Context, flow: _Flow) -> dict[str, Any]:
    """2025-11-25 era: show each question as a live pop-up during this one tool call."""

    async def url_done(elicitation_id: str) -> None:
        await ctx.session.send_elicit_complete(elicitation_id, related_request_id=ctx.request_id)

    flow.on_url_done = url_done
    try:
        while (question := await flow.next_question()) is not None:
            popup = asyncio.ensure_future(_elicit(ctx, question))
            withdrawn = asyncio.ensure_future(question.withdrawn.wait())
            done, _ = await asyncio.wait({popup, withdrawn}, return_when=asyncio.FIRST_COMPLETED)
            withdrawn.cancel()
            if popup in done:
                flow.answer(question.id, popup.result())
            else:
                popup.cancel()
        return _outcome(flow)
    finally:
        if flow.task and not flow.task.done():
            flow.task.cancel()
        _flows.pop(flow.id, None)


async def run_flow(ctx: Context, runner: Callable[[_Flow], Awaitable[dict[str, Any]]], unsupported: str) -> Any:
    """Run *runner* with pop-ups. *unsupported* is the error for clients that cannot show them."""
    form_mode, url_mode = _elicitation_modes(ctx)
    if not form_mode:
        raise ToolError(unsupported)
    if not ctx._is_modern_protocol():
        return await _run_with_popups(ctx, _start(url_mode, runner))

    # 2026-07-28 era: return each question as InputRequiredResult; the client re-calls with the answer.
    flow = _flows.get(ctx.request_state or "")
    if flow is None:
        flow = _start(url_mode, runner)
    for question_id, result in (ctx.input_responses or {}).items():
        flow.answer(question_id, result)
    question = await flow.next_question()
    if question is None:
        return _outcome(flow)
    return types.InputRequiredResult(
        input_requests={question.id: types.ElicitRequest(params=question.params)}, request_state=flow.id
    )
