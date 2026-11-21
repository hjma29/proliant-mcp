"""Local browser flow for adding multiple iLOs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import os
from typing import Any

from fastmcp.exceptions import ToolError

from proliant_mcp import bulk_inventory, inventory
from proliant_mcp.chat import FlowError, FlowPage, Relay, SecretPrompt, _page
from proliant_mcp.config import config_path, load_oneview, load_servers
from proliant_mcp.inventory import Entry, InventoryError
from proliant_mcp.probe import ProbeResult, probe_many

_session: Relay | None = None
_TIMEOUT = 900.0
_WAIT = 45.0


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _form(step: str, body: str) -> str:
    return f"<form method='post'><input type='hidden' name='step' value='{step}'>{body}</form>"


def _source_page(error: str = ""):
    def render(nonce: str) -> str:
        message = f"<p class='err'>{_escape(error)}</p>" if error else ""
        script = """
const file = document.getElementById('file'), data = document.getElementById('data');
const filename = document.getElementById('filename'), msg = document.getElementById('file-error');
const submit = document.getElementById('submit');
file.onchange = function() {
  data.value = ''; filename.value = ''; msg.textContent = '';
  if (!file.files.length) { submit.disabled = false; return; }
  const f = file.files[0];
  if (f.size > 1000000) { msg.textContent = 'File exceeds the 1 MB limit.'; submit.disabled = true; return; }
  submit.disabled = true;
  const reader = new FileReader();
  reader.onload = function() { data.value = reader.result.split(',')[1]; filename.value = f.name; submit.disabled = false; };
  reader.onerror = function() { msg.textContent = 'Could not read the file.'; };
  reader.readAsDataURL(f);
};
"""
        body = (
            f"{message}<p>Enter one iLO address per line, a name and address, an IPv4 range, or a CIDR network. "
            "Alternatively choose a CSV, INI, text, or .xlsx file (first sheet).</p>"
            "<p>Use a file without passwords. File contents stay on this computer and are not sent to the AI.</p>"
            "<label for='source'>Addresses or range</label>"
            "<textarea id='source' name='source' rows='7' style='width:100%;box-sizing:border-box' "
            "placeholder='192.0.2.10-192.0.2.20'></textarea>"
            "<label for='file'>Or choose a file (maximum 1 MB)</label>"
            "<input id='file' type='file' accept='.txt,.csv,.ini,.xlsx'>"
            "<input type='hidden' id='data' name='data'><input type='hidden' id='filename' name='filename'>"
            "<p id='file-error' class='err'></p><button id='submit' type='submit'>Preview</button>"
            f"<script nonce='{nonce}'>{script}</script>"
        )
        return _page("Add iLO servers", _form("source", body))
    return render


def _preview(entries: list[Entry], problems: list[str], note: str, existing: set[str], error: str = ""):
    def render(nonce: str) -> str:
        rows = "".join(
            "<tr>"
            f"<td><input type='checkbox' name='selected_{i}' value='1' checked aria-label='Select row {i + 1}'></td>"
            f"<td><input name='name_{i}' value='{_escape(e.name)}' aria-label='Name for row {i + 1}'></td>"
            f"<td><input name='host_{i}' value='{_escape(e.host)}' aria-label='Address for row {i + 1}'></td>"
            f"<td>{'Existing name' if e.name.lower() in existing else ''}</td></tr>"
            for i, e in enumerate(entries)
        )
        issues = "".join(f"<li>{_escape(p)}</li>" for p in problems)
        body = (
            "<style>main{width:min(60rem,90vw);margin-top:3vh}table{width:100%}td input{margin:.2rem 0}"
            "td input[type=checkbox]{width:auto}td{vertical-align:middle}</style>"
            + (f"<p class='err'>{_escape(error)}</p>" if error else "")
            + (f"<p>{_escape(note)}</p>" if note else "")
            + (f"<p>These lines were not imported:</p><ul>{issues}</ul>" if issues else "")
            + f"<p>Review and select the iLOs to check ({len(entries)} found). Nothing has been contacted or saved.</p>"
            "<table><thead><tr><th>Use</th><th>Name</th><th>iLO address</th><th>Inventory</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            "<label><input type='checkbox' name='replace' value='1' style='width:auto'> Replace matching existing names</label>"
            "<p>Entries with an address already used by another name are skipped.</p>"
            "<button type='submit'>Continue to credentials</button>"
        )
        return _page("Preview iLOs", _form("preview", body))
    return render


def _results(entries: list[Entry], results: list[ProbeResult], skipped: list[str], error: str = ""):
    def render(nonce: str) -> str:
        rows = []
        for i, (entry, result) in enumerate(zip(entries, results, strict=True)):
            status = "OK" if result.ok else ("Login rejected" if result.login_refused else "Unreachable or failed")
            detail = result.detail.replace(entry.password, "[redacted]") if entry.password else result.detail
            rows.append(
                f"<tr><td><input type='checkbox' name='save_{i}' value='1'{' checked' if result.ok else ''} "
                f"aria-label='Save {_escape(entry.name)}'></td>"
                f"<td>{_escape(entry.name)}</td><td>{_escape(entry.host)}</td><td>{_escape(status)}: {_escape(detail)}</td></tr>"
            )
        issues = "".join(f"<li>{_escape(item)}</li>" for item in skipped)
        retry = "<button type='submit' name='action' value='retry'>Retry failed logins</button> " if any(not r.ok for r in results) else ""
        body = (
            "<style>main{width:min(60rem,90vw);margin-top:3vh}table{width:100%}"
            "td input[type=checkbox]{width:auto}td{padding:.4rem;vertical-align:top}</style>"
            + (f"<p class='err'>{_escape(error)}</p>" if error else "")
            + (f"<p>Skipped existing entries:</p><ul>{issues}</ul>" if issues else "")
            + "<p>Select what to save. Successful logins are selected; failures are not. "
            "Saving a failure stores an unverified login. Passwords are stored in plaintext in your local inventory.</p>"
            "<table><thead><tr><th>Save</th><th>Name</th><th>Address</th><th>Result</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table><p>{retry}<button type='submit' name='action' value='save'>Save selected</button></p>"
        )
        return _page("Check results", _form("results", body))
    return render


async def _answer(page: FlowPage, step: str, render: Any, fields: tuple[str, ...]) -> dict[str, str]:
    try:
        return await asyncio.wait_for(page.expect(step, render, fields), _TIMEOUT)
    except TimeoutError as exc:
        raise FlowError("Timed out waiting for the browser page") from exc


def _conflicts(entries: list[Entry], replace_existing: bool) -> tuple[list[Entry], list[str]]:
    names = inventory.existing_names()
    env_targets = [
        (os.environ.get(name_var, default).lower(), host)
        for host_var, name_var, default in (("ILO_HOST", "ILO_NAME", "default"), ("ONEVIEW_HOST", "ONEVIEW_NAME", "oneview"))
        if (host := os.environ.get(host_var))
    ]
    env_names = {name for name, _ in env_targets}
    path = config_path()
    if path is not None and not path.exists():
        hosts = {host.lower(): name for name, host in env_targets}
    else:
        targets = list(load_servers().values()) + list(load_oneview().values())
        hosts = {target.host.lower(): target.name.lower() for target in targets}
    usable, skipped = [], []
    for entry in entries:
        owner = hosts.get(entry.host.lower())
        if entry.name.lower() in env_names:
            skipped.append(f"{entry.name}: name is configured by environment variables")
        elif owner and owner != entry.name.lower():
            skipped.append(f"{entry.name}: address already belongs to {owner}")
        elif entry.name.lower() in names and not replace_existing:
            skipped.append(f"{entry.name}: name already exists")
        else:
            usable.append(entry)
    return usable, skipped


async def _credentials(page: FlowPage, error: str = "") -> tuple[str, str]:
    prompt = SecretPrompt(
        message="",
        title="Shared iLO credentials",
        label="Password",
        button="Check selected iLOs",
        note="One login attempt per iLO. The password is not shared with the AI; selected entries are saved in plaintext on this computer.",
        fields=(("username", "User name", "text", "Administrator", True),),
    )
    try:
        values = await asyncio.wait_for(page.ask_secret("credentials", prompt, error or None), _TIMEOUT)
    except TimeoutError as exc:
        raise FlowError("Timed out waiting for credentials") from exc
    return values["username"], values["password"]


async def _run(session: Relay) -> dict[str, Any]:
    from proliant_mcp import server

    page = FlowPage("Add iLO servers", max_body_bytes=4_100_000)
    session.publish("enter_servers", page.link() + "Use the page to choose the iLOs, then keep calling add_servers_status.", url=page.url)
    try:
        source_error = ""
        while True:
            values = await _answer(page, "source", _source_page(source_error), ("source", "data", "filename"))
            try:
                if values["source"].strip() and values["data"]:
                    raise InventoryError("Enter addresses or choose a file, not both")
                raw = base64.b64decode(values["data"], validate=True) if values["data"] else values["source"].encode("utf-8")
                parsed, note = bulk_inventory.parse_input(raw, values["filename"])
                break
            except (InventoryError, binascii.Error) as exc:
                source_error = str(exc)

        entries = parsed.entries
        preview_error = ""
        while True:
            fields = ("replace",) + tuple(f"{field}_{i}" for i in range(len(entries)) for field in ("selected", "name", "host"))
            values = await _answer(page, "preview", _preview(entries, parsed.problems, note, inventory.existing_names(), preview_error), fields)
            try:
                chosen = [
                    inventory.make_entry(values[f"name_{i}"], "ilo", bulk_inventory._address(values[f"host_{i}"]), verify_ssl=e.verify_ssl)
                    for i, e in enumerate(entries) if values[f"selected_{i}"]
                ]
                if not chosen:
                    raise InventoryError("Select at least one iLO")
                if len({e.name.lower() for e in chosen}) != len(chosen) or len({e.host.lower() for e in chosen}) != len(chosen):
                    raise InventoryError("Selected names and addresses must be unique")
                replace_existing = bool(values["replace"])
                chosen, skipped = _conflicts(chosen, replace_existing)
                if not chosen:
                    raise InventoryError("All selected entries already exist; select replacement or change the entries")
                break
            except InventoryError as exc:
                preview_error = str(exc)

        username, password = await _credentials(page)
        while True:
            try:
                entries = [inventory.make_entry(e.name, "ilo", e.host, username, password, e.verify_ssl) for e in chosen]
                break
            except InventoryError as exc:
                username, password = await _credentials(page, str(exc))
        page.show("<p>Checking selected iLOs. This can take a few minutes.</p>")
        session.publish("checking", "The page is checking the selected iLOs. Keep calling add_servers_status.", url=page.url)
        results = await probe_many(entries, server._transport)
        error = ""
        while True:
            session.publish("review_results", "The check results are ready on the page. Keep calling add_servers_status while the user reviews them.", url=page.url)
            values = await _answer(page, "results", _results(entries, results, skipped, error),
                                   ("action",) + tuple(f"save_{i}" for i in range(len(entries))))
            if values["action"] == "retry":
                failed = [i for i, result in enumerate(results) if not result.ok]
                if not failed:
                    error = "There are no failed logins to retry"
                    continue
                username, password = await _credentials(page, "Retrying sends another login attempt to every failed iLO.")
                try:
                    retries = [inventory.make_entry(entries[i].name, "ilo", entries[i].host, username, password, entries[i].verify_ssl) for i in failed]
                except InventoryError as exc:
                    error = str(exc)
                    continue
                page.show("<p>Retrying failed iLOs.</p>")
                retried = await probe_many(retries, server._transport)
                for i, entry, result in zip(failed, retries, retried, strict=True):
                    entries[i], results[i] = entry, result
                error = ""
                continue
            if values["action"] != "save":
                error = "Choose Save selected or Retry failed logins"
                continue
            selected = [e for i, e in enumerate(entries) if values[f"save_{i}"]]
            if not selected:
                error = "Select at least one entry to save"
                continue
            selected, new_skips = _conflicts(selected, replace_existing)
            skipped.extend(new_skips)
            if not selected:
                error = "The selected entries now conflict with existing inventory; nothing was saved"
                continue
            saved = inventory.save_entries(selected, replace_existing=replace_existing)
            unverified = [e.name for e, result in zip(entries, results, strict=True) if e in selected and not result.ok]
            page.finish(f"<p>Saved {len(saved.added)} iLOs; replaced {len(saved.replaced)}. You can return to your chat.</p>")
            return {
                "status": "saved", "added": saved.added, "replaced": saved.replaced,
                "skipped": skipped + saved.skipped, "saved_unverified": unverified,
                "checked_ok": sum(result.ok for result in results),
                "failed": [{"name": e.name, "reason": "login rejected" if r.login_refused else "unreachable or failed"}
                           for e, r in zip(entries, results, strict=True) if not r.ok],
                "saved_to": str(saved.path),
            }
    except BaseException as exc:
        reason = "Cancelled." if isinstance(exc, asyncio.CancelledError) else str(exc)
        page.finish(f"<p class='err'>{_escape(reason)}</p><p>Nothing was saved. Return to your chat.</p>")
        raise


async def start() -> dict[str, Any]:
    global _session
    if _session is not None and _session.task is not None and not _session.task.done():
        raise ToolError("A bulk add is already in progress; use add_servers_status.")
    previous = _session.current() if _session is not None and _session.task is not None and not _session.reported else None
    session = Relay("Bulk add failed: ", "Call add_servers_start to try again.")
    session.task = asyncio.create_task(_run(session))
    _session = session
    await session.wait_for_news(5.0)
    result = session.current()
    if previous:
        result["previous_add"] = previous
        result["next_step"] = "Report the previous bulk-add result before starting the new page. " + result.get("next_step", "")
    return result


async def status() -> dict[str, Any]:
    if _session is None or _session.task is None:
        raise ToolError("No bulk add is in progress. Call add_servers_start first.")
    return await _session.status(_WAIT, "Call add_servers_status again.")
