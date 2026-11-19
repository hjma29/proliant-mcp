"""HPE compute MCP server: read-only tools for ProLiant iLO, Compute Ops Management and OneView."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import Counter
from typing import Annotated, Any, Awaitable, Callable, Literal

import httpx
import mcp_types
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from proliant_mcp import __version__, chat_inventory, chat_login, com, ilo, inventory_cli, login, oneview
from proliant_mcp.com import ComClient, ComError
from proliant_mcp.config import ComAccount, OneViewAppliance, Server, config_path, load_com, load_oneview, load_servers
from proliant_mcp.oneview import OneViewClient, OneViewError
from proliant_mcp.redfish import RedfishClient, RedfishError
from proliant_mcp.util import redact

INSTRUCTIONS = """\
Read-only tools for HPE compute management, across three sources:
- iLO (Redfish), one server at a time: tools without a prefix (get_server_summary, ...).
- HPE Compute Ops Management (COM, GreenLake cloud), whole fleet: tools prefixed com_.
- HPE OneView / Synergy Composer, on-prem appliance: tools prefixed oneview_.
Start with list_servers: it returns the configured iLO server names, OneView appliances
and COM accounts. For a fleet-wide question start with com_get_overview or
oneview_get_appliance. The same physical server can appear in all three; match by
serial number. If a tool returns an error, report it; never invent values.
To find which server an IP, hostname or serial belongs to, call find_server (all sources at once).
For the OS a server runs or why it isn't booting, see `os` in get_server_summary / get_fleet_summary.
For significant events on several servers use get_fleet_event_summary. For Synergy uplinks use
oneview_list_uplink_sets.
For which HPE GreenLake services exist and which are set up in the workspace use greenlake_list_services.
For tables or comparisons across servers use com_list_servers (add include_hardware for CPU, GPU
and storage controller columns) or com_list_gpus; they return the whole fleet in one call. For a
table of each server's storage controllers, drives, GPUs and NICs use com_hardware_report: it is compact and
groups servers with identical hardware. For NICs, HBAs, link speeds or which network mode
(Ethernet, InfiniBand, FibreChannel) is used, use com_list_nics. Never loop com_get_server
or com_api_get over many servers.
If a com_ tool says no COM account is configured, the COM login failed (HTTP 401) or COM is not
set up for the workspace, sign the user in right away in this chat; do not send them to a
browser, terminal or the GreenLake website. Call com_login (pop-ups). If it fails or is
cancelled without the user seeing a pop-up, call com_login_start (with the email only if the user
already gave it; never ask for it with a question tool, which may be auto-answered "user not
available": without an email the user types it on a local page), tell the user exactly what it
returns (the page link, the Okta Verify number to tap), and call com_login_status until it says signed_in. If no iLO or OneView is
configured, or the user wants to add servers, call add_server (one iLO or OneView) or
import_inventory (a .ini or .csv file the user names in a pop-up). If add_server's pop-ups do not
appear, call add_server_start (with the address only if the user gave it), relay the page link, and
call add_server_status until it says saved. For who logged in to iLOs (per day, user, source IP,
failed logins) use get_ilo_logins: one call for all iLOs. Never ask the user to type
a password into the chat; these tools collect it on a secure page."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

ServerName = Annotated[str, Field(description="Server name from list_servers (or its iLO host/IP).")]

ApplianceName = Annotated[str | None, Field(description="OneView appliance name from list_servers; omit to use the first one.")]
AccountName = Annotated[str | None, Field(description="COM account name from list_servers; omit to use the first one.")]
Limit = Annotated[int, Field(ge=1, le=1000)]

mcp = FastMCP("proliant-mcp", instructions=INSTRUCTIONS, version=__version__)

LATEST_RELEASE_API = "https://api.github.com/repos/hjma29/proliant-mcp/releases/latest"

# Tests swap this to inject fake iLO / OneView / COM endpoints (httpx.MockTransport).
_transport = None


def _servers() -> dict[str, Server]:
    servers = load_servers()
    if not servers:
        raise ToolError(
            "No iLO servers configured. Add one with the add_server tool (pop-ups in the chat; add_server_start if "
            "pop-ups do not appear), import a file with import_inventory, set ILO_HOST/ILO_USERNAME/ILO_PASSWORD, or edit "
            "~/.config/proliant-mcp/inventory.ini (see examples/inventory.example.ini)."
        )
    return servers


def _resolve(name: str) -> Server:
    servers = _servers()
    if name in servers:
        return servers[name]
    for s in servers.values():
        if name == s.host:
            return s
    raise ToolError(f"Unknown server '{name}'. Configured: {', '.join(sorted(servers))}")


async def _run(name: str, fn: Callable[[RedfishClient], Awaitable[Any]]) -> Any:
    server = _resolve(name)
    try:
        async with RedfishClient(server, transport=_transport) as client:
            return await fn(client)
    except RedfishError as exc:
        raise ToolError(str(exc)) from exc


def _pick(kind: str, items: dict[str, Any], name: str | None) -> Any:
    if not items:
        hint = (
            "Add one with the add_server tool (pop-ups in the chat; add_server_start if pop-ups do not appear), import a file with import_inventory, add a section with `type = oneview` (host/username/password) to the inventory file, or set ONEVIEW_HOST/ONEVIEW_USERNAME/ONEVIEW_PASSWORD."
            if kind == "OneView appliance"
            else "Add a section with `type = com` (client_id/client_secret/region) to the inventory file, set HPECOM_CLIENT_ID/HPECOM_CLIENT_SECRET/HPECOM_REGION, or sign the user in now with com_login (pop-ups) or com_login_start (no pop-ups)."
        )
        raise ToolError(f"No {kind} configured. {hint}")
    if not name:
        return next(iter(items.values()))
    for key, item in items.items():
        if key.lower() == name.lower():
            return item
    raise ToolError(f"Unknown {kind} '{name}'. Configured: {', '.join(items)}")


async def _ov(name: str | None, fn: Callable[[OneViewClient], Awaitable[Any]]) -> Any:
    appliance: OneViewAppliance = _pick("OneView appliance", load_oneview(), name)
    try:
        async with OneViewClient(appliance, transport=_transport) as client:
            return await fn(client)
    except OneViewError as exc:
        raise ToolError(str(exc)) from exc


async def _com(name: str | None, fn: Callable[[ComClient], Awaitable[Any]]) -> Any:
    account: ComAccount = _pick("COM account", load_com(), name)
    try:
        async with ComClient(account, transport=_transport) as client:
            return await fn(client)
    except ComError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ_ONLY)
def list_servers() -> dict[str, Any]:
    """List what this server can reach: iLO servers (name, iLO host), OneView appliances
    and COM accounts (name, region). Credentials are never returned. Call this first."""
    return {
        "config_file": str(config_path()) if config_path() else None,
        "servers": [{"name": s.name, "ilo_host": s.host} for s in load_servers().values()],
        "oneview_appliances": [{"name": a.name, "host": a.host} for a in load_oneview().values()],
        "com_accounts": [
            {
                "name": a.name,
                "region": com._regions.get(a.client_id, a.region),
                **({"configured_region": a.region} if a.client_id in com._regions else {}),
                "workspace": a.workspace or None,
                "source": a.source or None,
            }
            for a in load_com().values()
        ],
    }


@mcp.tool(annotations=READ_ONLY)
async def find_server(
    query: Annotated[str, Field(description="Server name, iLO IP or hostname, serial number or OS hostname.")],
    probe: Annotated[
        bool, Field(description="If nothing matches and the query is an address, read the iLO's login-free identity pages.")
    ] = True,
) -> dict[str, Any]:
    """Which server is this? Looks one name, iLO IP/hostname, serial or OS hostname up in the iLO
    inventory, every COM account and every OneView appliance (servers and interconnect modules) in
    one call. If nothing matches and the query is an IP or hostname, it reads that iLO's
    unauthenticated identity pages to report its iLO model, firmware, hostname, server model and
    serial. Use it instead of searching com_list_servers or oneview_list_server_hardware by hand."""
    from proliant_mcp import find

    return await find.find_server(query, probe, _transport)


@mcp.tool(annotations=READ_ONLY)
async def get_ilo_info(server: ServerName) -> dict[str, Any]:
    """iLO details: model (iLO 5/6/7), firmware version and date, license, Compute Ops
    Management connection status, and the iLO's own network (hostname, IP, MAC, DHCP)."""
    return await _run(server, ilo.ilo_info)


@mcp.tool(annotations=READ_ONLY)
async def get_server_summary(server: ServerName) -> dict[str, Any]:
    """Server identity and state: model, product ID, serial, power and POST state, health,
    UID light, BIOS and iLO versions, CPU count/model, total memory, and `os`: whether an OS is
    running and which (name and version when the HPE agent reports it), boot progress (e.g. stuck
    in POST or BIOS setup) and the UEFI boot order."""
    return await _run(server, ilo.server_summary)


@mcp.tool(annotations=READ_ONLY)
async def get_fleet_summary() -> list[dict[str, Any]]:
    """get_server_summary (including the OS and boot state) for every configured server at once.
    Unreachable servers are returned with an `error` field instead of failing the whole call."""

    async def one(s: Server) -> dict[str, Any]:
        try:
            async with RedfishClient(s, transport=_transport) as client:
                return {"server": s.name, **await ilo.server_summary(client)}
        except RedfishError as exc:
            return {"server": s.name, "error": str(exc)}

    return list(await asyncio.gather(*(one(s) for s in _servers().values())))


@mcp.tool(annotations=READ_ONLY)
async def get_health(server: ServerName) -> dict[str, Any]:
    """Overall and per-subsystem health (processors, memory, storage, network, fans,
    power supplies, temperatures) plus fan / power-supply redundancy."""
    return await _run(server, ilo.health)


@mcp.tool(annotations=READ_ONLY)
async def get_firmware_inventory(server: ServerName) -> list[dict[str, Any]]:
    """All firmware components and versions (iLO, BIOS/System ROM, NICs, storage
    controllers, drives, CPLD, ...)."""
    return await _run(server, ilo.firmware_inventory)


@mcp.tool(annotations=READ_ONLY)
async def get_processors(server: ServerName) -> dict[str, Any]:
    """CPUs (socket, model, cores, threads, speed) and GPUs that iLO reports."""
    return await _run(server, ilo.processors)


@mcp.tool(annotations=READ_ONLY)
async def get_memory(server: ServerName) -> dict[str, Any]:
    """Memory: total GiB, populated/empty slot counts, and each DIMM (slot, size, type,
    speed, vendor, HPE part number, health)."""
    return await _run(server, ilo.memory)


@mcp.tool(annotations=READ_ONLY)
async def get_storage(server: ServerName) -> list[dict[str, Any]]:
    """Storage subsystems: controllers (model, firmware), physical drives (model, media,
    capacity, serial, firmware, bay) and logical volumes / RAID."""
    return await _run(server, ilo.storage)


@mcp.tool(annotations=READ_ONLY)
async def get_network_adapters(server: ServerName) -> list[dict[str, Any]]:
    """Host network adapters: model, part number, slot, firmware, and ports (MAC, link,
    speed, LLDP neighbor switch/port when available)."""
    return await _run(server, ilo.network_adapters)


When = Annotated[
    str | None,
    Field(description="Relative (7d, 24h, 30m), a date (2026-11-12) or an ISO 8601 time; UTC unless it has a zone."),
]


LogName = Annotated[
    Literal["iml", "iel", "sl"],
    Field(description="iml = Integrated Management Log (server hardware events); iel = iLO Event Log; sl = Security Log (iLO 6 and later)."),
]
MinSeverity = Annotated[
    Literal["Warning", "Critical"] | None, Field(description="Only entries at least this severe (Warning = Warning and Critical).")
]


def _window(since: str | None, until: str | None, now: dt.datetime) -> tuple[dt.datetime | None, dt.datetime | None]:
    try:
        return (ilo.parse_when(since, now) if since else None, ilo.parse_when(until, now, end_of_day=True) if until else None)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ_ONLY)
async def get_event_log(
    server: ServerName,
    log: LogName = "iml",
    limit: Annotated[int, Field(ge=1, le=500)] = 20,
    severity: Annotated[Literal["OK", "Warning", "Critical"] | None, Field(description="Only entries of exactly this severity.")] = None,
    since: When = None,
    until: When = None,
    contains: Annotated[str | None, Field(description="Only entries whose message contains this text (any case), e.g. 'login'.")] = None,
    min_severity: MinSeverity = None,
    summary: Annotated[
        bool, Field(description="Group identical messages into one row with count and first/last time, most severe first.")
    ] = False,
) -> list[dict[str, Any]]:
    """Newest entries from the IML, iLO event log or Security Log, filtered by severity, time window
    and message text; with summary, identical messages are grouped (compact; use it for "what
    happened" or "significant events"). Entries the iLO stamped with a wrong clock (0000 or far-future
    dates after an RTC reset) are marked `time_invalid`, listed last, and left out when since/until
    is given. For several servers use get_fleet_event_summary; for who logged in use get_ilo_logins."""
    now = dt.datetime.now(dt.UTC)
    start, end = _window(since, until, now)
    return await _run(server, lambda c: ilo.event_log(c, log, limit, severity, start, end, contains, now, min_severity, summary))


@mcp.tool(annotations=READ_ONLY)
async def get_fleet_event_summary(
    servers: Annotated[list[str] | None, Field(description="Server names from list_servers; omit for every configured iLO.")] = None,
    log: LogName = "iml",
    min_severity: MinSeverity = "Warning",
    since: When = None,
    until: When = None,
    contains: Annotated[str | None, Field(description="Only entries whose message contains this text (any case).")] = None,
    limit: Annotated[int, Field(ge=1, le=200, description="Most groups per server.")] = 30,
) -> dict[str, Any]:
    """Significant events for many servers in one call: per server, the IML, iLO event log or
    Security Log grouped by message (count, first/last time, repaired), Warning and Critical by
    default, most severe first. Use it for "significant events / errors on these servers".
    Unreachable iLOs return an `error`."""
    targets = [_resolve(n) for n in servers] if servers else list(_servers().values())
    now = dt.datetime.now(dt.UTC)
    start, end = _window(since, until, now)

    async def one(s: Server) -> dict[str, Any]:
        try:
            async with RedfishClient(s, transport=_transport) as client:
                groups = await ilo.event_log(client, log, limit, None, start, end, contains, now, min_severity, True)
        except RedfishError as exc:
            return {"server": s.name, "error": str(exc)}
        by_severity: Counter[str] = Counter()
        for g in groups:
            by_severity[g["severity"] or "Unknown"] += g["count"]
        return {"server": s.name, "entries_by_severity": dict(by_severity), "groups": groups}

    return {"log": log, "min_severity": min_severity, "servers": list(await asyncio.gather(*(one(s) for s in targets)))}


@mcp.tool(annotations=READ_ONLY)
async def get_ilo_logins(
    servers: Annotated[list[str] | None, Field(description="Server names from list_servers; omit for every configured iLO.")] = None,
    days: Annotated[int, Field(ge=1, le=90, description="Look back this many days, including today.")] = 7,
    utc_offset_hours: Annotated[
        float | None, Field(ge=-12, le=14, description="Time zone for by_day, e.g. -8 for PST; omit for this computer's zone.")
    ] = None,
    include_this_computer: Annotated[
        bool, Field(description="Also count logins from the computer running proliant-mcp (its own Redfish sessions).")
    ] = False,
) -> dict[str, Any]:
    """Who logged in to the iLOs, from each iLO Event Log, in one call for many servers: logins
    per day (total and per server), by login type, user and source IP/host, failed logins and
    lockout alerts. Says when an iLO's log does not reach back to the start of the window (the log
    wrapped, so counts are a minimum) and how many entries have an invalid iLO clock. The iLO logs
    Redfish/API sessions as Browser logins, including proliant-mcp's own: those are counted only in
    `this_computer_logins` unless include_this_computer. Unreachable iLOs return an `error`."""
    configured = _servers()
    targets = [_resolve(n) for n in servers] if servers else list(configured.values())
    now = dt.datetime.now(dt.UTC)
    tz = dt.timezone(dt.timedelta(hours=utc_offset_hours)) if utc_offset_hours is not None else now.astimezone().tzinfo or dt.UTC
    today = now.astimezone(tz).date()
    first_day = today - dt.timedelta(days=days - 1)
    since = dt.datetime.combine(first_day, dt.time(), tz).astimezone(dt.UTC)

    async def one(s: Server) -> dict[str, Any]:
        try:
            async with RedfishClient(s, transport=_transport) as client:
                return {"server": s.name, **await ilo.logins(client, since, now, tz, now, include_this_computer)}
        except RedfishError as exc:
            return {"server": s.name, "error": str(exc)}

    rows = list(await asyncio.gather(*(one(s) for s in targets)))
    ok = [r for r in rows if "error" not in r]
    dates = [(first_day + dt.timedelta(days=i)).isoformat() for i in range(days)]
    by_day = [
        {"date": d, "total": sum(r["by_day"].get(d, 0) for r in ok), **{r["server"]: r["by_day"].get(d, 0) for r in ok}} for d in dates
    ]
    for r in ok:
        del r["by_day"]
    offset = tz.utcoffset(now) or dt.timedelta()
    notes = []
    short = [r["server"] for r in ok if not r["log_covers_window"]]
    if short:
        notes.append(
            f"The iLO Event Log of {', '.join(short)} starts after the window begins (see log_starts): older entries "
            "were overwritten, so those counts are a minimum."
        )
    if any(r.get("entries_with_invalid_time") for r in ok):
        notes.append("Some iLOs have entries with an invalid clock (0000 or far-future dates, after an RTC reset); they are not counted.")
    if any(src.get("this_computer") for r in ok for src in r["sources"]):
        notes.append("Sources marked this_computer include proliant-mcp's own Redfish sessions, logged by the iLO as Browser logins.")
    elif any(r["this_computer_logins"] for r in ok):
        notes.append(
            f"{sum(r['this_computer_logins'] for r in ok)} logins from this computer (proliant-mcp's own Redfish sessions) "
            "are not counted; set include_this_computer to count them."
        )
    return {
        "window": {
            "from": since.isoformat().replace("+00:00", "Z"),
            "to": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "days": days,
            "utc_offset_hours": offset.total_seconds() / 3600,
            "this_computer_included": include_this_computer,
        },
        "totals": {
            "logins": sum(r["logins"] for r in ok),
            "this_computer_logins": sum(r["this_computer_logins"] for r in ok),
            "failed_logins": sum(r["failed_logins"] for r in ok),
            "security_alerts": sum(len(r["security_alerts"]) for r in ok),
            "servers": len(ok),
            "unreachable": len(rows) - len(ok),
        },
        "by_day": by_day,
        "servers": rows,
        "notes": notes,
    }


@mcp.tool(annotations=READ_ONLY)
async def redfish_get(
    server: ServerName,
    path: Annotated[str, Field(description="Redfish URI starting with /redfish/v1, e.g. /redfish/v1/Systems/1/Bios")],
) -> dict[str, Any]:
    """Raw read-only GET of any iLO Redfish resource, for data the other tools don't cover."""
    if not path.startswith("/redfish/v1") or ".." in path:
        raise ToolError("path must start with /redfish/v1")
    if path.rstrip("/").lower().startswith("/redfish/v1/sessionservice/sessions/") or "/accountservice/accounts" in path.lower():
        raise ToolError("Session and account resources are not exposed")
    return await _run(server, lambda c: c.get(path))


# ── HPE Compute Ops Management (GreenLake) ────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
async def com_get_overview(account: AccountName = None) -> dict[str, Any]:
    """Compute Ops Management fleet report: server count by health, power, connection,
    generation and model; servers needing attention; disconnected servers; soonest
    subscription expirations; active (uncleared) alerts by severity with the newest ten."""
    return await _com(account, com.overview)


@mcp.tool(annotations=READ_ONLY)
async def com_list_servers(
    account: AccountName = None,
    health: Annotated[Literal["OK", "WARNING", "CRITICAL", "UNKNOWN"] | None, Field(description="Only servers with this health summary.")] = None,
    connected: Annotated[bool | None, Field(description="Only servers whose iLO is (or isn't) connected to COM.")] = None,
    name_contains: Annotated[str | None, Field(description="Case-insensitive substring of the server name.")] = None,
    include_hardware: Annotated[bool, Field(description="Add a `hardware` object per server: CPU model/count/cores, GPUs (name, vendor, count, health) and storage (RAID/boot controllers with the drives behind them, and direct-attached drives) from COM's cached inventory. Slower (one read per server, done inside this call); use it for tables with CPU or GPU columns instead of calling com_get_server per server.")] = False,
) -> list[dict[str, Any]]:
    """Servers in Compute Ops Management: name, serial, model, generation, health, power,
    COM connection, subscription, iLO host/IP/version, OS, CPU vendor and firmware bundle.
    One call returns the whole fleet, so use it for any table or comparison across servers.
    Set include_hardware for CPU, GPU and storage controller columns."""
    return await _com(account, lambda c: com.list_servers(c, health, connected, name_contains, include_hardware))


@mcp.tool(annotations=READ_ONLY)
async def greenlake_list_services(
    account: AccountName = None,
    provisioned_only: Annotated[bool, Field(description="Only services that are set up (provisioned) in this workspace.")] = False,
) -> dict[str, Any]:
    """Every HPE GreenLake service in the catalog and where each is provisioned in the user's
    workspace (Compute Ops Management, Data Services, Aruba Central, ...): name, provisioned or
    not, regions with status, description, plus the counts. Use it for "what services do I have in
    GreenLake" and "is X available / set up". Uses the same sign-in as the com_ tools."""
    return await _com(account, lambda c: com.list_services(c, provisioned_only))


@mcp.tool(annotations=READ_ONLY)
async def com_hardware_report(
    account: AccountName = None,
    name_contains: Annotated[str | None, Field(description="Case-insensitive substring of the server name.")] = None,
    connected: Annotated[bool | None, Field(description="Only servers whose iLO is (or isn't) connected to COM.")] = None,
    storage_over_tb: Annotated[float | None, Field(description="Only servers whose total raw drive capacity is strictly more than this many TB (decimal; 1 = 1000 GB). Adds storage_filter with how many servers are over, not over or unknown.")] = None,
) -> dict[str, Any]:
    """Whole-fleet hardware table in one compact call (about a tenth the size of com_list_servers
    with include_hardware): servers with identical hardware are grouped into one configuration
    with their names, model, CPU, storage controllers with the drives behind them, direct-attached
    drives, GPUs and network adapters (with their mode: Ethernet, InfiniBand or FibreChannel), plus
    NIC mode totals and the servers COM has no storage, GPU or NIC data for (unknown, not absent).
    Each configuration has total_storage_gb (raw capacity of all drives). Use it for "table of each
    server's storage controller, GPU and NIC config", and for "how many servers have more than N TB
    of storage" (set storage_over_tb: storage_filter gives the counts). Prefer it to scripting sums."""
    return await _com(account, lambda c: com.hardware_report(c, name_contains, connected, storage_over_tb))


@mcp.tool(annotations=READ_ONLY)
async def com_list_nics(
    account: AccountName = None,
    name_contains: Annotated[str | None, Field(description="Case-insensitive substring of the server name.")] = None,
    connected: Annotated[bool | None, Field(description="Only servers whose iLO is (or isn't) connected to COM.")] = None,
    linked_only: Annotated[bool, Field(description="List only adapters with at least one port up.")] = False,
) -> dict[str, Any]:
    """Network adapter report for the whole COM fleet in one call: per server each NIC/HBA
    (name, mode Ethernet / InfiniBand / FibreChannel, ports, ports up, link speeds, firmware,
    health), plus totals by mode (adapters, ports, linked ports, and the most used mode), by
    adapter model and by link speed, and the servers COM has no network inventory for. Start
    here for any NIC, HBA, link speed or network mode question."""
    return await _com(account, lambda c: com.list_nics(c, name_contains, connected, linked_only))


@mcp.tool(annotations=READ_ONLY)
async def com_list_gpus(
    account: AccountName = None,
    name_contains: Annotated[str | None, Field(description="Case-insensitive substring of the server name.")] = None,
    connected: Annotated[bool | None, Field(description="Only servers whose iLO is (or isn't) connected to COM.")] = None,
    include_servers_without_gpus: Annotated[bool, Field(description="Also list servers with no GPU.")] = False,
) -> dict[str, Any]:
    """GPU report for the whole COM fleet in one call: per server the model, generation, iLO
    firmware version, health, CPU and GPUs (name, vendor, part number, count, health), plus
    totals by GPU model and the servers whose inventory COM does not have (their GPUs are
    unknown, not absent). Start here for any GPU question."""
    return await _com(account, lambda c: com.list_gpus(c, name_contains, connected, include_servers_without_gpus))


@mcp.tool(annotations=READ_ONLY)
async def com_get_server(
    server: Annotated[str, Field(description="COM server name, serial number, iLO IP/hostname or id.")],
    account: AccountName = None,
) -> dict[str, Any]:
    """One COM server in detail: identity, per-subsystem health, iLO, subscription, last
    firmware update, full firmware inventory, CPU, GPUs (with serial numbers), storage
    controllers and drives (with serial numbers), network adapters (each port's mode, link,
    speed and MAC), and its
    active alerts. For several servers use com_list_servers, com_list_gpus or com_list_nics, not one call each."""
    return await _com(account, lambda c: com.get_server(c, server))


@mcp.tool(annotations=READ_ONLY)
async def com_list_alerts(
    account: AccountName = None,
    server: Annotated[str | None, Field(description="Only alerts for this server (name, serial, iLO IP or id).")] = None,
    severity: Annotated[Literal["CRITICAL", "WARNING", "OK"] | None, Field(description="Only this severity.")] = None,
    include_cleared: Annotated[bool, Field(description="Also return alerts that have cleared.")] = False,
    limit: Limit = 50,
) -> list[dict[str, Any]]:
    """Newest COM alerts (hardware, security, service events), uncleared only by default."""
    return await _com(account, lambda c: com.list_alerts(c, server, severity, include_cleared, limit))


@mcp.tool(annotations=READ_ONLY)
async def com_list_jobs(
    account: AccountName = None,
    state: Annotated[Literal["RUNNING", "PENDING", "COMPLETE", "ERROR", "STALLED"] | None, Field(description="Only jobs in this state.")] = None,
    limit: Limit = 25,
) -> list[dict[str, Any]]:
    """Newest COM jobs (firmware updates, inventory collection, power, OneView sync, ...)
    with state and result."""
    return await _com(account, lambda c: com.list_jobs(c, limit, state))


@mcp.tool(annotations=READ_ONLY)
async def com_list_groups(account: AccountName = None) -> list[dict[str, Any]]:
    """COM server groups: members (serials), compliance (firmware, iLO settings) and
    whether firmware is updated automatically when a server is added."""
    return await _com(account, com.list_groups)


@mcp.tool(annotations=READ_ONLY)
async def com_list_firmware_bundles(
    account: AccountName = None,
    include_inactive: Annotated[bool, Field(description="Also list superseded bundles.")] = False,
) -> list[dict[str, Any]]:
    """Firmware bundles (SPPs and patches) available in COM, newest first."""
    return await _com(account, lambda c: com.list_bundles(c, include_inactive))


@mcp.tool(annotations=READ_ONLY)
async def com_api_get(
    path: Annotated[str, Field(description="COM API path starting with /compute-ops-mgmt/, e.g. /compute-ops-mgmt/v1/servers/{id}/inventory")],
    account: AccountName = None,
) -> dict[str, Any]:
    """Raw read-only GET of any Compute Ops Management API resource, for data the other
    com_ tools don't cover (server inventory, settings, appliances, reports). Secrets are redacted."""
    if not path.startswith("/compute-ops-mgmt/") or ".." in path:
        raise ToolError("path must start with /compute-ops-mgmt/")
    return redact(await _com(account, lambda c: c.get(path)))


@mcp.tool(
    annotations=ToolAnnotations(
        title="Sign in to HPE GreenLake", readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    )
)
async def com_login(
    ctx: Context,
    email: Annotated[str | None, Field(description="Pre-fills the email pop-up; the user confirms or changes it.")] = None,
    workspace: Annotated[str | None, Field(description="GreenLake workspace name; asked in a pop-up if omitted and the user has several.")] = None,
    region: Annotated[Literal["us-west", "eu-central", "ap-northeast"] | None, Field(description="COM region; detected if omitted.")] = None,
) -> dict[str, Any] | mcp_types.InputRequiredResult:
    """Sign the user in to HPE GreenLake so the com_ tools work. Shows pop-ups in the chat:
    the user's email, then for @hpe.com accounts the number to tap in Okta Verify, or for
    other accounts (e.g. gmail.com) a secure local page with a masked password field. It then
    creates a GreenLake API client for the read-only com_ tools and saves it on this computer.
    Call it when a com_ tool says no COM account is configured or login failed, or when the
    user asks to sign in. Never ask for the password in chat; the pop-ups collect everything."""
    return await chat_login.com_login(ctx, email, workspace, region)


SIGN_IN = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


@mcp.tool(annotations=SIGN_IN)
async def com_login_start(
    email: Annotated[str | None, Field(description="The user's HPE GreenLake email, only if they already said it. Omit it otherwise: the user then types it on a local page (do not guess it).")] = None,
    workspace: Annotated[str | None, Field(description="GreenLake workspace name, if the user already said which.")] = None,
    region: Annotated[Literal["us-west", "eu-central", "ap-northeast"] | None, Field(description="COM region; detected if omitted.")] = None,
) -> dict[str, Any]:
    """Sign the user in to HPE GreenLake without pop-ups, for clients where com_login's pop-ups
    do not appear. Returns the first step for the user, which you must relay in the chat:
    `enter_email` with a `url` of a local page where the user types their email (when you did not
    pass one), `approve_push` with the `number` to tap in Okta Verify (@hpe.com accounts), or
    `enter_password` with a `url` of a secure page on this computer for the password (other
    accounts; never ask for the password in chat). Then call com_login_status until it returns
    `signed_in` (the com_ tools work immediately) or `failed`."""
    return await chat_login.com_login_start(email, workspace, region)


@mcp.tool(annotations=SIGN_IN)
async def com_login_status(
    workspace: Annotated[str | None, Field(description="The user's choice when the last status was choose_workspace.")] = None,
) -> dict[str, Any]:
    """Next step of the sign-in begun by com_login_start. Waits up to 45 seconds for the user,
    then returns the current step (`approve_push`, `enter_password`, `choose_workspace` with
    `workspaces`), `signed_in` with the server count, or `failed`. If `waiting` is true nothing
    changed yet: remind the user of the step and call it again."""
    return await chat_login.com_login_status(workspace)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Add an iLO or OneView to the inventory", readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    )
)
async def add_server(
    ctx: Context,
    type: Annotated[Literal["ilo", "oneview"] | None, Field(description="Pre-fills the pop-up: ilo or oneview.")] = None,
    host: Annotated[str | None, Field(description="Pre-fills the pop-up: IP address or name of the iLO or OneView.")] = None,
    name: Annotated[str | None, Field(description="Pre-fills the pop-up: short name for the server.")] = None,
    username: Annotated[str | None, Field(description="Pre-fills the pop-up: user name (default Administrator).")] = None,
) -> dict[str, Any] | mcp_types.InputRequiredResult:
    """Add one iLO or OneView to the user's inventory with pop-ups in the chat: a form for the
    type, address, name and user name, then a secure page for the password. The login is
    checked before anything is saved. The tools use the new server immediately. Call it when
    the user wants to add a server, or when no iLO or OneView is configured. Never ask for the
    password in chat, and never pass one; the secure page collects it."""
    return await chat_inventory.add_server(ctx, type, name, host, username)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Add an iLO or OneView without pop-ups", readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    )
)
async def add_server_start(
    host: Annotated[str | None, Field(description="IP address or name of the iLO or OneView, only if the user already said it. Omit it otherwise: the user then types it on a local page.")] = None,
    type: Annotated[Literal["ilo", "oneview"], Field(description="ilo (one server) or oneview (OneView / Synergy Composer).")] = "ilo",
    name: Annotated[str | None, Field(description="Short name for it, e.g. dl380-lab; defaults to the address. An existing name is replaced.")] = None,
    username: Annotated[str | None, Field(description="User name; default Administrator.")] = None,
) -> dict[str, Any]:
    """Add one iLO or OneView to the user's inventory without pop-ups, for clients where
    add_server's pop-ups do not appear (or were cancelled). Returns `enter_server` (no host given:
    the page asks for the address, user name and password) or `enter_password`, with a `url` of a
    secure page on this computer: tell the user to open it and fill it in there (never in the chat;
    never pass a password). Then call add_server_status until it returns `saved` (the
    tools use the server immediately) or `failed`. The login is checked before anything is saved."""
    return await chat_inventory.add_server_start(type, host, name, username)


@mcp.tool(annotations=SIGN_IN)
async def add_server_status() -> dict[str, Any]:
    """Next step of add_server_start. Waits up to 45 seconds for the user, then returns
    `enter_password` (again, with the reason, if the password was wrong), `saved`, or `failed`.
    If `waiting` is true nothing changed yet: remind the user of the step and call it again."""
    return await chat_inventory.add_server_status()


@mcp.tool(
    annotations=ToolAnnotations(
        title="Import servers from a file", readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    )
)
async def import_inventory(ctx: Context) -> dict[str, Any] | mcp_types.InputRequiredResult:
    """Import many iLO and OneView entries from a .ini or .csv file (Excel: save as CSV) with
    pop-ups in the chat: the user types the file's path, confirms what was found, and gives one
    shared password on a secure page for entries that have none. Optionally checks each login.
    The tools use the entries immediately. Takes no arguments: the user chooses the file, not you."""
    return await chat_inventory.import_inventory(ctx)


# ── HPE OneView / Synergy ─────────────────────────────────────────────────────


@mcp.tool(annotations=READ_ONLY)
async def oneview_get_appliance(appliance: ApplianceName = None) -> dict[str, Any]:
    """OneView appliance report: model and software version, appliance CPU/memory/disk
    health, counts of server hardware, profiles, enclosures and interconnects, server
    hardware by status and power, and active alerts by severity."""
    return await _ov(appliance, oneview.appliance_info)


@mcp.tool(annotations=READ_ONLY)
async def oneview_list_server_hardware(appliance: ApplianceName = None) -> list[dict[str, Any]]:
    """Servers managed by OneView: name, OS hostname, model, serial, status, power,
    assigned profile, enclosure/bay, CPU, memory, ROM, OS and iLO (model, firmware, IP)."""
    return await _ov(appliance, oneview.list_hardware)


@mcp.tool(annotations=READ_ONLY)
async def oneview_get_server_hardware(
    server: Annotated[str, Field(description="Server hardware name (e.g. 'Enclosure-01, bay 2'), OS hostname, serial, UUID or iLO IP.")],
    appliance: ApplianceName = None,
) -> dict[str, Any]:
    """One OneView server in detail: the list fields plus UID light, every firmware
    component with location and version, and its active alerts."""
    return await _ov(appliance, lambda c: oneview.get_hardware(c, server))


@mcp.tool(annotations=READ_ONLY)
async def oneview_list_server_profiles(appliance: ApplianceName = None) -> list[dict[str, Any]]:
    """Server profiles: status, assigned server hardware, template and compliance,
    firmware baseline, boot mode and connection count."""
    return await _ov(appliance, oneview.list_profiles)


@mcp.tool(annotations=READ_ONLY)
async def oneview_list_alerts(
    appliance: ApplianceName = None,
    active_only: Annotated[bool, Field(description="Only alerts that are still Active.")] = True,
    severity: Annotated[Literal["Critical", "Warning", "OK", "Unknown"] | None, Field(description="Only this severity.")] = None,
    limit: Limit = 50,
) -> list[dict[str, Any]]:
    """Newest OneView alerts with the affected resource, description and corrective action."""
    return await _ov(appliance, lambda c: oneview.list_alerts(c, active_only, severity, limit))


@mcp.tool(annotations=READ_ONLY)
async def oneview_list_enclosures(appliance: ApplianceName = None) -> list[dict[str, Any]]:
    """Synergy frames / enclosures: model, serial, status, firmware baseline, populated
    bays, Composer modules, power capacity and allocation, PSU status and failed fans."""
    return await _ov(appliance, oneview.list_enclosures)


@mcp.tool(annotations=READ_ONLY)
async def oneview_list_interconnects(appliance: ApplianceName = None) -> list[dict[str, Any]]:
    """Interconnect modules (Virtual Connect, switches): model, firmware, status, power,
    enclosure, hostname and IP. For uplink and port status use oneview_list_uplink_sets."""
    return await _ov(appliance, oneview.list_interconnects)


@mcp.tool(annotations=READ_ONLY)
async def oneview_list_uplink_sets(appliance: ApplianceName = None) -> dict[str, Any]:
    """Synergy / Virtual Connect uplink status in one call: every uplink set with its status,
    type, networks (with VLAN), reachability and each uplink port (module, port, linked or not,
    Active / StandBy role, speed, LLDP neighbor switch and port), plus issues such as no networks,
    unlinked ports or no module redundancy, and related active alerts. Use it for "uplink status"."""
    return await _ov(appliance, oneview.list_uplink_sets)


@mcp.tool(annotations=READ_ONLY)
async def oneview_list_firmware_baselines(appliance: ApplianceName = None) -> list[dict[str, Any]]:
    """Firmware baselines (SPP / Synergy Service Packs) uploaded to OneView, newest first."""
    return await _ov(appliance, oneview.list_baselines)


_OV_BLOCKED_PREFIXES = ("/rest/login-sessions", "/rest/sessions", "/rest/users", "/rest/certificates/ca/")
# iLO console / SSO URLs carry live iLO session keys; keypair endpoints return private keys.
_OV_BLOCKED_PARTS = ("remoteconsoleurl", "ilossourl", "javaremoteconsoleurl", "/sso", "keypair")


@mcp.tool(annotations=READ_ONLY)
async def oneview_rest_get(
    path: Annotated[str, Field(description="OneView REST path starting with /rest/, e.g. /rest/logical-interconnects or /rest/server-hardware/{id}/utilization")],
    appliance: ApplianceName = None,
) -> dict[str, Any]:
    """Raw read-only GET of any OneView REST resource, for data the other oneview_ tools
    don't cover (networks, uplink sets, logical enclosures, tasks, utilization). Secrets are redacted."""
    low = path.lower()
    if not low.startswith("/rest/") or ".." in path:
        raise ToolError("path must start with /rest/")
    if low.startswith(_OV_BLOCKED_PREFIXES) or any(part in low for part in _OV_BLOCKED_PARTS):
        raise ToolError("Session, user, console/SSO URL and key resources are not exposed")
    return redact(await _ov(appliance, lambda c: c.get(path)))


def _version_tuple(v: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(p) for p in v.lstrip("v").split("."))
    except ValueError:
        return None


async def _release_status() -> str:
    """One line comparing this version with the latest GitHub release (best effort)."""
    try:
        async with httpx.AsyncClient(timeout=5, transport=_transport, follow_redirects=True) as client:
            resp = await client.get(LATEST_RELEASE_API, headers={"Accept": "application/vnd.github+json"})
            if resp.status_code == 404:
                return "latest release: none published yet"
            resp.raise_for_status()
            latest = resp.json()["tag_name"]
    except (httpx.HTTPError, KeyError, ValueError):
        return "latest release: unknown (cannot reach GitHub)"
    mine, theirs = _version_tuple(__version__), _version_tuple(latest)
    if mine is None or theirs is None:
        return f"latest release: {latest}"
    if mine == theirs:
        return f"latest release: {latest} (up to date)"
    if mine > theirs:
        return f"latest release: {latest} (this build is newer, unreleased)"
    return f"latest release: {latest}  UPDATE AVAILABLE (restart your AI client; see the README)"


async def _check(connect: bool) -> int:
    """Self-test for installers: version, tools, config, and optionally a login to each target."""
    print(f"proliant-mcp {__version__}: {len(await mcp.list_tools())} tools")
    print(await _release_status())
    try:
        servers, appliances, accounts = load_servers(), load_oneview(), load_com()
    except FileNotFoundError as exc:
        print(exc)
        return 1
    print(f"config file: {config_path() or '(none; using environment variables)'}")
    if not (servers or appliances or accounts):
        print("nothing configured yet (see docs/configuration.md)")
        return 0
    failed = 0

    async def probe(label: str, fn: Callable[[], Awaitable[str]]) -> None:
        nonlocal failed
        if not connect:
            print(label)
            return
        try:
            print(f"{label}  OK  {await fn()}")
        except (RedfishError, OneViewError, ComError) as exc:
            failed += 1
            print(f"{label}  FAILED  {exc}")

    async def ilo_line(s: Server) -> str:
        async with RedfishClient(s, transport=_transport) as client:
            summary = await ilo.server_summary(client)
        return f"{summary['model']}, iLO {summary['ilo_version']}"

    async def ov_line(a: OneViewAppliance) -> str:
        async with OneViewClient(a, transport=_transport) as client:
            v = await client.get("/rest/appliance/nodeinfo/version")
        return f"{v.get('modelNumber')} {v.get('softwareVersion')}"

    async def com_line(a: ComAccount) -> str:
        async with ComClient(a, transport=_transport) as client:
            total = (await client.get("/servers", {"limit": 1})).get("total")
        line = f"{total} servers"
        if client.region != a.region:
            line += f" in {client.region} (this workspace has no COM in {a.region})"
        if a.workspace:
            line += f"; workspace '{a.workspace}'"
        return line + f"; login: {a.source or 'configuration'}"

    if servers:
        print("iLO servers:")
    for s in servers.values():
        await probe(f"  {s.name}  {s.host}", lambda s=s: ilo_line(s))
    if appliances:
        print("OneView appliances:")
    for a in appliances.values():
        await probe(f"  {a.name}  {a.host}", lambda a=a: ov_line(a))
    if accounts:
        print("COM accounts:")
    for c in accounts.values():
        await probe(f"  {c.name}  {c.region}", lambda c=c: com_line(c))
    return 1 if failed else 0


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="proliant-mcp", description=__doc__)
    parser.add_argument("command", nargs="?", choices=["login", "logout", "import"], help="login: sign in to HPE GreenLake with your email and an Okta Verify push (creates the API client for you); logout: forget that login; import FILE: add the iLO / OneView entries of a .ini or .csv file to your inventory")
    parser.add_argument("file", nargs="?", help="with import: the .ini or .csv file")
    parser.add_argument("--replace", action="store_true", help="with import: overwrite entries that are already in your inventory")
    parser.add_argument("--no-check", action="store_true", help="with import: do not log in to each imported server")
    parser.add_argument("--email", help="with login: your GreenLake email (asked if omitted)")
    parser.add_argument("--workspace", help="with login: workspace name (asked if you have several)")
    parser.add_argument("--region", choices=["us-west", "eu-central", "ap-northeast"], help="with login: Compute Ops Management region (detected if omitted)")
    parser.add_argument("--check", action="store_true", help="print version, tool count and configured iLOs, OneView appliances and COM accounts, then exit")
    parser.add_argument("--connect", action="store_true", help="with --check: also log in to each iLO, OneView appliance and COM account")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default 8000)")
    args = parser.parse_args()
    if args.command == "login":
        raise SystemExit(login.run_login(args.email, args.workspace, args.region))
    if args.command == "logout":
        raise SystemExit(login.run_logout())
    if args.command == "import":
        if not args.file:
            parser.error("import needs a file: proliant-mcp import servers.csv")
        raise SystemExit(inventory_cli.run_import(args.file, args.replace, not args.no_check))
    if args.check:
        raise SystemExit(asyncio.run(_check(args.connect)))
    if args.http:
        # No authentication on this endpoint: keep it on localhost or a trusted lab network.
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
