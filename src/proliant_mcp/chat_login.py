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

from proliant_mcp import login
from proliant_mcp.chat import FlowError, Prompter, SecretPrompt, _Flow, accepted, form_params, run_flow
from proliant_mcp.com import ComError
from proliant_mcp.config import com_login_path, load_com

UNSUPPORTED = (
    "This AI client cannot show sign-in pop-ups (MCP elicitation). Ask the user to run "
    "`proliant-mcp login` in a terminal on the machine running this server, or to configure a GreenLake "
    "API client (README: HPE Compute Ops Management)."
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


async def _sign_in(flow: _Flow, email: str | None, workspace: str | None, region: str | None) -> dict[str, Any]:
    ui = ChatUI(flow)
    try:
        address = await ui.email(email or login.load_login().get("email", ""))
        data = await login.login(address, ui, workspace, region)
    except (login.LoginError, FlowError) as exc:
        raise FlowError(f"GreenLake sign-in failed: {exc}") from exc
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


async def com_login(ctx: Context, email: str | None, workspace: str | None, region: str | None) -> Any:
    return await run_flow(ctx, lambda flow: _sign_in(flow, email, workspace, region), UNSUPPORTED)
