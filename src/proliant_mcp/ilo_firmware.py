"""iLO firmware updates from HPE's Software Delivery Repository (SDR).

- ``catalog``: the iLO packages HPE publishes for a server generation (downloads.linux.hpe.com/SDR,
  fwpp-genNN firmware packs), with each package's ``MinimumActiveVersion`` from its .json sidecar.
- ``plan_steps``: the chain of packages from the installed iLO to a target (1.21 cannot take 1.25.02
  directly, it needs 1.24.00 first).
- ``estimate``: rough minutes per phase, from HPE's numbers until this computer has measured real updates.
- ``flash``: one step on one iLO. The iLO downloads the package from HPE itself (HPE AddFromUri,
  UpdateTarget), flashes it and restarts. Only the iLO restarts: the host OS keeps running.
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from proliant_mcp.config import Server, _home_config
from proliant_mcp.redfish import RedfishClient, RedfishError

SDR_BASE = "https://downloads.linux.hpe.com/SDR/repo"
PACKS_SCANNED = 6  # newest firmware packs per generation that are searched for iLO packages
CATALOG_TTL = 3600.0

# Phases of one step, in order, with rough default seconds (measured on iLO 7 1.21 -> 1.24 -> 1.25.02).
PHASES = ("downloading", "flashing", "activating", "resetting", "verifying")
PHASE_TEXT = {
    "downloading": "the iLO downloads and checks the package from HPE",
    "flashing": "the iLO writes the new firmware (progress in percent)",
    "activating": "flash finished; the iLO prepares to restart",
    "resetting": "the iLO restarts and is unreachable (web UI, Redfish, remote console)",
    "verifying": "the iLO is back; checking it runs the new version",
}
DEFAULT_SECONDS = {"downloading": 60, "flashing": 330, "activating": 90, "resetting": 150, "verifying": 15}

POLL_SECONDS = 10.0
STEP_TIMEOUT = 2400.0  # one step: HPE allows 600 s install + 120 s delay; leave room for slow downloads
START_TIMEOUT = 420.0  # the iLO must start downloading or flashing within this after the request
RETURN_TIMEOUT = 420.0  # back online but still on the old version for this long: failed
OFFLINE_MISSES = 2  # failed polls in a row that mean the iLO is restarting
STALE_GRACE = 90.0  # an Error/Complete left from an earlier attempt is ignored this long after the request
HISTORY_KEEP = 20

_UA = {"User-Agent": "proliant-mcp"}
_catalog_cache: dict[tuple[int, str], tuple[float, list["Package"]]] = {}


class FirmwareError(RuntimeError):
    pass


def vkey(text: str | None) -> tuple[int, ...]:
    """Comparable version: "iLO 6 v1.74" -> (1, 74, 0), "1.25.02 Oct 14 2026" -> (1, 25, 2)."""
    m = re.search(r"(\d+(?:\.\d+)+)", text or "")
    if not m:
        return ()
    parts = tuple(int(p) for p in m.group(1).split("."))
    return parts + (0,) * (3 - len(parts)) if len(parts) < 3 else parts


@dataclass(frozen=True)
class Package:
    family: str  # ilo5 / ilo6 / ilo7 / ilo8
    version: str  # "1.25.02", "1.79"
    filename: str
    url: str
    pack: str  # SDR firmware pack, e.g. 2026.09.00.00
    min_active: str | None = None  # oldest installed iLO that may flash this package directly
    release_date: str | None = None
    install_seconds: int | None = None

    @property
    def key(self) -> tuple[int, ...]:
        return vkey(self.version)

    def public(self) -> dict[str, Any]:
        return {"version": self.version, "release_date": self.release_date, "requires_at_least": self.min_active,
                "package": self.filename, "url": self.url}


def ilo_family(manager_model: str | None) -> str | None:
    m = re.search(r"iLO\s*(\d+)", manager_model or "", re.I)
    return f"ilo{m.group(1)}" if m else None


def generation(system_model: str | None) -> int | None:
    m = re.search(r"Gen\s*(\d+)", system_model or "", re.I)
    return int(m.group(1)) if m else None


def _package_version(raw: str) -> str:
    return f"{raw[0]}.{raw[1:]}" if re.fullmatch(r"\d{3}", raw) else raw  # iLO 5/6 names: ilo6_179 = 1.79


async def _text(http: httpx.AsyncClient, url: str) -> str:
    resp = await http.get(url, headers=_UA)
    resp.raise_for_status()
    return resp.text


async def catalog(gen: int, family: str, transport: httpx.AsyncBaseTransport | None = None) -> list[Package]:
    """iLO packages for *family* in the newest SDR firmware packs of *gen*, oldest version first."""
    cached = _catalog_cache.get((gen, family))
    if cached and time.monotonic() - cached[0] < CATALOG_TTL:
        return cached[1]
    base = f"{SDR_BASE}/fwpp-gen{gen}/"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), transport=transport, follow_redirects=True) as http:
            packs = sorted(set(re.findall(r'href="(\d{4}\.\d{2}\.\d{2}\.\d{2})/', await _text(http, base))), reverse=True)
            if not packs:
                raise FirmwareError(f"No firmware packs found at {base}")
            listings = await asyncio.gather(*(_text(http, f"{base}{p}/") for p in packs[:PACKS_SCANNED]))
            found: dict[str, tuple[str, str]] = {}
            for pack, listing in zip(packs[:PACKS_SCANNED], listings):
                for name in re.findall(rf'href="({family}_([0-9.]+)\.fwpkg)"', listing):
                    found.setdefault(name[0], (pack, name[1]))  # keep the newest pack that has it

            async def sidecar(filename: str, pack: str) -> dict[str, Any]:
                try:
                    return json.loads(await _text(http, f"{base}{pack}/{filename.removesuffix('.fwpkg')}.json"))
                except (httpx.HTTPError, ValueError):
                    return {}

            metas = await asyncio.gather(*(sidecar(f, p) for f, (p, _) in found.items()))
    except httpx.HTTPError as exc:
        raise FirmwareError(f"Cannot read HPE's firmware repository {base} ({exc.__class__.__name__})") from exc
    packages = []
    for (filename, (pack, raw)), meta in zip(found.items(), metas):
        images = [img for dev in (meta.get("Devices") or {}).get("Device") or [] for img in dev.get("FirmwareImages") or []]
        packages.append(Package(
            family=family, version=_package_version(raw), filename=filename, url=f"{base}{pack}/{filename}", pack=pack,
            min_active=meta.get("MinimumActiveVersion") or None,
            release_date=((meta.get("Package") or {}).get("ReleaseDate") or "")[:10] or None,
            install_seconds=max((img.get("InstallDurationSec") or 0 for img in images), default=0) or None,
        ))
    packages.sort(key=lambda p: p.key)
    _catalog_cache[(gen, family)] = (time.monotonic(), packages)
    return packages


def plan_steps(installed: str, target: Package, packages: list[Package]) -> list[Package]:
    """Packages to flash in order to get from *installed* to *target*, honoring MinimumActiveVersion."""
    current, steps = vkey(installed), []
    while current < target.key:
        eligible = [p for p in packages if current < p.key <= target.key and (not p.min_active or vkey(p.min_active) <= current)]
        if not eligible:
            raise FirmwareError(f"{target.version} needs iLO {target.min_active} or newer first, and HPE's repository has no "
                                f"package that gets there from {installed}")
        step = max(eligible, key=lambda p: p.key)
        steps.append(step)
        current = step.key
    return steps


# ── Estimates ────────────────────────────────────────────────────────────────

def _history_path():
    return _home_config("proliant-mcp", "firmware-history.json")


def _history() -> list[dict[str, Any]]:
    try:
        data = json.loads(_history_path().read_text())
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def record(family: str, version: str, phases: dict[str, float]) -> None:
    """Remember how long each phase took, so later estimates use this lab's real numbers."""
    entries = [*_history(), {"family": family, "version": version, "phases": {k: round(v) for k, v in phases.items()},
                             "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}]
    path = _history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries[-HISTORY_KEEP:], indent=1))
    except OSError:
        pass


def phase_seconds(family: str) -> tuple[dict[str, int], str]:
    """Seconds per phase: the median of this computer's past updates of *family*, else the defaults."""
    past = [e["phases"] for e in _history() if e.get("family") == family and isinstance(e.get("phases"), dict)]
    if not past:
        return dict(DEFAULT_SECONDS), "typical times (no update measured on this computer yet)"
    out = {}
    for phase in PHASES:
        values = [p[phase] for p in past if isinstance(p.get(phase), (int, float))]
        out[phase] = int(statistics.median(values)) if values else DEFAULT_SECONDS[phase]
    return out, f"median of {len(past)} update step(s) measured on this computer"


def minutes(seconds: float) -> int:
    return max(1, round(seconds / 60))


def step_estimate(family: str) -> dict[str, Any]:
    secs, basis = phase_seconds(family)
    return {"minutes_per_step": minutes(sum(secs.values())),
            "phases": [{"phase": p, "about_minutes": round(secs[p] / 60, 1), "what": PHASE_TEXT[p]} for p in PHASES],
            "basis": basis}


def total_seconds(families_steps: list[tuple[str, int]], mode: str) -> float:
    """Whole run: sequential adds every server's steps, parallel takes the longest server."""
    per_server = [sum(phase_seconds(fam)[0].values()) * steps for fam, steps in families_steps]
    if not per_server:
        return 0.0
    return max(per_server) if mode == "parallel" else sum(per_server)


# ── Flashing ─────────────────────────────────────────────────────────────────

@dataclass
class Progress:
    """Live state of one server's update, read by the status tool and the page."""

    server: str
    family: str
    installed: str
    target: str
    steps: list[Package]
    status: str = "pending"  # pending, running, done, failed, skipped
    step: int = 0  # 1-based while running
    phase: str = ""
    flash_percent: int | None = None
    ilo_state: str | None = None
    error: str | None = None
    started: float | None = None
    phase_started: float | None = None
    finished: float | None = None
    phase_times: dict[str, float] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    def enter(self, phase: str, note: str = "") -> bool:
        if phase == self.phase:
            return False
        now = time.monotonic()
        if self.phase and self.phase_started is not None:
            self.phase_times[self.phase] = self.phase_times.get(self.phase, 0.0) + now - self.phase_started
        self.phase, self.phase_started = phase, now
        self.log.append(f"{time.strftime('%H:%M:%S')} step {self.step}/{len(self.steps)} {phase}{': ' + note if note else ''}")
        del self.log[:-30]
        return True

    def eta_seconds(self) -> float | None:
        if self.status not in ("pending", "running"):
            return 0.0 if self.status == "done" else None
        secs = phase_seconds(self.family)[0]
        per_step = sum(secs.values())
        if self.status == "pending":
            return per_step * len(self.steps)
        idx = PHASES.index(self.phase) if self.phase in PHASES else 0
        in_phase = time.monotonic() - (self.phase_started or time.monotonic())
        left = max(0.0, secs.get(self.phase, 0) - in_phase) + sum(secs[p] for p in PHASES[idx + 1:])
        if self.phase == "flashing" and self.flash_percent:
            left = secs["flashing"] * (100 - self.flash_percent) / 100 + sum(secs[p] for p in PHASES[idx + 1:])
        return left + per_step * (len(self.steps) - self.step)

    def public(self) -> dict[str, Any]:
        now = time.monotonic()
        eta = self.eta_seconds()
        return {
            "server": self.server, "status": self.status, "from": self.installed, "to": self.target,
            "path": [p.version for p in self.steps],
            **({"step": f"{self.step}/{len(self.steps)}", "phase": self.phase, "phase_means": PHASE_TEXT.get(self.phase)} if self.status == "running" else {}),
            **({"flash_percent": self.flash_percent} if self.phase == "flashing" and self.flash_percent is not None else {}),
            **({"elapsed_minutes": round(((self.finished or now) - self.started) / 60, 1)} if self.started else {}),
            **({"about_minutes_left": round(eta / 60, 1)} if eta is not None and self.status in ("pending", "running") else {}),
            **({"error": self.error} if self.error else {}),
            "recent": self.log[-6:],
        }


def _hpe(resource: dict[str, Any]) -> dict[str, Any]:
    return (resource.get("Oem") or {}).get("Hpe") or {}


async def _update_service(c: RedfishClient) -> tuple[str, dict[str, Any]]:
    uri = ((await c.root()).get("UpdateService") or {}).get("@odata.id") or "/redfish/v1/UpdateService/"
    return uri, await c.get(uri)


async def read_ilo(server: Server, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """iLO model, firmware and server generation, for planning."""
    async with RedfishClient(server, transport=transport) as c:
        manager, system = await c.manager(), await c.system()
        _, svc = await _update_service(c)
    return {"ilo_model": manager.get("Model"), "installed": manager.get("FirmwareVersion") or "",
            "system_model": system.get("Model"), "update_state": _hpe(svc).get("State")}


BUSY_STATES = {"Uploading", "Verifying", "Writing", "Updating"}


@dataclass
class Option:
    target: Package
    steps: list[Package]
    error: str | None = None

    def public(self, family: str) -> dict[str, Any]:
        per_step = sum(phase_seconds(family)[0].values())
        return {**self.target.public(), "steps": [p.version for p in self.steps], "about_minutes": minutes(per_step * len(self.steps)),
                **({"error": self.error} if self.error else {})}


@dataclass
class Survey:
    server: str
    installed: str = ""
    ilo_model: str | None = None
    family: str | None = None
    gen: int | None = None
    options: list[Option] = field(default_factory=list)  # newer versions, newest first
    error: str | None = None
    busy: str | None = None

    @property
    def usable(self) -> list[Option]:
        return [o for o in self.options if not o.error]

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"server": self.server, "ilo_model": self.ilo_model, "installed": self.installed}
        if self.error:
            return {**out, "error": self.error}
        out["up_to_date"] = not self.options
        if self.options:
            out["latest"] = self.options[0].target.version
            out["available"] = [o.public(self.family or "") for o in self.options]
        if self.busy:
            out["note"] = f"an update is already running on this iLO (State {self.busy})"
        return out


async def survey(servers: list[Server], transport: httpx.AsyncBaseTransport | None = None) -> list[Survey]:
    """Installed iLO firmware and the newer versions HPE publishes, with the steps each one needs."""
    gate = asyncio.Semaphore(8)

    async def one(server: Server) -> Survey:
        s = Survey(server.name)
        try:
            async with gate:
                facts = await read_ilo(server, transport)
            s.installed, s.ilo_model = facts["installed"], facts["ilo_model"]
            s.family, s.gen = ilo_family(facts["ilo_model"]), generation(facts["system_model"])
            s.busy = facts["update_state"] if facts["update_state"] in BUSY_STATES else None
            if not s.family or not s.gen:
                raise FirmwareError(f"cannot tell the iLO family or server generation ({facts['ilo_model']}, {facts['system_model']})")
            packages = await catalog(s.gen, s.family, transport)
            for pkg in sorted((p for p in packages if p.key > vkey(s.installed)), key=lambda p: p.key, reverse=True):
                try:
                    s.options.append(Option(pkg, plan_steps(s.installed, pkg, packages)))
                except FirmwareError as exc:
                    s.options.append(Option(pkg, [], str(exc)))
        except (RedfishError, FirmwareError) as exc:
            s.error = str(exc)
        return s

    return list(await asyncio.gather(*(one(s) for s in servers)))


async def flash(server: Server, pkg: Package, progress: Progress, on_change: Callable[[], None],
                transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Flash *pkg* on *server* and wait until the iLO is back on that version."""
    progress.enter("downloading", pkg.filename)
    on_change()
    async with RedfishClient(server, transport=transport) as c:
        svc_uri, svc = await _update_service(c)
        state = before = _hpe(svc).get("State")
        if state in BUSY_STATES:
            raise FirmwareError(f"another firmware update is already running on this iLO (State {state}); wait for it to finish")
        action = ((_hpe(svc).get("Actions") or {}).get("#HpeiLOUpdateServiceExt.AddFromUri") or {}).get("target")
        if not action:
            raise FirmwareError("this iLO has no AddFromUri action, so it cannot fetch firmware from a URL")
        await c.post(action, {"ImageURI": pkg.url, "UpdateRepository": True, "UpdateTarget": True, "TPMOverrideFlag": True})

    loop = asyncio.get_running_loop()
    started = loop.time()
    started_update = back_since = None
    went_offline = False
    misses = 0
    client: RedfishClient | None = None

    async def unreachable() -> None:
        """The iLO stopped answering. After the update began, OFFLINE_MISSES failed polls in a row are its
        restart; a single failure is a network blip."""
        nonlocal client, went_offline, misses, back_since
        if client is not None:
            await client.__aexit__(None, None, None)
            client = None
        misses += 1
        if misses >= OFFLINE_MISSES and (started_update is not None or progress.phase != "downloading"):
            went_offline, back_since = True, None
            if progress.enter("resetting"):
                on_change()
        await asyncio.sleep(POLL_SECONDS)

    try:
        while True:
            now = loop.time()
            if now - started > STEP_TIMEOUT:
                raise FirmwareError(f"no result after {minutes(STEP_TIMEOUT)} minutes (last phase {progress.phase})")
            try:
                if client is None:
                    client = await RedfishClient(server, transport=transport).__aenter__()
                svc = await client.get(svc_uri)
            except RedfishError:
                await unreachable()
                continue

            misses = 0
            hpe = _hpe(svc)
            state, percent = hpe.get("State"), hpe.get("FlashProgressPercent")
            if state == before and state in ("Error", "Rejected", "Complete") and started_update is None and now - started < STALE_GRACE:
                state = "Idle"  # still the result of an earlier attempt, not of this request
            if state in BUSY_STATES:
                went_offline, back_since = False, None  # still working: an earlier failed poll was not the restart
            changed = state != progress.ilo_state
            progress.ilo_state = state
            if state in ("Error", "Rejected"):
                result = (hpe.get("Result") or {}).get("MessageId") or "no detail"
                raise FirmwareError(f"the iLO reported {state} ({result})")
            if state in ("Uploading", "Verifying", "Writing"):
                started_update = started_update or now
                changed |= progress.enter("downloading", state)
            elif state == "Updating":
                started_update = started_update or now
                changed |= progress.enter("flashing")
                if isinstance(percent, int) and percent != progress.flash_percent:
                    progress.flash_percent, changed = percent, True
            elif state == "Complete" or (started_update is not None and progress.phase == "flashing"):
                changed |= progress.enter("activating")
            if changed:
                on_change()

            try:
                version = (await client.manager()).get("FirmwareVersion") or ""
            except RedfishError:
                await unreachable()
                continue
            if vkey(version) == pkg.key:
                progress.enter("verifying", version)
                progress.installed = version
                on_change()
                return
            if went_offline and state not in BUSY_STATES:
                back_since = back_since or now
                if now - back_since > RETURN_TIMEOUT:
                    raise FirmwareError(f"the iLO restarted but still runs {version}, not {pkg.version}")
            if started_update is None and not went_offline and now - started > START_TIMEOUT:
                raise FirmwareError(
                    f"the iLO did not start the update within {minutes(START_TIMEOUT)} minutes (State {state}). It downloads the "
                    f"package itself, so check that it can reach {pkg.url.split('/SDR/')[0]} (DNS, proxy, firewall)"
                )
            await asyncio.sleep(POLL_SECONDS)
    finally:
        if client is not None:
            await client.__aexit__(None, None, None)
