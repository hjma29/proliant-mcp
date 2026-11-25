"""Update iLO firmware from the AI chat: the user picks and confirms on a local page.

``ilo_firmware_update_start`` opens a page that lists every configured iLO with the newer versions
HPE publishes. The user ticks the servers, picks a version for each, chooses sequential or parallel,
reviews the plan with time estimates and clicks Start. The AI never picks or confirms: nothing is
flashed unless the user clicked Start on the page. The same page then shows live progress.

The update runs as a background task inside this proliant-mcp process (an iLO step takes about ten
minutes), so the assistant, or a sub-agent it starts, follows it with ``ilo_firmware_update_status``.
Progress is also written to ``~/.config/proliant-mcp/firmware-update.json`` so another proliant-mcp
process (e.g. a sub-agent's own MCP connection) can report it.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import os
import time
from typing import Any

import httpx
from fastmcp.exceptions import ToolError

from proliant_mcp import ilo_firmware as fw
from proliant_mcp.chat import FlowError, FlowPage, Relay, _page
from proliant_mcp.config import Server, _home_config

STATUS_TOOL = "ilo_firmware_update_status"
TITLE = "Update iLO firmware"
_TIMEOUT = 900.0  # for the user to pick and confirm on the page
_WAIT = 40.0
_START_WAIT = 40.0
_HEARTBEAT = 15.0
_STALE = 90.0
_POLL = (f"Then call {STATUS_TOOL} right away: it waits up to 40 seconds and returns as soon as something changes. "
         "Do not ask the user to confirm in the chat.")

_STYLE = ("<style>main{width:min(70rem,94vw);margin-top:3vh}table{width:100%;border-collapse:collapse;margin:.6rem 0}"
          "td,th{padding:.4rem;text-align:left;vertical-align:top;border-bottom:1px solid #e3e5e8}"
          "td input[type=checkbox]{width:auto;margin:.2rem}td select{margin:0;padding:.3rem}"
          ".muted{color:#6b7785}.warn{color:#c8102e}.ok{color:#01a982}button+button{margin-left:.6rem}"
          ".sec{background:#fff;color:#425563;border:1px solid #ccd}.bar{background:#e3e5e8;border-radius:4px;height:.6rem;width:9rem}"
          ".bar i{display:block;background:#01a982;height:100%;border-radius:4px}ul.log{font-size:.85rem;color:#425563}"
          ".choice{max-width:40rem}</style>")


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _progress_path():
    return _home_config("proliant-mcp", "firmware-update.json")


class Run:
    """One pick-confirm-update session."""

    def __init__(self) -> None:
        self.relay = Relay("iLO firmware update failed: ", "Call ilo_firmware_update_start to try again.")
        self.page: FlowPage | None = None
        self.mode = "sequential"
        self.progress: list[fw.Progress] = []
        self.started: float | None = None
        self.started_at: str | None = None
        self.finished = False
        self.version = 0
        self.seen = 0
        self.changed = asyncio.Event()

    @property
    def updating(self) -> bool:
        return bool(self.progress) and not self.finished

    def touch(self) -> None:
        self.version += 1
        self.changed.set()
        if self.page is not None and self.progress:
            body = _progress_html(self)
            self.page.show(body, refresh=not self.finished)
        self.save()

    def snapshot(self) -> dict[str, Any]:
        etas = [p.eta_seconds() for p in self.progress if p.status in ("pending", "running")]
        etas = [e for e in etas if e is not None]
        left = (max(etas) if self.mode == "parallel" else sum(etas)) if etas else 0.0
        statuses = {p.status for p in self.progress}
        if not self.finished:
            status = "updating"
        elif statuses == {"done"}:
            status = "done"
        elif "done" in statuses:
            status = "partly_done"
        else:
            status = "failed"
        out: dict[str, Any] = {
            "status": status, "mode": self.mode, "started_at": self.started_at,
            "elapsed_minutes": round((time.monotonic() - (self.started or time.monotonic())) / 60, 1),
            **({"about_minutes_left": round(left / 60, 1)} if not self.finished else {}),
            "servers": [p.public() for p in self.progress],
        }
        if self.page is not None:
            out["url"] = self.page.url
        return out

    def save(self) -> None:
        data = {**self.snapshot(), "pid": os.getpid(), "saved": time.time()}
        path = _progress_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(path)
        except OSError:
            pass

    async def wait_for_news(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while self.version == self.seen and not self.finished:
            self.changed.clear()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except TimeoutError:
                return


_run: Run | None = None


# ── Pages ────────────────────────────────────────────────────────────────────

def _option_label(o: fw.Option, family: str) -> str:
    n = len(o.steps)
    per_step = sum(fw.phase_seconds(family)[0].values())
    via = f" via {', '.join(p.version for p in o.steps[:-1])}" if n > 1 else ""
    date = f", released {o.target.release_date}" if o.target.release_date else ""
    return f"{o.target.version} ({n} step{'s' if n != 1 else ''}{via}, about {fw.minutes(per_step * n)} min{date})"


def _phase_table(family: str) -> str:
    secs, basis = fw.phase_seconds(family)
    rows = "".join(f"<tr><td>{_e(p)}</td><td>about {secs[p] / 60:.1f} min</td><td>{_e(fw.PHASE_TEXT[p])}</td></tr>" for p in fw.PHASES)
    return (f"<table><thead><tr><th>Phase (each step)</th><th>Time</th><th>What happens</th></tr></thead><tbody>{rows}</tbody></table>"
            f"<p class='muted'>Times: {_e(basis)}.</p>")


def _select_page(surveys: list[fw.Survey], ticked: set[int], chosen: dict[int, str], mode: str, error: str = ""):
    def render(nonce: str) -> str:
        rows = []
        for i, s in enumerate(surveys):
            name = f"<b>{_e(s.server)}</b>"
            if s.error:
                rows.append(f"<tr><td></td><td>{name}</td><td colspan='3' class='warn'>{_e(s.error)}</td></tr>")
                continue
            current = f"{_e(s.ilo_model or '')} {_e(s.installed)}"
            if s.busy:
                rows.append(f"<tr><td></td><td>{name}</td><td>{current}</td><td colspan='2' class='warn'>An update is already running "
                            f"on this iLO ({_e(s.busy)}).</td></tr>")
                continue
            if not s.usable:
                note = "Up to date" if not s.options else _e(s.options[0].error or "")
                rows.append(f"<tr><td></td><td>{name}</td><td>{current}</td><td colspan='2' class='ok'>{note}</td></tr>")
                continue
            pick = chosen.get(i) or s.usable[0].target.version
            opts = "".join(f"<option value='{_e(o.target.version)}'{' selected' if o.target.version == pick else ''}>"
                           f"{_e(_option_label(o, s.family or ''))}</option>" for o in s.usable)
            rows.append(f"<tr><td><input type='checkbox' name='sel_{i}' value='1'{' checked' if i in ticked else ''} "
                        f"aria-label='Update {_e(s.server)}'></td><td>{name}</td><td>{current}</td>"
                        f"<td><select name='ver_{i}' aria-label='Version for {_e(s.server)}'>{opts}</select></td></tr>")
        modes = "".join(
            f"<label class='choice'><input type='radio' name='mode' value='{v}'{' checked' if v == mode else ''}>{t}</label>"
            for v, t in (("sequential", "<b>Sequential</b>: one server at a time; stops if one fails. Total = the sum of all servers."),
                         ("parallel", ("<b>Parallel</b>: all at once; total = the longest server, but all chosen iLOs are "
                                       "unreachable at about the same time."))))
        family = next((s.family for s in surveys if s.family), "ilo7")
        body = (
            _STYLE + (f"<p class='err'>{_e(error)}</p>" if error else "")
            + "<p>Tick the servers to update and pick the iLO firmware version for each. Versions come from HPE's firmware "
            "repository (downloads.linux.hpe.com). Some versions need an older one first, so they take several steps. "
            "Nothing is changed until you confirm on the next page.</p>"
            "<table><thead><tr><th>Update</th><th>Server</th><th>Installed iLO</th><th>New version</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table><p><b>Order</b></p>{modes}{_phase_table(family)}"
            "<p><button type='submit' name='action' value='review'>Review plan</button>"
            "<button type='submit' name='action' value='cancel' class='sec' formnovalidate>Update nothing</button></p>"
        )
        return _page(TITLE, f"<form method='post'><input type='hidden' name='step' value='select'>{body}</form>")
    return render


def _plan(chosen: list[tuple[fw.Survey, fw.Option]], mode: str) -> dict[str, Any]:
    total = fw.total_seconds([(s.family or "", len(o.steps)) for s, o in chosen], mode)
    reset = max((fw.phase_seconds(s.family or "")[0]["resetting"] for s, _ in chosen), default=0)
    return {
        "mode": mode, "about_minutes_total": fw.minutes(total),
        "servers": [{"server": s.server, "from": s.installed, "to": o.target.version, "steps": [p.version for p in o.steps],
                     "about_minutes": o.public(s.family or "")["about_minutes"]} for s, o in chosen],
        "per_step": fw.step_estimate(chosen[0][0].family or "") if chosen else None,
        "impact": f"Only the iLO restarts, once per step (unreachable about {fw.minutes(reset)} min each time: web UI, Redfish, "
                  "remote console). The host OS keeps running and is not rebooted.",
    }


def _confirm_page(chosen: list[tuple[fw.Survey, fw.Option]], mode: str):
    plan = _plan(chosen, mode)

    def render(nonce: str) -> str:
        rows = "".join(f"<tr><td><b>{_e(s.server)}</b></td><td>{_e(s.installed)}</td>"
                       f"<td>{_e(' → '.join(p.version for p in o.steps))}</td><td>about {o.public(s.family or '')['about_minutes']} min</td></tr>"
                       for s, o in chosen)
        body = (
            _STYLE
            + f"<p><b>Update the iLO firmware of {len(chosen)} server{'s' if len(chosen) != 1 else ''}, {_e(mode)}: about "
            f"{plan['about_minutes_total']} minutes in total.</b></p>"
            "<table><thead><tr><th>Server</th><th>Installed</th><th>Steps</th><th>Time</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>{_phase_table(chosen[0][0].family or '')}"
            f"<p>{_e(plan['impact'])} Each iLO downloads the package from downloads.linux.hpe.com itself, so it needs internet "
            "access (or a proxy set on the iLO). Do not reset the iLO or remove power while it updates.</p>"
            "<p><button type='submit' name='action' value='start'>Start update</button>"
            "<button type='submit' name='action' value='back' class='sec'>Back</button></p>"
        )
        return _page("Confirm iLO firmware update", f"<form method='post'><input type='hidden' name='step' value='confirm'>{body}</form>")
    return render


def _progress_html(run: Run) -> str:
    snap = run.snapshot()
    rows = []
    for p in run.progress:
        d = p.public()
        bar = ""
        if d.get("flash_percent") is not None:
            bar = f"<div class='bar'><i style='width:{int(d['flash_percent'])}%'></i></div>{d['flash_percent']}%"
        state = {"done": "<span class='ok'>done</span>", "failed": f"<span class='warn'>failed: {_e(p.error)}</span>",
                 "skipped": f"<span class='muted'>skipped: {_e(p.error)}</span>", "pending": "<span class='muted'>waiting</span>"}
        what = state.get(p.status) or f"step {_e(d.get('step'))}: <b>{_e(p.phase)}</b><br><span class='muted'>{_e(fw.PHASE_TEXT.get(p.phase, ''))}</span>"
        left = f"about {d['about_minutes_left']} min" if "about_minutes_left" in d else ""
        rows.append(f"<tr><td><b>{_e(p.server)}</b></td><td>{_e(p.installed)}</td><td>{_e(' → '.join(d['path']))}</td>"
                    f"<td>{what}{bar}</td><td>{_e(d.get('elapsed_minutes', ''))} min</td><td>{left}</td></tr>")
    log = "".join(f"<li>{_e(p.server)} {_e(line)}</li>" for p in run.progress for line in p.log[-4:])
    head = (f"<p><b>Updating</b> ({_e(run.mode)}), about {snap.get('about_minutes_left', 0)} minutes left. You can close this page: "
            "the update continues, and your AI chat reports progress.</p>") if not run.finished else (
            f"<p><b>Finished: {_e(snap['status'])}</b> after {snap['elapsed_minutes']} minutes. Go back to your AI chat.</p>")
    return (_STYLE + head + "<table><thead><tr><th>Server</th><th>iLO now</th><th>Steps</th><th>Phase</th><th>Elapsed</th>"
            f"<th>Left</th></tr></thead><tbody>{''.join(rows)}</tbody></table><ul class='log'>{log}</ul>")


# ── Flow ─────────────────────────────────────────────────────────────────────

async def _answer(page: FlowPage, step: str, render: Any, fields: tuple[str, ...]) -> dict[str, str]:
    try:
        return await asyncio.wait_for(page.expect(step, render, fields), _TIMEOUT)
    except TimeoutError as exc:
        raise FlowError("Timed out waiting for the user on the page; nothing was changed") from exc


async def _execute(run: Run, servers: dict[str, Server], transport: httpx.AsyncBaseTransport | None) -> None:
    async def one(p: fw.Progress) -> None:
        p.status, p.started = "running", time.monotonic()
        run.touch()
        try:
            for i, pkg in enumerate(p.steps, 1):
                p.step, p.phase, p.flash_percent, p.phase_times = i, "", None, {}
                await fw.flash(servers[p.server], pkg, p, run.touch, transport)
                p.enter("done")
                fw.record(p.family, pkg.version, p.phase_times)
            p.status = "done"
        except Exception as exc:  # noqa: BLE001 - reported per server
            p.status, p.error = "failed", str(exc) or exc.__class__.__name__
        finally:
            p.finished = time.monotonic()
            run.touch()

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT)
            run.save()

    beat = asyncio.create_task(heartbeat())
    try:
        if run.mode == "parallel":
            await asyncio.gather(*(one(p) for p in run.progress))
        else:
            for p in run.progress:
                if any(q.status == "failed" for q in run.progress):
                    p.status, p.error = "skipped", "an earlier server failed (sequential mode stops)"
                    continue
                await one(p)
    finally:
        beat.cancel()
        run.finished = True
        run.touch()


def _estimate_text(plan: dict[str, Any]) -> str:
    per = plan["per_step"] or {}
    phases = ", ".join(f"{p['phase']} ~{p['about_minutes']} min" for p in per.get("phases", []))
    return f"about {plan['about_minutes_total']} min in total ({plan['mode']}); each step: {phases}"


async def _flow(run: Run, servers: dict[str, Server], preselect: list[str], version: str | None, mode: str | None,
                transport: httpx.AsyncBaseTransport | None) -> dict[str, Any]:
    page = FlowPage(TITLE, max_body_bytes=32768)
    run.page = page
    try:
        run.relay.publish("checking", f"{page.link()}Reading the iLOs and HPE's firmware repository. {_POLL}", url=page.url)
        page.show("<p>Reading the iLOs and HPE's firmware repository...</p>")
        surveys = await fw.survey(list(servers.values()), transport)
        ticked = {i for i, s in enumerate(surveys) if s.server in preselect and s.usable and not s.busy}
        chosen_versions = {i: version for i, s in enumerate(surveys) if version and any(o.target.version == version for o in s.usable)}
        run.mode = mode or "sequential"
        if not any(s.usable and not s.busy for s in surveys):
            page.finish(_STYLE + "<p>No iLO needs an update (or none could be read).</p><p>Go back to your AI chat.</p>")
            return {"status": "nothing_to_update", "servers": [s.public() for s in surveys],
                    "next_step": "Tell the user; nothing was changed."}
        error = ""
        while True:
            summary = [s.public() for s in surveys]
            run.relay.publish(
                "select_servers",
                f"{page.link()}On that page the user ticks the servers, picks the iLO version for each and sequential or parallel, then "
                "reviews the plan. Do not choose for them. Tell them what is available (below) and that each step takes about "
                f"{fw.step_estimate(next((s.family for s in surveys if s.family), 'ilo7'))['minutes_per_step']} minutes. {_POLL}",
                url=page.url, servers=summary,
            )
            fields = ("action", "mode", *(f"sel_{i}" for i in range(len(surveys))), *(f"ver_{i}" for i in range(len(surveys))))
            values = await _answer(page, "select", _select_page(surveys, ticked, chosen_versions, run.mode, error), fields)
            if values["action"] == "cancel":
                page.finish("<p>Nothing was changed. Go back to your AI chat.</p>")
                return {"status": "cancelled", "next_step": "The user chose to update nothing. Nothing was changed."}
            run.mode = values.get("mode") if values.get("mode") in ("sequential", "parallel") else "sequential"
            ticked = {i for i, s in enumerate(surveys) if values.get(f"sel_{i}") and s.usable and not s.busy}
            chosen_versions = {i: values.get(f"ver_{i}", "") for i in range(len(surveys))}
            chosen = []
            for i in sorted(ticked):
                option = next((o for o in surveys[i].usable if o.target.version == chosen_versions[i]), surveys[i].usable[0])
                chosen.append((surveys[i], option))
            if not chosen:
                error = "Tick at least one server."
                continue
            plan = _plan(chosen, run.mode)
            run.relay.publish(
                "confirm_update",
                f"{page.link()}The user is reviewing this plan ({_estimate_text(plan)}) and must click Start on that page. {_POLL}",
                url=page.url, plan=plan,
            )
            values = await _answer(page, "confirm", _confirm_page(chosen, run.mode), ("action",))
            if values["action"] == "start":
                break
            error = ""

        run.progress = [fw.Progress(s.server, s.family or "", s.installed, o.target.version, o.steps) for s, o in chosen]
        run.started, run.started_at = time.monotonic(), time.strftime("%Y-%m-%dT%H:%M:%S%z")
        run.relay.publish("updating", "The update started.", url=page.url)
        run.touch()
        await _execute(run, servers, transport)
        page.finish(_progress_html(run), close_after=600)
        result = run.snapshot()
        result["next_step"] = ("Tell the user the result for each server (from, to, minutes, or the error). Measured phase times "
                               "are saved, so the next estimate uses them.")
        return result
    except BaseException as exc:
        if not run.progress:
            reason = "Cancelled." if isinstance(exc, asyncio.CancelledError) else str(exc)
            page.finish(f"<p class='err'>{_e(reason)}</p><p>Nothing was changed. Return to your chat.</p>")
        raise


def _started_step(run: Run) -> dict[str, Any]:
    snap = run.snapshot()
    snap["next_step"] = (
        "The user confirmed on the page and the update is running inside proliant-mcp; it continues without this chat (about "
        f"{snap.get('about_minutes_left')} min). Tell the user the plan, the estimate and the phases (downloading, flashing with "
        "percent, activating, resetting, verifying). If you can start a background sub-agent, give it this job: 'call "
        f"{STATUS_TOOL} repeatedly until status is done, partly_done or failed; after each call report each server's step, phase, "
        "flash_percent and about_minutes_left; at the end report each server's from/to version or error'. Otherwise call "
        f"{STATUS_TOOL} yourself; each call returns when something changes or after 40 s. Do not start another update, reset these "
        "iLOs or power off these servers meanwhile."
    )
    return snap


async def start(servers: dict[str, Server], preselect: list[str] | None, version: str | None, mode: str | None,
                transport: httpx.AsyncBaseTransport | None) -> dict[str, Any]:
    global _run
    if _run is not None and _run.updating:
        snap = _run.snapshot()
        snap["next_step"] = f"An update is already running; nothing new was started. Follow it with {STATUS_TOOL}."
        return snap
    names = []
    for wanted in preselect or []:
        match = next((s.name for s in servers.values() if wanted in (s.name, s.host)), None)
        if match is None:
            raise ToolError(f"Unknown server '{wanted}'. Configured: {', '.join(sorted(servers))}")
        names.append(match)
    if _run is not None and _run.relay.task is not None and not _run.relay.task.done():
        _run.relay.task.cancel()
    run = Run()
    run.relay.task = asyncio.create_task(_flow(run, servers, names, (version or "").strip() or None, mode, transport))
    _run = run
    await run.relay.wait_for_news(_START_WAIT)
    return _started_step(run) if run.updating else run.relay.current()


def _from_file() -> dict[str, Any]:
    try:
        data = json.loads(_progress_path().read_text())
    except (OSError, ValueError) as exc:
        raise ToolError("No iLO firmware update in progress. Call ilo_firmware_update_start first.") from exc
    age = time.time() - float(data.pop("saved", 0) or 0)
    data.pop("pid", None)
    data["from_file"] = True
    if data.get("status") == "updating" and age > _STALE:
        data["status"] = "unknown"
        data["next_step"] = (f"The proliant-mcp process that ran this update stopped reporting {fw.minutes(age)} min ago (it may have "
                             "been closed). The iLO finishes a flash that already started on its own. Check each server with "
                             "get_ilo_info, or ilo_firmware_list_updates.")
    elif data.get("status") == "updating":
        data["next_step"] = f"This update runs in another proliant-mcp process; this is its latest saved progress. Call {STATUS_TOOL} again."
    return data


async def status() -> dict[str, Any]:
    run = _run
    if run is None or run.relay.task is None:
        return _from_file()
    if not run.progress:
        result = await run.relay.status(_WAIT, f"Call {STATUS_TOOL} again.")
        if not run.progress:
            return result
    if not run.finished:
        await run.wait_for_news(_WAIT)
        run.seen = run.version
    if run.finished:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(run.relay.task), 10)
        return run.relay.current() if run.relay.task.done() else run.snapshot()
    return _started_step(run)
