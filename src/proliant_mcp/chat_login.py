"""``com_login``: sign in to HPE GreenLake from the AI chat with pop-ups (MCP elicitation).

1. A pop-up asks for the GreenLake email.
2. ``@hpe.com`` accounts: a pop-up shows the number to tap in Okta Verify.
   Other accounts (e.g. gmail.com): a one-time page on this computer asks for the password
   in a masked field (see ``chat.py``); it never passes through the AI client or model.
3. A pop-up picks the workspace when there are several.

The sign-in itself is ``login.py``; this file only maps its questions to pop-ups.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp import Context
from fastmcp.exceptions import ToolError

from proliant_mcp import login
from proliant_mcp.chat import FlowError, Prompter, Relay, SecretPrompt, _Flow, accepted, form_params, run_flow
from proliant_mcp.com import ComError
from proliant_mcp.config import com_login_path, load_com

UNSUPPORTED = (
    "This AI client cannot show sign-in pop-ups (MCP elicitation). Sign the user in here instead: call com_login_start "
    "(with their HPE GreenLake email if you know it; without it the user types it on a local page), relay what it "
    "returns (a page link, or the Okta Verify number), and call com_login_status until it says signed_in."
)


class ChatUI(Prompter):
    """``login.LoginUI`` over MCP elicitation."""

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
        answer = await self.form("Sign in to HPE GreenLake for the Compute Ops Management tools.", {"email": email_field}, ["email"])
        email = str(answer.get("email", "")).strip()
        if "@" not in email:
            raise FlowError("A GreenLake email address is required")
        return email

    async def password(self, email: str, error: str | None) -> str:
        return await self.secret(
            SecretPrompt(
                message=f"Type your HPE GreenLake password for {email} on a sign-in page.",
                title="Sign in to HPE GreenLake",
                who=email,
                label="GreenLake password",
                button="Sign in",
                note="For the proliant-mcp Compute Ops Management tools. The password is used once to sign in and is not stored.",
            ),
            error,
        )

    async def approve(self, number: Any, approval: asyncio.Task[str]) -> str:
        tap = f"tap {number}" if number is not None else "approve the push"
        question = await self.flow.ask(
            form_params(
                f"Open Okta Verify on your phone and {tap}. Then submit this form.",
                {"approved": {"type": "boolean", "title": f"I did {tap} in Okta Verify", "default": True}},
                ["approved"],
            )
        )
        self.say(f"Okta Verify: {tap}.")
        approved, value = await self.answer_or(question, approval, login.POLL_TIMEOUT + 30)
        if approved:
            return value
        accepted(value)
        return await approval

    async def workspace(self, names: list[str]) -> str:
        answer = await self.form(
            "Which HPE GreenLake workspace should the Compute Ops Management tools use?",
            {"workspace": {"type": "string", "title": "Workspace", "enum": names}},
            ["workspace"],
        )
        return str(answer.get("workspace", ""))


async def _signed_in(address: str, data: dict[str, Any], notes: list[str]) -> dict[str, Any]:
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
        notes.append(f"Saved, but the first Compute Ops Management call failed: {exc}")
    first = next(iter(load_com().values()), None)
    if first is not None and first.source != str(com_login_path()):
        notes.append(f"COM account '{first.name}' from {first.source} is configured and is used first; remove it to use this login.")
    else:
        notes.append("The com_ tools use this login now; no restart needed.")
    result["notes"] = notes
    return result


async def _sign_in(flow: _Flow, email: str | None, workspace: str | None, region: str | None) -> dict[str, Any]:
    ui = ChatUI(flow)
    try:
        address = await ui.email(email or login.load_login().get("email", ""))
        data = await login.login(address, ui, workspace, region)
    except (login.LoginError, FlowError) as exc:
        raise FlowError(f"GreenLake sign-in failed: {exc}.") from exc
    return await _signed_in(address, data, flow.notes)


async def com_login(ctx: Context, email: str | None, workspace: str | None, region: str | None) -> Any:
    try:
        return await run_flow(ctx, lambda flow: _sign_in(flow, email, workspace, region), UNSUPPORTED)
    except ToolError as exc:
        known = (email or login.load_login().get("email", "")).strip()
        result = await com_login_start(known or None, workspace, region)
        result["note"] = "The sign-in pop-up did not appear, so sign-in continued without pop-ups. Relay the next step to the user."
        return result


# ── Sign-in without pop-ups: com_login_start + com_login_status ───────────────
#
# For clients that cannot show pop-ups (Claude Desktop, some Codex versions, harnesses that
# auto-cancel them). Each tool call returns at once with what the user must do next (the
# Okta Verify number, a link to the password page, or the workspaces to choose from); the
# assistant relays it in the chat and calls com_login_status until the sign-in ends. Calls
# return within about 45 seconds, inside the 60 s default tool timeout of clients like Codex.

STATUS_WAIT = 45.0
START_WAIT = 40.0


class RelayUI:
    """``login.LoginUI`` that publishes each step as a state for the next tool call to return."""

    def __init__(self, session: Relay) -> None:
        self.session = session

    def say(self, message: str) -> None:
        self.session.notes.append(message)

    async def password(self, email: str, error: str | None) -> str:
        from proliant_mcp.chat import _PasswordPage

        page = _PasswordPage(
            SecretPrompt(
                message=f"Type your HPE GreenLake password for {email}.",
                title="Sign in to HPE GreenLake",
                who=email,
                label="GreenLake password",
                button="Sign in",
                note="For the proliant-mcp Compute Ops Management tools. The password is used once to sign in and is not stored.",
            ),
            error,
        )
        try:
            self.session.publish(
                "enter_password",
                f"{error + ' ' if error else ''}Ask the user to open {page.url} in a browser on the computer running this "
                f"server and type the GreenLake password for {email} there (never in the chat). Then call com_login_status.",
                url=page.url,
            )
            return await asyncio.wait_for(page.password, login.POLL_TIMEOUT)
        except TimeoutError as exc:
            raise login.LoginError("No password entered within 5 minutes") from exc
        finally:
            page.close()

    async def approve(self, number: Any, approval: asyncio.Task[str]) -> str:
        tap = f"tap {number}" if number is not None else "approve the push"
        self.session.publish(
            "approve_push",
            f"Tell the user: open Okta Verify on your phone and {tap} on the newest request. Then call com_login_status.",
            number=number,
        )
        return await approval

    async def workspace(self, names: list[str]) -> str:
        self.session.choice = asyncio.get_running_loop().create_future()
        self.session.publish(
            "choose_workspace",
            "Ask the user which HPE GreenLake workspace to use, then call com_login_status with workspace set to their choice.",
            workspaces=names,
        )
        try:
            return await asyncio.wait_for(self.session.choice, login.POLL_TIMEOUT)
        except TimeoutError as exc:
            raise login.LoginError("No workspace chosen within 5 minutes") from exc


_relay: Relay | None = None


async def _ask_email_on_page(session: Relay) -> str:
    from proliant_mcp.chat import _PasswordPage

    page = _PasswordPage(
        SecretPrompt(
            message="Type your HPE GreenLake email.",
            title="Sign in to HPE GreenLake",
            label="HPE GreenLake email",
            button="Continue",
            note="For the proliant-mcp Compute Ops Management tools. HPE accounts then approve an Okta Verify push; other accounts type their password on the next page.",
            input_type="email",
        ),
        None,
    )
    try:
        session.publish(
            "enter_email",
            f"Ask the user to open {page.url} in a browser on the computer running this server and type their HPE GreenLake "
            "email there (the email is not a secret, but do not guess it). Then call com_login_status.",
            url=page.url,
        )
        email = (await asyncio.wait_for(page.password, login.POLL_TIMEOUT)).strip()
    except TimeoutError as exc:
        raise login.LoginError("No email entered within 5 minutes") from exc
    finally:
        page.close()
    if "@" not in email:
        raise login.LoginError("A GreenLake email address is required")
    return email


async def _relay_sign_in(session: Relay, email: str | None, workspace: str | None, region: str | None) -> dict[str, Any]:
    email = email or await _ask_email_on_page(session)
    data = await login.login(email, RelayUI(session), workspace, region)
    return await _signed_in(email, data, session.notes)


async def com_login_start(email: str | None, workspace: str | None, region: str | None) -> dict[str, Any]:
    global _relay
    email = (email or "").strip()
    if email and "@" not in email:
        raise ToolError("That is not an email address. Pass the user's HPE GreenLake email, or omit it to let the user type it on a page.")
    if _relay is not None and _relay.task is not None and not _relay.task.done():
        _relay.task.cancel()
    session = Relay("GreenLake sign-in failed: ", "Call com_login_start to try again.")
    session.task = asyncio.create_task(_relay_sign_in(session, email or None, workspace, region))
    _relay = session
    await session.wait_for_news(START_WAIT)
    return session.current()


async def com_login_status(workspace: str | None) -> dict[str, Any]:
    session = _relay
    if session is None or session.task is None:
        raise ToolError("No sign-in in progress. Call com_login_start first.")
    if workspace and session.choice is not None and not session.choice.done():
        session.choice.set_result(workspace)
    return await session.status(STATUS_WAIT, "Call com_login_status again.")
