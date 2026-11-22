"""List and delete the signed-in user's personal GreenLake API clients, from the AI chat.

Each GreenLake user can only have a few personal API clients, and ``com_login`` makes one per
computer, so old ones pile up. Both tools sign the user in (Okta push or password on a local
page, like ``com_login_start``), then:

- ``greenlake_list_api_clients_start`` shows the list (read-only).
- ``greenlake_delete_api_clients_start`` shows the list with check boxes. The user ticks what to
  delete and confirms on the page. The AI never picks what is deleted: it only sees names after
  the user confirmed, and a client is deleted only if it was ticked on the page.

The sign-in session lives in memory for one run and is never saved.
"""

from __future__ import annotations

import asyncio
import html
from typing import Any

from fastmcp.exceptions import ToolError

from proliant_mcp import chat_login, login
from proliant_mcp.chat import FlowError, FlowPage, Relay, _page

STATUS_TOOL = "greenlake_api_clients_status"
PURPOSE = "your GreenLake API clients"
_TIMEOUT = 900.0
_WAIT = 45.0
_START_WAIT = 40.0
_POLL = chat_login._keep_polling(STATUS_TOOL)

_session: Relay | None = None


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _public(client: dict[str, Any]) -> dict[str, Any]:
    note = []
    if client["this_computer"]:
        note.append("used by this computer's saved GreenLake login")
    if client["created_by"] != "proliant-mcp":
        note.append("not created by proliant-mcp")
    return {"name": client["name"], "client_id": client["client_id"], "created": client["created"], **({"note": "; ".join(note)} if note else {})}


def _note_html(client: dict[str, Any]) -> str:
    parts = []
    if client["this_computer"]:
        parts.append("<b>Used by this computer's saved GreenLake login.</b> The Compute Ops Management tools here stop working until you sign in again.")
    if client["created_by"] != "proliant-mcp":
        parts.append("Not created by proliant-mcp.")
    return " ".join(parts)


_STYLE = ("<style>main{width:min(64rem,92vw);margin-top:3vh}table{width:100%;border-collapse:collapse}"
          "td,th{padding:.4rem;text-align:left;vertical-align:top;border-bottom:1px solid #e3e5e8}"
          "td input[type=checkbox]{width:auto;margin:.2rem}code{font-size:.85rem;word-break:break-all}"
          ".warn{color:#c8102e}button+button{margin-left:.6rem}.sec{background:#fff;color:#425563;border:1px solid #ccd}</style>")


def _rows(clients: list[dict[str, Any]], checked: set[int] | None = None) -> str:
    out = []
    for i, c in enumerate(clients):
        box = ""
        if checked is not None:
            box = (f"<td><input type='checkbox' name='selected_{i}' value='1'{' checked' if i in checked else ''} "
                   f"aria-label='Delete {_escape(c['name'])}'></td>")
        out.append(f"<tr>{box}<td>{_escape(c['name'])}</td><td>{_escape(c['created'] or '-')}</td>"
                   f"<td><code>{_escape(c['client_id'] or '-')}</code></td><td>{_note_html(c)}</td></tr>")
    return "".join(out)


def _select_page(workspace: str, clients: list[dict[str, Any]], checked: set[int], error: str = ""):
    def render(nonce: str) -> str:
        body = (
            _STYLE
            + (f"<p class='err'>{_escape(error)}</p>" if error else "")
            + f"<p>Your personal API clients in <b>{_escape(workspace)}</b> ({len(clients)}). Tick the ones to delete. "
            "Nothing is deleted until you confirm on the next page.</p>"
            "<table><thead><tr><th>Delete</th><th>Name</th><th>Created</th><th>Client ID</th><th>Note</th></tr></thead>"
            f"<tbody>{_rows(clients, checked)}</tbody></table>"
            "<p><button type='submit' name='action' value='review'>Review deletion</button>"
            "<button type='submit' name='action' value='cancel' class='sec' formnovalidate>Delete nothing</button></p>"
        )
        return _page("Delete GreenLake API clients", f"<form method='post'><input type='hidden' name='step' value='select'>{body}</form>")
    return render


def _confirm_page(workspace: str, chosen: list[dict[str, Any]]):
    def render(nonce: str) -> str:
        warn = ""
        if any(c["this_computer"] for c in chosen):
            warn = ("<p class='warn'><b>This includes the API client used by this computer's saved GreenLake login.</b> "
                    "The Compute Ops Management tools here will stop working until you sign in again.</p>")
        body = (
            _STYLE
            + f"<p class='warn'><b>Permanently delete {len(chosen)} API client{'s' if len(chosen) != 1 else ''} "
            f"in {_escape(workspace)}?</b> Anything that uses them stops working. This cannot be undone.</p>{warn}"
            "<table><thead><tr><th>Name</th><th>Created</th><th>Client ID</th><th>Note</th></tr></thead>"
            f"<tbody>{_rows(chosen)}</tbody></table>"
            f"<p><button type='submit' name='action' value='delete'>Delete {len(chosen)}</button>"
            "<button type='submit' name='action' value='back' class='sec'>Back</button></p>"
        )
        return _page("Confirm deletion", f"<form method='post'><input type='hidden' name='step' value='confirm'>{body}</form>")
    return render


def _result_page(workspace: str, deleted: list[str], failed: list[dict[str, str]], remaining: int | None):
    rows = "".join(f"<li>Deleted {_escape(n)}</li>" for n in deleted) + "".join(
        f"<li class='err'>Could not delete {_escape(f['name'])}: {_escape(f['reason'])}</li>" for f in failed)
    left = f"<p>{remaining} API client{'s' if remaining != 1 else ''} left in {_escape(workspace)}.</p>" if remaining is not None else ""
    return f"<ul>{rows}</ul>{left}<p>Go back to your AI chat; you can close this tab.</p>"


async def _answer(page: FlowPage, step: str, render: Any, fields: tuple[str, ...]) -> dict[str, str]:
    try:
        return await asyncio.wait_for(page.expect(step, render, fields), _TIMEOUT)
    except TimeoutError as exc:
        raise FlowError("Timed out waiting for the browser page") from exc


def _preselected(clients: list[dict[str, Any]], preselect: list[str] | None) -> set[int]:
    """Tick boxes the assistant suggested. Never the client this computer's login uses; the user can still tick it."""
    wanted = [w.lower() for w in (preselect or []) if w.strip()]
    return {i for i, c in enumerate(clients) if not c["this_computer"] and any(w in c["name"].lower() for w in wanted)}


async def _run(session: Relay, delete: bool, email: str | None, workspace: str | None, preselect: list[str] | None) -> dict[str, Any]:
    page = FlowPage("GreenLake API clients")
    try:
        email = email or login.load_login().get("email") or None
        ws = await chat_login.relay_workspace(session, page, email, workspace, STATUS_TOOL, PURPOSE)
        page.show("<p>Reading your API clients...</p>")
        clients = await login.list_api_clients(ws.access_token, ws.ccs_session)
        if not delete:
            page.finish(_STYLE + f"<p>Your personal API clients in <b>{_escape(ws.workspace_name)}</b> ({len(clients)}).</p>"
                        "<table><thead><tr><th>Name</th><th>Created</th><th>Client ID</th><th>Note</th></tr></thead>"
                        f"<tbody>{_rows(clients)}</tbody></table><p>Go back to your AI chat; you can close this tab.</p>")
            return {
                "status": "listed", "workspace": ws.workspace_name, "count": len(clients),
                "clients": [_public(c) for c in clients],
                "next_step": "Show the user a table of these clients. To delete some, call greenlake_delete_api_clients_start; "
                "the user chooses which ones on a page.",
            }
        if not clients:
            page.finish(f"<p>There are no personal API clients in <b>{_escape(ws.workspace_name)}</b>.</p><p>Go back to your AI chat.</p>")
            return {"status": "nothing_to_delete", "workspace": ws.workspace_name, "count": 0}

        checked = _preselected(clients, preselect)
        error = ""
        while True:
            session.publish(
                "select_api_clients",
                f"{page.link()}The user ticks the API clients to delete on that page, reviews and confirms there. Do not choose "
                f"for them and do not ask them to confirm in the chat. {_POLL}",
                url=page.url, workspace=ws.workspace_name, clients=[_public(c) for c in clients],
            )
            values = await _answer(page, "select", _select_page(ws.workspace_name, clients, checked, error),
                                   ("action",) + tuple(f"selected_{i}" for i in range(len(clients))))
            if values["action"] == "cancel":
                page.finish("<p>Nothing was deleted. Go back to your AI chat.</p>")
                return {"status": "cancelled", "workspace": ws.workspace_name, "deleted": [], "note": "The user chose to delete nothing."}
            checked = {i for i in range(len(clients)) if values.get(f"selected_{i}")}
            if not checked:
                error = "Tick at least one API client."
                continue
            chosen = [clients[i] for i in sorted(checked)]
            session.publish(
                "confirm_deletion",
                f"{page.link()}The user is reviewing {len(chosen)} API client(s) and must confirm deletion on that page. {_POLL}",
                url=page.url, workspace=ws.workspace_name, selected=[c["name"] for c in chosen],
            )
            values = await _answer(page, "confirm", _confirm_page(ws.workspace_name, chosen), ("action",))
            if values["action"] == "back":
                error = ""
                continue
            if values["action"] != "delete":
                error = "Choose Delete or Back."
                continue
            break

        page.show(f"<p>Deleting {len(chosen)} API client(s)...</p>")
        session.publish("deleting", f"Deleting the API clients the user confirmed. {_POLL}", workspace=ws.workspace_name)
        deleted: list[str] = []
        failed: list[dict[str, str]] = []
        for c in chosen:
            try:
                await login.delete_api_client(ws.access_token, ws.ccs_session, c["name"])
                deleted.append(c["name"])
            except login.LoginError as exc:
                failed.append({"name": c["name"], "reason": str(exc)})
        try:
            remaining: int | None = len(await login.list_api_clients(ws.access_token, ws.ccs_session))
        except login.LoginError:
            remaining = None
        page.finish(_result_page(ws.workspace_name, deleted, failed, remaining))
        result: dict[str, Any] = {"status": "deleted" if deleted else "failed", "workspace": ws.workspace_name,
                                  "deleted": deleted, "failed": failed, "remaining": remaining}
        if any(c["this_computer"] and c["name"] in deleted for c in chosen):
            result["note"] = ("The API client of this computer's saved GreenLake login was deleted, so the com_ tools fail until "
                              "the user signs in again with com_login (or com_login_start).")
        return result
    except BaseException as exc:
        reason = "Cancelled." if isinstance(exc, asyncio.CancelledError) else str(exc)
        page.finish(f"<p class='err'>{_escape(reason)}</p><p>Nothing more was changed. Return to your chat.</p>")
        raise


async def start(delete: bool, email: str | None, workspace: str | None, preselect: list[str] | None = None) -> dict[str, Any]:
    global _session
    email = (email or "").strip()
    if email and "@" not in email:
        raise ToolError("That is not an email address. Pass the user's HPE GreenLake email, or omit it to let the user type it on a page.")
    if _session is not None and _session.task is not None and not _session.task.done():
        _session.task.cancel()
    session = Relay("GreenLake API client step failed: ", "Call the start tool again to retry.")
    session.task = asyncio.create_task(_run(session, delete, email or None, workspace, preselect))
    _session = session
    await session.wait_for_news(_START_WAIT)
    return session.current()


async def status(workspace: str | None) -> dict[str, Any]:
    session = _session
    if session is None or session.task is None:
        raise ToolError("Nothing in progress. Call greenlake_list_api_clients_start or greenlake_delete_api_clients_start first.")
    if workspace and session.choice is not None and not session.choice.done():
        session.choice.set_result(workspace)
    return await session.status(_WAIT, f"Call {STATUS_TOOL} again.")
