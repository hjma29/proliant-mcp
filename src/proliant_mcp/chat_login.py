"""``com_login``: sign in to HPE GreenLake from the AI chat with pop-ups (MCP elicitation).

1. A pop-up asks for the GreenLake email.
2. ``@hpe.com`` accounts: a pop-up shows the number to tap in Okta Verify.
   Other accounts (e.g. gmail.com): a one-time page on this computer asks for the
   password in a masked field. The MCP spec forbids collecting passwords in chat forms,
   so it is opened with URL-mode elicitation (or linked from a form on clients without
   URL mode); the password goes straight from the browser to this server, never through
   the AI client or model.
3. A pop-up picks the workspace when there are several.

The sign-in (``login.py``) runs as a background task that posts its questions. Each
``com_login`` call shows them: as live pop-ups on 2025-11-25-era connections, or as
``InputRequiredResult`` rounds (the client re-calls the tool with the answers) on
2026-07-28 connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import secrets
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

from proliant_mcp import login
from proliant_mcp.com import ComError
from proliant_mcp.config import com_login_path, load_com

ASK_TIMEOUT = 300.0
FLOW_TTL = 900.0


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


def _form(message: str, properties: dict[str, Any], required: list[str]) -> types.ElicitRequestFormParams:
    return types.ElicitRequestFormParams(
        message=message, requested_schema={"type": "object", "properties": properties, "required": required}
    )


class _PasswordPage:
    """One-time local page with a masked password field. Bound to 127.0.0.1, random port
    and path, accepts one POST, and never logs the request body."""

    def __init__(self, email: str, error: str | None) -> None:
        loop = asyncio.get_running_loop()
        self.token = secrets.token_urlsafe(24)
        self.password: asyncio.Future[str] = loop.create_future()
        page = self

        def deliver(value: str) -> None:
            if not page.password.done():
                page.password.set_result(value)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: str) -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
                self.end_headers()
                self.wfile.write(data)

            def _ours(self) -> bool:
                return urllib.parse.urlsplit(self.path).path == f"/{page.token}"

            def do_GET(self) -> None:
                if not self._ours():
                    return self._send(404, "Not found")
                if page.password.done():
                    return self._send(410, _page("Already used", "<p>This sign-in page was already used. You can close this tab.</p>"))
                self._send(200, _password_form(email, error))

            def do_POST(self) -> None:
                if not self._ours() or page.password.done():
                    return self._send(410, _page("Already used", "<p>This sign-in page was already used. You can close this tab.</p>"))
                length = min(int(self.headers.get("Content-Length") or 0), 8192)
                fields = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
                value = fields.get("password", [""])[0]
                if not value:
                    return self._send(200, _password_form(email, "Enter your password."))
                loop.call_soon_threadsafe(deliver, value)
                self._send(200, _page("Password received", "<p>Password received. Go back to your AI chat; you can close this tab.</p>"))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/{self.token}"
        threading.Thread(target=self._server.serve_forever, name="proliant-mcp-password-page", daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<title>{html.escape(title)}</title><style>"
        "body{font-family:system-ui,sans-serif;background:#f4f5f6;margin:0;display:flex;justify-content:center}"
        "main{background:#fff;margin-top:12vh;padding:2rem 2.5rem;border-radius:10px;box-shadow:0 2px 12px #0002;width:22rem}"
        "h1{font-size:1.25rem;margin:0 0 1rem}.who{color:#425563}.err{color:#c8102e}"
        "input{width:100%;box-sizing:border-box;padding:.6rem;font-size:1rem;margin:.4rem 0 1rem}"
        "button{background:#01a982;color:#fff;border:0;border-radius:6px;padding:.6rem 1.4rem;font-size:1rem;cursor:pointer}"
        f"</style></head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"
    )


def _password_form(email: str, error: str | None) -> str:
    err = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    return _page(
        "Sign in to HPE GreenLake",
        f"<p class='who'>{html.escape(email)}</p>{err}"
        "<form method='post' autocomplete='off'>"
        "<label for='password'>GreenLake password</label>"
        "<input id='password' name='password' type='password' autocomplete='current-password' autofocus required>"
        "<button type='submit'>Sign in</button></form>"
        "<p><small>For the proliant-mcp Compute Ops Management tools. The password is used once to sign in "
        "and is not stored.</small></p>",
    )


class _Flow:
    """One sign-in in progress: the background task and the questions it is waiting on."""

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
        """The next question still waiting for an answer, or None once the sign-in has ended."""
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


def _accepted(result: Any) -> dict[str, Any]:
    if getattr(result, "action", None) != "accept":
        raise login.LoginError("Sign-in cancelled")
    return dict(getattr(result, "content", None) or {})


class ChatUI:
    """``login.LoginUI`` over MCP elicitation."""

    def __init__(self, flow: _Flow) -> None:
        self.flow = flow

    def say(self, message: str) -> None:
        self.flow.notes.append(message)

    async def _answer_or(self, question: _Question, work: asyncio.Future[Any], timeout: float) -> tuple[bool, Any]:
        """Wait for the person's answer or for *work* to finish, whichever is first.
        Returns (True, work result) or (False, the answer)."""
        done, _ = await asyncio.wait({question.answer, work}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            question.withdraw()
            return True, work.result()
        if question.answer in done:
            return False, question.answer.result()
        question.withdraw()
        raise login.LoginError("No answer within 5 minutes")

    async def email(self, default: str) -> str:
        email_field: dict[str, Any] = {
            "type": "string",
            "format": "email",
            "title": "HPE GreenLake email",
            "description": "HPE accounts approve an Okta Verify push. Other accounts (e.g. gmail.com) type their "
            "GreenLake password on a secure page, never in this chat.",
        }
        if default:
            email_field["default"] = default
        question = await self.flow.ask(
            _form("Sign in to HPE GreenLake for the Compute Ops Management tools.", {"email": email_field}, ["email"])
        )
        answer = await asyncio.wait_for(question.answer, ASK_TIMEOUT)
        email = str(_accepted(answer).get("email", "")).strip()
        if "@" not in email:
            raise login.LoginError("A GreenLake email address is required")
        return email

    async def password(self, email: str, error: str | None) -> str:
        page = _PasswordPage(email, error)
        try:
            prefix = f"{error} " if error else ""
            if self.flow.url_mode:
                params: types.ElicitRequestFormParams | types.ElicitRequestURLParams = types.ElicitRequestURLParams(
                    message=f"{prefix}Type your HPE GreenLake password for {email} on a sign-in page served by the "
                    "proliant-mcp server on this computer. The password is not shared with the AI.",
                    url=page.url,
                    elicitation_id=page.token,
                )
            else:
                params = _form(
                    f"{prefix}Open {page.url} in your browser and type your HPE GreenLake password for {email} there "
                    "(a page served by the proliant-mcp server on this computer; the password is not shared with the AI). "
                    "Then submit this form.",
                    {"done": {"type": "boolean", "title": "I typed my password on that page", "default": True}},
                    ["done"],
                )
            question = await self.flow.ask(params)
            submitted, value = await self._answer_or(question, page.password, ASK_TIMEOUT)
            if not submitted:
                _accepted(value)
                value = await asyncio.wait_for(asyncio.shield(page.password), ASK_TIMEOUT)
            if self.flow.url_mode and self.flow.on_url_done:
                with contextlib.suppress(Exception):
                    await self.flow.on_url_done(page.token)
            return value
        except TimeoutError as exc:
            raise login.LoginError("No password entered within 5 minutes") from exc
        finally:
            page.close()

    async def approve(self, number: Any, approval: asyncio.Task[str]) -> str:
        tap = f"tap {number}" if number is not None else "approve the push"
        question = await self.flow.ask(
            _form(
                f"Open Okta Verify on your phone and {tap}. Then submit this form.",
                {"approved": {"type": "boolean", "title": f"I did {tap} in Okta Verify", "default": True}},
                ["approved"],
            )
        )
        self.say(f"Okta Verify: {tap}.")
        approved, value = await self._answer_or(question, approval, login.POLL_TIMEOUT + 30)
        if approved:
            return value
        _accepted(value)
        return await approval

    async def workspace(self, names: list[str]) -> str:
        question = await self.flow.ask(
            _form(
                "Which HPE GreenLake workspace should the Compute Ops Management tools use?",
                {"workspace": {"type": "string", "title": "Workspace", "enum": names}},
                ["workspace"],
            )
        )
        answer = await asyncio.wait_for(question.answer, ASK_TIMEOUT)
        return str(_accepted(answer).get("workspace", ""))


async def _sign_in(flow: _Flow, ui: ChatUI, email: str | None, workspace: str | None, region: str | None) -> dict[str, Any]:
    address = await ui.email(email or login.load_login().get("email", ""))
    data = await login.login(address, ui, workspace, region)
    result: dict[str, Any] = {
        "status": "signed_in",
        "email": address,
        "workspace": data["workspace"],
        "region": data["region"],
        "api_client": data["credential_name"],
        "saved_to": str(com_login_path()),
    }
    try:
        result["servers"] = await login.server_count(data)
    except ComError as exc:
        flow.notes.append(f"Saved, but the first Compute Ops Management call failed: {exc}")
    first = next(iter(load_com().values()), None)
    if first is not None and first.source != str(com_login_path()):
        flow.notes.append(
            f"COM account '{first.name}' from {first.source} is configured and is used first; remove it to use this login."
        )
    else:
        flow.notes.append("The com_ tools use this login now; no restart needed.")
    result["notes"] = flow.notes
    return result


_flows: dict[str, _Flow] = {}


def _elicitation_modes(ctx: Context) -> tuple[bool, bool]:
    params = ctx.session.client_params
    cap = params.capabilities.elicitation if params is not None else None
    if cap is None:
        return False, False
    url = cap.url is not None
    return cap.form is not None or not url, url


def _start(ctx: Context, url_mode: bool, email: str | None, workspace: str | None, region: str | None) -> _Flow:
    now = time.monotonic()
    for key, old in list(_flows.items()):
        if now - old.started > FLOW_TTL:
            if old.task:
                old.task.cancel()
            _flows.pop(key, None)
    flow = _Flow(url_mode)
    ui = ChatUI(flow)
    flow.task = asyncio.create_task(_sign_in(flow, ui, email, workspace, region))
    _flows[flow.id] = flow
    return flow


def _outcome(flow: _Flow) -> dict[str, Any]:
    _flows.pop(flow.id, None)
    assert flow.task is not None
    try:
        return flow.task.result()
    except asyncio.CancelledError as exc:
        raise ToolError("Sign-in cancelled") from exc
    except (login.LoginError, TimeoutError) as exc:
        raise ToolError(f"GreenLake sign-in failed: {exc}") from exc


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


async def com_login(ctx: Context, email: str | None, workspace: str | None, region: str | None) -> Any:
    form_mode, url_mode = _elicitation_modes(ctx)
    if not form_mode:
        raise ToolError(
            "This AI client cannot show sign-in pop-ups (MCP elicitation). Ask the user to run "
            "`proliant-mcp login` in a terminal on the machine running this server, or to configure a GreenLake "
            "API client (README: HPE Compute Ops Management)."
        )
    if not ctx._is_modern_protocol():
        return await _run_with_popups(ctx, _start(ctx, url_mode, email, workspace, region))

    # 2026-07-28 era: return each question as InputRequiredResult; the client re-calls with the answer.
    flow = _flows.get(ctx.request_state or "")
    if flow is None:
        flow = _start(ctx, url_mode, email, workspace, region)
    for question_id, result in (ctx.input_responses or {}).items():
        flow.answer(question_id, result)
    question = await flow.next_question()
    if question is None:
        return _outcome(flow)
    return types.InputRequiredResult(
        input_requests={question.id: types.ElicitRequest(params=question.params)}, request_state=flow.id
    )
