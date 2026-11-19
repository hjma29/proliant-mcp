"""``add_server`` and ``import_inventory``: build the inventory from the AI chat with pop-ups.

Neither tool takes a file path or a password as an argument, so the AI cannot choose which
file is read or see a password: the user types the path in a pop-up and the password on the
secure local page (see ``chat.py``). Results never contain passwords.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Context
from fastmcp.exceptions import ToolError

from proliant_mcp import inventory
from proliant_mcp.chat import FlowError, Prompter, Relay, SecretPrompt, _Flow, run_flow
from proliant_mcp.inventory import Entry, InventoryError
from proliant_mcp.probe import LOGIN_ERRORS, PROBE_ERRORS, probe, probe_many

CHECK_LIMIT = 30
LIST_LIMIT = 50

UNSUPPORTED_ADD = (
    "This AI client cannot show pop-ups (MCP elicitation). Add the server here instead: ask the user for the "
    "type (ilo or oneview), address and user name, call add_server_start, relay the password page link it "
    "returns, and call add_server_status until it says saved."
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
    except FlowError as exc:
        raise FlowError(f"{exc}. If no pop-up was shown, use add_server_start instead (no pop-ups).") from exc
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


async def _check_and_save(draft: Entry, ask: Callable[[SecretPrompt, str | None], Awaitable[str]]) -> dict[str, Any]:
    """Ask for the password (up to three tries), log in with it, and only then save the entry."""
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
    return await run_flow(ctx, lambda flow: _add_server(flow, kind, name, host, username), UNSUPPORTED_ADD)


# ── add_server_start / add_server_status: add a server without pop-ups ────────

STATUS_WAIT = 45.0
START_WAIT = 5.0

_adding: Relay | None = None


async def add_server_start(kind: str, host: str, name: str | None, username: str | None) -> dict[str, Any]:
    global _adding
    try:
        draft = inventory.make_entry(name or "", kind, host, username or "")
    except InventoryError as exc:
        raise ToolError(f"Cannot add that server: {exc}") from exc
    if _adding is not None and _adding.task is not None and not _adding.task.done():
        _adding.task.cancel()
    session = Relay("", "Call add_server_start to try again.")
    step = (
        "Ask the user to open {url} in a browser on the computer running this server and type the password for "
        f"{draft.username} on {draft.host} there (never in the chat). Then call add_server_status."
    )
    session.task = asyncio.create_task(_check_and_save(draft, lambda prompt, error: session.secret(prompt, error, step)))
    _adding = session
    await session.wait_for_news(START_WAIT)
    return session.current()


async def add_server_status() -> dict[str, Any]:
    session = _adding
    if session is None or session.task is None:
        raise ToolError("No server is being added. Call add_server_start first.")
    return await session.status(STATUS_WAIT, "Call add_server_status again.")


async def import_inventory(ctx: Context) -> Any:
    return await run_flow(ctx, _import, UNSUPPORTED_IMPORT)
