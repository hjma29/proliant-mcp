"""``add_server`` and ``import_inventory``: build the inventory from the AI chat with pop-ups.

Neither tool takes a file path or a password as an argument, so the AI cannot choose which
file is read or see a password: the user types the path in a pop-up and the password on the
secure local page (see ``chat.py``). Results never contain passwords.
"""

from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Context
from fastmcp.exceptions import ToolError

from proliant_mcp import inventory
from proliant_mcp.chat import FlowError, FlowPage, PopupCancelled, Prompter, Relay, SecretPrompt, _Flow, popup_failed, run_flow
from proliant_mcp.inventory import Entry, InventoryError
from proliant_mcp.probe import LOGIN_ERRORS, PROBE_ERRORS, detect_kind, probe, probe_many

CHECK_LIMIT = 30
LIST_LIMIT = 50

UNSUPPORTED_ADD = (
    "This AI client cannot show pop-ups (MCP elicitation). Add the server here instead: call add_server_start "
    "(with the address only if the user gave it; otherwise the user types it on a local page), relay the page link "
    "it returns, and call add_server_status until it says saved."
)
UNSUPPORTED_IMPORT = (
    "This AI client cannot show pop-ups (MCP elicitation). Ask the user to run `proliant-mcp import FILE` "
    "in a terminal on the machine running this server (docs/configuration.md)."
)


def _transport() -> httpx.AsyncBaseTransport | None:
    from proliant_mcp import server  # tests swap server._transport to a fake iLO / OneView

    return server._transport


def _cap(items: list[str]) -> list[str]:
    return items[:LIST_LIMIT] + ([f"...and {len(items) - LIST_LIMIT} more"] if len(items) > LIST_LIMIT else [])


async def _add_server(flow: _Flow, kind: str | None, name: str | None, host: str | None, username: str | None) -> dict[str, Any]:
    ui = Prompter(flow)
    fields: dict[str, dict[str, Any]] = {
        "type": {
            "type": "string",
            "title": "What is it?",
            "oneOf": [{"const": "ilo", "title": "HPE iLO (one server)"}, {"const": "oneview", "title": "HPE OneView / Synergy Composer"}],
            "default": kind or "ilo",
        },
        "host": {"type": "string", "title": "iLO or OneView IP address or name"},
        "name": {
            "type": "string",
            "title": "Name (optional)",
            "description": "Short name the assistant uses, such as dl380-lab. Defaults to the address. An existing name is replaced.",
        },
        "username": {"type": "string", "title": "User name", "default": username or "Administrator"},
    }
    if host:
        fields["host"]["default"] = host
    if name:
        fields["name"]["default"] = name
    try:
        answer = await ui.form(
            "Add a server to your inventory. The password is asked next, on a secure page, and is checked before anything is saved.",
            fields,
            ["type", "host"],
        )
    except PopupCancelled:
        raise
    try:
        draft = inventory.make_entry(str(answer.get("name", "")), str(answer.get("type", "ilo")), str(answer.get("host", "")), str(answer.get("username", "")))
    except InventoryError as exc:
        raise FlowError(f"Add server failed: {exc}") from exc
    return await _check_and_save(draft, lambda prompt, error: ui.secret(prompt, error))


def _password_prompt(draft: Entry) -> SecretPrompt:
    label = "iLO" if draft.kind == "ilo" else "OneView"
    return SecretPrompt(
        message=f"Type the {label} password for {draft.username} on {draft.host}.",
        title=f"{label} password for {draft.name}",
        who=f"{draft.username} @ {draft.host}",
        label=f"{label} password",
        button="Save",
        note="It is checked against the server, then saved in your proliant-mcp inventory file on this computer "
        "(readable only by you). It is not shared with the AI.",
    )


_KIND_NAMES = {"ilo": "an HPE iLO", "oneview": "HPE OneView / Synergy Composer"}


async def _check_and_save(draft: Entry, ask: Callable[[SecretPrompt, str | None], Awaitable[str]]) -> dict[str, Any]:
    """Ask for the password (up to three tries), log in with it, and only then save the entry."""
    corrected = None
    detected = await detect_kind(draft.host, _transport())
    if detected and detected != draft.kind:
        corrected = f"{draft.host} is {_KIND_NAMES[detected]}, not {_KIND_NAMES[draft.kind]}, so it was added as {detected}."
        draft = inventory.make_entry(draft.name, detected, draft.host, draft.username)
    label = "iLO" if draft.kind == "ilo" else "OneView"
    prompt = _password_prompt(draft)
    error: str | None = None
    for _ in range(3):
        password = await ask(prompt, error)
        try:
            entry = inventory.make_entry(draft.name, draft.kind, draft.host, draft.username, password)
        except InventoryError as exc:
            error = f"{exc}."
            continue
        try:
            detail = await probe(entry, _transport())
        except LOGIN_ERRORS:
            error = f"{label} at {entry.host} rejected that user name or password."
            continue
        except PROBE_ERRORS as exc:
            raise FlowError(f"Add server failed: {exc}. Nothing was saved; check the address and that this computer can reach it.") from exc
        break
    else:
        raise FlowError(f"Add server failed: {label} rejected the password three times. Nothing was saved.")

    saved = inventory.save_entries([entry])
    notes = [f"Saved to {saved.path} (readable only by you). The tools use it now; no restart needed."]
    if corrected:
        notes.insert(0, corrected)
    if saved.copied_from:
        notes.append(f"Your existing servers were copied from {saved.copied_from} first; that file is unchanged.")
    return {
        "status": "saved",
        "name": entry.name,
        "type": entry.kind,
        "host": entry.host,
        "username": entry.username,
        "checked": detail,
        "replaced_existing": bool(saved.replaced),
        "saved_to": str(saved.path),
        "notes": notes,
    }


async def _import(flow: _Flow) -> dict[str, Any]:
    ui = Prompter(flow)
    answer = await ui.form(
        "Import servers from a file into your inventory. The file stays where it is.",
        {
            "path": {
                "type": "string",
                "title": "Path to your .ini or .csv file",
                "description": "Columns: name, type (ilo or oneview), host, username, password. In Excel use File > Save As > CSV. "
                "Windows: Shift + right-click the file > Copy as path.",
            },
            "replace": {"type": "boolean", "title": "Replace entries that are already in my inventory", "default": False},
        },
        ["path"],
    )
    try:
        parsed = inventory.parse_inventory_file(str(answer.get("path", "")))
    except InventoryError as exc:
        raise FlowError(f"Import failed: {exc}") from exc
    source = Path(str(answer["path"]).strip().strip("\"'")).name
    had_passwords = any(e.password for e in parsed.entries)
    missing = [e.name for e in parsed.entries if not e.password]
    summary = inventory.summarize(parsed, inventory.existing_names())

    confirm: dict[str, dict[str, Any]] = {"proceed": {"type": "boolean", "title": f"Import {summary}", "default": True}}
    if missing:
        confirm["shared_password"] = {
            "type": "boolean",
            "title": f"{len(missing)} entries have no password: ask me for one password they all share",
            "default": True,
        }
    confirm["check"] = {
        "type": "boolean",
        "title": f"Check that each server accepts the login (about 2 seconds each, first {CHECK_LIMIT})",
        "default": len(parsed.entries) <= CHECK_LIMIT,
    }
    chosen = await ui.form(f"Found {summary} in {source}. Nothing is saved until you submit this.", confirm, ["proceed"])
    if not chosen.get("proceed"):
        raise FlowError("Cancelled")

    entries: list[Entry] = parsed.entries
    if missing and chosen.get("shared_password"):
        password = await ui.secret(
            SecretPrompt(
                message=f"Type the password shared by the {len(missing)} entries that have none.",
                title="Shared password",
                who=f"{len(missing)} entries",
                label="Password",
                button="Save",
                note="Saved in your proliant-mcp inventory file on this computer (readable only by you). It is not shared with the AI.",
            )
        )
        try:
            entries = [inventory.make_entry(e.name, e.kind, e.host, e.username, e.password or password, e.verify_ssl) for e in entries]
        except InventoryError as exc:
            raise FlowError(f"Import failed: {exc}") from exc

    saved = inventory.save_entries(entries, replace_existing=bool(answer.get("replace")))
    changed = set(saved.added) | set(saved.replaced)
    result: dict[str, Any] = {
        "status": "imported",
        "file": source,
        "saved_to": str(saved.path),
        "added": _cap(saved.added),
        "replaced": _cap(saved.replaced),
        "skipped_already_present": _cap(saved.skipped),
        "invalid_rows": _cap(parsed.problems),
        "without_password": _cap([e.name for e in entries if not e.password and e.name in changed]),
    }
    notes = [f"Saved to {saved.path} (readable only by you). The tools use it now; no restart needed."]
    if saved.copied_from:
        notes.append(f"Your existing servers were copied from {saved.copied_from} first; that file is unchanged.")
    if had_passwords:
        notes.append(f"{source} contains passwords in plain text; delete it or keep it somewhere private.")
    if saved.skipped:
        notes.append("Entries that were already present were kept as they were; import again with 'Replace' ticked to overwrite them.")

    if chosen.get("check") and changed:
        to_check = [e for e in entries if e.name in changed][:CHECK_LIMIT]
        results = await probe_many(to_check, _transport())
        result["checked"] = {
            "ok": sum(1 for r in results if r.ok),
            "password_rejected": [r.name for r in results if r.login_refused],
            "unreachable_or_failed": [{"name": r.name, "error": r.detail} for r in results if not r.ok and not r.login_refused][:10],
        }
        if len(changed) > CHECK_LIMIT:
            notes.append(f"Only the first {CHECK_LIMIT} entries were checked; run proliant-mcp --check --connect for all.")
    result["notes"] = notes
    return result


async def add_server(ctx: Context, kind: str | None, name: str | None, host: str | None, username: str | None) -> Any:
    try:
        return await run_flow(ctx, lambda flow: _add_server(flow, kind, name, host, username), UNSUPPORTED_ADD)
    except ToolError as exc:
        if not popup_failed(exc, UNSUPPORTED_ADD):
            raise
    result = await add_server_start(kind, host, name, username)
    result["note"] = "The pop-up did not appear, so this continues without pop-ups. Relay the next step to the user."
    return result


# ── add_server_start / add_server_status: add a server without pop-ups ────────

STATUS_WAIT = 45.0
START_WAIT = 5.0

_adding: Relay | None = None


# Every step tells the assistant to keep polling instead of asking the user to report back.
_KEEP_POLLING = (
    "Then call add_server_status right away: it waits up to 45 seconds for the user and returns as soon as they "
    "submit. Do not ask the user to confirm in the chat."
)


async def _details_from_page(session: Relay, page: FlowPage, kind: str | None, name: str | None, username: str | None) -> tuple[Entry, str]:
    """Ask for the type, address, user name and password in one form."""
    error: str | None = None
    for _ in range(3):
        answer = page.ask_secret(
            "details",
            SecretPrompt(
                message="Add a server to your proliant-mcp inventory.",
                title="Add a server",
                label="Password",
                button="Check and save",
                note="The login is checked against the server, then saved in your proliant-mcp inventory file on this "
                "computer (readable only by you). It is not shared with the AI.",
                fields=(
                    ("type", "What is it?", "select:ilo=HPE iLO (one server)|oneview=HPE OneView / Synergy Composer", kind or "ilo", True),
                    ("host", "iLO or OneView IP address or name", "text", "", True),
                    ("name", "Name (optional), such as dl380-lab", "text", name or "", False),
                    ("username", "User name", "text", username or "Administrator", True),
                ),
            ),
            error,
        )
        session.publish(
            "enter_server",
            f"{error + ' ' if error else ''}{page.link()}The user fills in the iLO or OneView address, user name and "
            "password on that page (never in the chat); it shows the result. " + _KEEP_POLLING,
            url=page.url,
        )
        try:
            v = await asyncio.wait_for(answer, STATUS_WAIT * 6)
        except TimeoutError as exc:
            raise FlowError("No server details entered within 4 minutes") from exc
        try:
            return inventory.make_entry(v.get("name", ""), v.get("type", "ilo"), v.get("host", ""), v.get("username", "")), v["password"]
        except InventoryError as exc:
            error = f"{exc}."
    raise FlowError("Add server failed: no valid server details after three tries")


async def _add_on_page(session: Relay, draft: Entry | None, kind: str | None, name: str | None, username: str | None) -> dict[str, Any]:
    page = FlowPage("Add a server")
    passwords: list[str] = []
    try:
        if draft is None:
            draft, first = await _details_from_page(session, page, kind, name, username)
            passwords.append(first)
            session.label = draft.host
        target = draft

        async def ask(prompt: SecretPrompt, error: str | None) -> str:
            if passwords:
                return passwords.pop()
            answer = page.ask_secret("password", prompt, error)
            session.publish(
                "enter_password",
                f"{error + ' ' if error else ''}{page.link()}The user types the password for {target.username} on "
                f"{target.host} on that page (the same page, if it is open; never in the chat). {_KEEP_POLLING}",
                url=page.url,
            )
            try:
                return (await asyncio.wait_for(answer, STATUS_WAIT * 6))["password"]
            except TimeoutError as exc:
                raise FlowError("No password entered within 4 minutes") from exc

        result = await _check_and_save(target, ask)
    except BaseException as exc:
        reason = "Cancelled." if isinstance(exc, asyncio.CancelledError) else str(exc)
        page.finish(f"<p class='err'>{html.escape(reason)}</p><p>Go back to your AI chat.</p>")
        raise
    page.finish(f"<p>Saved <b>{html.escape(result['name'])}</b>: {html.escape(str(result['checked']))}.</p>"
                "<p>Go back to your AI chat; you can close this tab.</p>")
    return result


async def add_server_start(kind: str | None, host: str | None, name: str | None, username: str | None) -> dict[str, Any]:
    global _adding
    draft = None
    if host:
        try:
            draft = inventory.make_entry(name or "", kind or "ilo", host, username or "")
        except InventoryError as exc:
            raise ToolError(f"Cannot add that server: {exc}") from exc
    previous = _end_previous_add(_adding)
    session = Relay("", "Call add_server_start to try again.")
    session.label = host or ""
    session.task = asyncio.create_task(_add_on_page(session, draft, kind, name, username))
    _adding = session
    await session.wait_for_news(START_WAIT)
    result = session.current()
    if previous:
        result["previous_add"] = previous
        result["next_step"] = f"{previous['summary']} Tell the user. {result.get('next_step', '')}".strip()
    return result


def _end_previous_add(session: Relay | None) -> dict[str, Any] | None:
    """How the earlier add ended, if the assistant has not seen it yet; stop it if it still waits."""
    if session is None or session.task is None or session.reported:
        return None
    what = f"of {session.label} " if session.label else ""
    if not session.task.done():
        session.task.cancel()
        session.reported = True
        return {"status": "stopped", "host": session.label or None,
                "summary": f"The earlier add {what}was still waiting for the user and is now stopped; nothing was saved for it."}
    outcome = session.current()
    if outcome.get("status") == "saved":
        outcome["summary"] = f"The earlier add finished: {outcome['name']} ({outcome['host']}) was checked and saved."
    else:
        outcome["summary"] = f"The earlier add {what}did not save anything: {outcome.get('error', 'it failed')}."
    return outcome


async def add_server_status() -> dict[str, Any]:
    session = _adding
    if session is None or session.task is None:
        raise ToolError("No server is being added. Call add_server_start first.")
    return await session.status(STATUS_WAIT, "Call add_server_status again.")


async def import_inventory(ctx: Context) -> Any:
    return await run_flow(ctx, _import, UNSUPPORTED_IMPORT)
