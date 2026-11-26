"""HPE compute MCP server: read-only tools for ProLiant iLO, Compute Ops Management and OneView,
plus an opt-in iLO firmware update that the user picks and confirms on a local page."""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
import time
from collections import Counter
from typing import Annotated, Any, Awaitable, Callable, Literal

import httpx
import mcp_types
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from proliant_mcp import __version__, chat_api_clients, chat_bulk, chat_firmware, chat_inventory, chat_login, com, compat, fleet_table, ilo, ilo_firmware, inventory_cache, inventory_cli, login, lookup, oneview, snapshots, usage_stats
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
Start with list_servers; for "how many Gen13 servers" pass generation or model: it counts across
all three sources. For other fleet questions start with com_get_overview or oneview_get_appliance. The same physical server can appear in
all three; match by serial number. If a tool returns an error, report it; never invent values.
To find which server an IP, hostname or serial belongs to, call find_server. For the OS a server runs
or why it isn't booting, see `os` in get_server_summary / get_fleet_summary. For significant events on
several servers use get_fleet_event_summary; for who logged in use get_ilo_logins; for Synergy uplinks
oneview_list_uplink_sets; for GreenLake services greenlake_list_services.
To show the user a list of servers use com_list_servers_table: a ready-made table and exact counts, to
present as they are (never retype the data into a script). Default columns are name, model, health: pass
`columns` for exactly what was asked (power, connected, ilo_ip, ilo_version, firmware_bundle, gpu, storage,
nics, cpu_model), nothing more. For analysis across servers use
com_list_servers (include_hardware adds CPU, GPU and storage) or com_list_gpus; for a table of storage
controllers, drives, GPUs and NICs com_hardware_report; for NICs, HBAs, link speeds or network mode
com_list_nics. Never loop com_get_server or com_api_get over many servers.
For hardware inventory (an iLO's processors, memory, firmware, or COM's hardware inventory) prefer
get_inventory_snapshot and get_com_inventory_snapshot: a local cache (on, 15 minutes) answers repeats in
milliseconds and each result says cache or live and when. Pass refresh=true when the
user wants current data. Never use snapshots for health, power, alerts or links: use the live tools.
Change the cache with set_inventory_cache, or clear it with clear_inventory_cache, only when asked.
If a com_ tool says no COM account is configured, the COM login failed (HTTP 401) or COM is not set up,
sign the user in right away in this chat; do not send them to a browser, terminal or the GreenLake site.
Call com_login (pop-ups). If it fails or is cancelled without the user seeing a pop-up, call
com_login_start, relay what it returns (page link, Okta Verify number) and call com_login_status without
waiting for the user to reply, until signed_in. Pass the email only if the user gave it; never ask for it
with a question tool (it may be auto-answered "user not available"). If this MCP server runs on another
computer than the user (remote gateway), the page cannot be opened: ask for the email in chat as plain
text and pass it; an @hpe.com account then needs only the Okta number, no page.
If the user hit the limit of GreenLake API clients, or asks to see or delete them, use
greenlake_list_api_clients_start / greenlake_delete_api_clients_start (the user picks on a page) and
greenlake_api_clients_status.
If no iLO or OneView is configured, or the user wants to add servers, call add_server (one). For several
iLOs, a list, a range or a file call add_servers_start, show its page link and call add_servers_status
until saved or failed; the user chooses targets and enters credentials on the page. import_inventory
imports a .ini or .csv via pop-ups. If add_server's pop-ups do not appear, call add_server_start (address
only if the user gave it), relay the page link and call add_server_status until saved.
Never ask the user to type a password into the chat; these tools collect it on a secure page."""

GUIDE = """proliant-mcp answers questions about HPE ProLiant servers from three sources: iLO (a few servers reached directly), HPE Compute Ops Management (COM, the GreenLake cloud service, usually the whole fleet) and HPE OneView (Synergy frames). Start with list_servers: it shows which iLOs, OneView appliances and COM accounts are configured. Then pick by question:

COM fleet (how many, which, health, power, connected, generation, model, iLO firmware, subscriptions): com_get_overview for counts; com_list_servers_table for a list, with filters health, connected, generation, name_contains and storage_controller (e.g. NS204) and columns such as gpu, storage_controllers, nics, nic_firmware, ilo_version.
COM hardware: GPUs com_list_gpus (gpu_model, e.g. H100); storage controllers, drives and capacity com_hardware_report (storage_controller, storage_over_tb); NICs and ports com_list_nics. One server: com_get_server. Also com_list_alerts, com_list_jobs, com_list_groups, com_list_firmware_bundles, greenlake_list_services.
Find a server by name, serial number or IP: find_server. Count or list servers of all sources by generation or model: list_servers(generation, model).
One iLO server (name or IP from list_servers): get_server_summary (model, serial, BIOS, iLO version, power, health, OS), get_health, get_processors (CPUs and GPUs), get_memory, get_storage, get_network_adapters (MACs, links, firmware), get_firmware_inventory, get_event_log, get_ilo_info. All iLOs at once: get_fleet_summary, get_fleet_event_summary.
OneView: oneview_get_appliance, oneview_list_enclosures, oneview_list_server_hardware, oneview_list_server_profiles (template compliance), oneview_list_alerts, oneview_list_interconnects, oneview_list_uplink_sets, oneview_list_firmware_baselines.
iLO firmware: ilo_firmware_list_updates (latest version from HPE, steps, minutes); ilo_firmware_update_start to update (the user picks and confirms on a page), then ilo_firmware_update_status for live progress.

Counts in results are exact: quote them. "unknown" means the source has no data, not that the part is absent. Never guess a value; if no tool covers it, say so."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

ServerName = Annotated[str, Field(description="Server name from list_servers (or its iLO host/IP).")]

ApplianceName = Annotated[str | None, Field(description="OneView appliance name from list_servers; omit to use the first one.")]
AccountName = Annotated[str | None, Field(description="COM account name from list_servers; omit to use the first one.")]
Limit = Annotated[int, Field(ge=1, le=1000)]
Generation = Annotated[str | None, Field(description="Only this server generation, e.g. 11 or Gen11.")]
StorageController = Annotated[
    str | None,
    Field(description="Only servers with a storage controller whose model contains this text, e.g. NS204 or MR416 (any case)."),
]

compat.install()
mcp = FastMCP("proliant-mcp", instructions=INSTRUCTIONS, version=__version__)
mcp.add_middleware(usage_stats.UsageMiddleware())


@mcp.resource("proliant://guide", name="proliant-mcp guide", description="Which proliant-mcp tool answers which question (iLO, COM, OneView).", mime_type="text/markdown")
def guide() -> str:
    return GUIDE

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


# The version leads this description so it shows in tool lists such as Copilot's /mcp show.
@mcp.tool(
    annotations=READ_ONLY,
    description=f"[proliant-mcp {__version__}] What this server can reach: iLO servers, OneView appliances, COM accounts, the "
    'running version and whether a newer release exists. Call first, and for "which version". With generation or model: '
    "counts and lists the matching servers across iLO, COM and OneView.",
)
async def list_servers(
    generation: Generation = None,
    model: Annotated[str | None, Field(description="Model contains this text, e.g. DL380.")] = None,
) -> dict[str, Any]:
    """What this server can reach: iLO servers, OneView appliances, COM accounts, the running
    version and whether a newer release exists. Call first, and for "which version". With
    generation or model: counts and lists the matching servers across iLO, COM and OneView."""
    if generation or model:
        return await lookup.lookup(generation, model, _transport)
    return {
        "proliant_mcp_version": __version__,
        "release_check": await _cached_release_status(),
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
    """Which server is this? Looks up a name, iLO IP/hostname, serial or OS hostname in the iLO
    inventory, every COM account and every OneView appliance in one call. If nothing matches and the
    query is an address, it reads that iLO's login-free identity pages (iLO model, firmware, server
    model, serial). Use it instead of searching the list tools by hand."""
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


@mcp.tool(annotations=READ_ONLY)
async def get_inventory_cache_status() -> dict[str, Any]:
    """Whether local hardware snapshot caching is enabled (it is on by default) and its lifetime.
    It does not inspect credentials or create a cache."""
    try:
        enabled, ttl = inventory_cache.settings()
        return {"enabled": enabled, "ttl_seconds": ttl, "path": str(inventory_cache._db_path()),
                "note": "The cache contains local hardware inventory. Live status tools do not use it."}
    except inventory_cache.CacheError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def set_inventory_cache(
    enabled: Annotated[bool, Field(description="True to use the local hardware snapshot cache (the default), false to turn it off.")],
    ttl_seconds: Annotated[int | None, Field(ge=1, le=86400, description="Maximum snapshot age in seconds; omit to preserve the existing setting.")] = None,
) -> dict[str, Any]:
    """Turn the local hardware snapshot cache on or off, or change its lifetime, when the user asks.
    It is on by default and stored on this computer in an owner-only folder (server names and
    serial numbers, never passwords). Turning it off keeps the existing data; clear_inventory_cache
    removes it. Live tools are unaffected."""
    try:
        return await asyncio.to_thread(inventory_cache.configure, enabled, ttl_seconds)
    except inventory_cache.CacheError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
async def clear_inventory_cache() -> dict[str, Any]:
    """Remove locally cached hardware snapshots when the user asks.
    Does not change the cache setting or delete the inventory or credentials."""
    try:
        count = await asyncio.to_thread(inventory_cache.clear)
        return {"removed": count, "note": "Cache rows cleared; filesystem backups or SQLite free pages may retain old data."}
    except inventory_cache.CacheError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def get_usage_stats(
    days: Annotated[int, Field(ge=1, le=90, description="Look back this many days.")] = 30,
) -> dict[str, Any]:
    """Local tool-call counters (on by default; set_usage_stats turns them off): calls, errors and time per tool and client,
    calls per day, and the longest bursts of calls (dozens in a row means a lost assistant)."""
    try:
        return await asyncio.to_thread(usage_stats.summary, days)
    except usage_stats.UsageError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
async def set_usage_stats(
    enabled: Annotated[bool, Field(description="True to count calls, false to stop.")],
    clear: Annotated[bool, Field(description="Also delete the counts so far.")] = False,
) -> dict[str, Any]:
    """Turn the local tool-call counters on or off, or delete them, only when the user asks. They hold
    tool name, client, time, duration, ok or error and result size on this computer only; nothing else."""
    try:
        return await asyncio.to_thread(usage_stats.configure, enabled, clear)
    except usage_stats.UsageError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
async def get_inventory_snapshot(
    servers: Annotated[list[str] | None, Field(description="Configured iLO names or addresses; omit for all.")] = None,
    categories: Annotated[list[Literal["processors", "memory", "firmware"]] | None, Field(description="Hardware categories; omit for all three.")] = None,
    refresh: Annotated[bool, Field(description="Fetch new data even when a cached snapshot is available.")] = False,
) -> dict[str, Any]:
    """Hardware snapshots (processors, memory, firmware) for one or more iLOs, from the local cache
    (on by default, 15 minutes) or live; each says which and when it was collected. refresh=true
    forces a live read. The iLO login is checked even on a hit. Not for current health or power:
    use the live tools."""
    targets = list(_servers().values()) if servers is None else list(dict.fromkeys(_resolve(name) for name in servers))
    if not targets:
        raise ToolError("Select at least one iLO server.")
    parts = list(dict.fromkeys(categories if categories is not None else ["processors", "memory", "firmware"]))
    if not parts:
        raise ToolError("Select at least one inventory category.")
    try:
        return await snapshots.ilo_snapshot(targets, parts, refresh, _transport)
    except inventory_cache.CacheError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
async def get_com_inventory_snapshot(
    account: AccountName = None,
    name_contains: Annotated[str | None, Field(description="Only server names containing this text.")] = None,
    refresh: Annotated[bool, Field(description="Fetch fresh COM inventory rather than using local cache entries.")] = False,
) -> dict[str, Any]:
    """COM hardware inventory across the fleet, from the local cache or live; each server says which
    and when. COM's own inventory may be older. refresh=true bypasses the local cache, not COM's
    collection. Not for current health, power or alerts: use the live COM tools."""
    try:
        return await _com(account, lambda client: snapshots.com_snapshot(client, name_contains, refresh))
    except inventory_cache.CacheError as exc:
        raise ToolError(str(exc)) from exc


When = Annotated[
    str | None,
    Field(description="7d, 24h, 30m, a date (2026-11-12) or ISO 8601; UTC unless a zone is given."),
]


LogName = Annotated[
    Literal["iml", "iel", "sl"],
    Field(description="iml (hardware events), iel (iLO events) or sl (Security Log, iLO 6+)."),
]
MinSeverity = Annotated[
    Literal["Warning", "Critical"] | None, Field(description="Only entries at least this severe.")
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
        bool, Field(description="Group identical messages with count and first/last time, most severe first.")
    ] = False,
) -> list[dict[str, Any]]:
    """Newest entries from the IML, iLO event log or Security Log, filtered by severity, time and
    text; with summary, identical messages are grouped (compact; use it for "what happened").
    Entries with a wrong iLO clock are marked `time_invalid`, listed last, and left out when
    since/until is set. For several servers use get_fleet_event_summary; for logins use get_ilo_logins."""
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
    """Significant events for many servers in one call: per server, the IML, iLO event log or Security
    Log grouped by message (count, first/last time, repaired), Warning and Critical by default, most
    severe first. Use it for "errors on these servers". Unreachable iLOs return an `error`."""
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
        bool, Field(description="Also count logins from the computer running proliant-mcp.")
    ] = False,
) -> dict[str, Any]:
    """Who logged in to the iLOs, from each iLO Event Log, for many servers in one call: logins per
    day, by type, user and source IP/host, failed logins and lockout alerts. Says when a log does
    not reach back to the window start (counts are then a minimum). proliant-mcp's own sessions
    show as Browser logins and are counted only in `this_computer_logins` unless
    include_this_computer. Unreachable iLOs return an `error`."""
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


# ── iLO firmware updates (the only tools that change a server) ────────────────


@mcp.tool(annotations=READ_ONLY)
async def ilo_firmware_list_updates(
    servers: Annotated[list[str] | None, Field(description="Server names; omit for all.")] = None,
) -> dict[str, Any]:
    """Latest iLO firmware from HPE for the configured iLOs: installed, newer versions, the steps each
    needs (some need an older one first) and minutes. Changes nothing."""
    targets = [_resolve(s) for s in servers] if servers else list(_servers().values())
    surveys = await ilo_firmware.survey(targets, _transport)
    family = next((s.family for s in surveys if s.family), "ilo7")
    return {
        "servers": [s.public() for s in surveys],
        "one_step": ilo_firmware.step_estimate(family),
        "next_step": "To update, call ilo_firmware_update_start: the user picks the servers, versions and sequential or parallel "
        "on a page and confirms there.",
    }


@mcp.tool(annotations=ToolAnnotations(title="Update iLO firmware", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
async def ilo_firmware_update_start(
    servers: Annotated[list[str] | None, Field(description="Servers to pre-tick.")] = None,
    version: Annotated[str | None, Field(description="Version to pre-select; default latest.")] = None,
    mode: Literal["sequential", "parallel"] | None = None,
) -> dict[str, Any]:
    """Update iLO firmware on servers the user picks and confirms on a local page (show its url; never
    pick or confirm for them). About 10 min a step; only the iLO restarts. Then poll the status tool."""
    return await chat_firmware.start(_servers(), servers, version, mode, _transport)


@mcp.tool(annotations=READ_ONLY)
async def ilo_firmware_update_status() -> dict[str, Any]:
    """Progress of ilo_firmware_update_start: per server step, phase, flash_percent, minutes left."""
    return await chat_firmware.status()


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
    include_hardware: Annotated[bool, Field(description="Add a `hardware` object per server (CPU, GPUs, storage controllers and drives) from COM's cached inventory. Slower: one read per server.")] = False,
    generation: Generation = None,
) -> list[dict[str, Any]]:
    """Raw COM JSON for analysis; to list servers use com_list_servers_table.
    Per server: name, serial, model, generation, health, power, COM connection, subscription,
    iLO host/IP/version, OS, CPU vendor, firmware bundle. About 700 characters per server, so a
    big fleet fills the model's context. One call returns the whole fleet."""
    return await _com(account, lambda c: com.list_servers(c, health, connected, name_contains, include_hardware, generation=generation))


@mcp.tool(annotations=READ_ONLY)
async def com_list_servers_table(
    account: AccountName = None,
    health: Annotated[Literal["OK", "WARNING", "CRITICAL", "UNKNOWN"] | None, Field(description="Only servers with this health summary.")] = None,
    connected: Annotated[bool | None, Field(description="Only servers whose iLO is (or isn't) connected to COM.")] = None,
    name_contains: Annotated[str | None, Field(description="Case-insensitive substring of the server name.")] = None,
    generation: Generation = None,
    storage_controller: StorageController = None,
    columns: Annotated[
        list[
            Literal[
                "name", "model", "health", "power", "connected", "ilo_ip", "ilo_host", "ilo_version", "firmware_bundle", "os",
                "serial", "cpu", "cpu_model", "gpu", "storage", "storage_controllers", "nics", "nic_firmware", "generation",
                "subscription", "oneview",
            ]
        ]
        | None,
        Field(
            description="Columns in order. Default: name, model, health. Add only what the user asks for: power, connected, "
            "ilo_ip, ilo_version, firmware_bundle; or gpu, storage (drives), storage_controllers (model + firmware), "
            "nics (ports, mode), nic_firmware (model + firmware), cpu_model (read per server from COM's cached inventory, slower)."
        ),
    ] = None,
) -> dict[str, Any]:
    """List or show the COM servers: ready to display, exact counts, fast. Use this for "list my
    servers", "which are critical", "servers with NS204" (storage_controller), "Gen11 servers with
    their NIC and storage controller firmware".
    With no `columns`, a big fleet comes back as a short grouped list (health summary, the servers
    needing attention, the OK ones by model); pass `columns` for a real table with exactly what was
    asked. Show `table` as written and quote `counts`; do not retype the data into a script. Same
    filters as com_list_servers, which returns the full JSON for analysis."""
    if storage_controller and not columns:
        columns = ["name", "model", "health", "storage_controllers"]
    wanted = fleet_table.pick_columns(columns)
    hardware = bool(fleet_table.HARDWARE_COLUMNS & set(wanted)) or bool(storage_controller)
    network = bool(fleet_table.NETWORK_COLUMNS & set(wanted))
    rows = await _com(account, lambda c: com.list_servers(c, health, connected, name_contains, hardware, network, generation))
    extra = None
    if storage_controller:
        rows, summary = com.match_storage_controller(rows, storage_controller)
        extra = {"storage_controller_filter": summary}
    result = fleet_table.build(rows, columns, extra)
    if not rows and (health or connected is not None or name_contains or generation or storage_controller):
        result["available"] = await _com(account, lookup.com_available)
        result["table"] = "No servers match. `available` shows what the fleet has."
    return result


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
    storage_over_tb: Annotated[float | None, Field(description="Only servers with more than this many TB of raw drive capacity (1 TB = 1000 GB); adds storage_filter counts.")] = None,
    generation: Generation = None,
    storage_controller: StorageController = None,
) -> dict[str, Any]:
    """Whole-fleet hardware table, compact: servers with identical hardware are grouped into one
    configuration with their names, model, CPU, storage controllers and drives, GPUs and network
    adapters (with mode), total_storage_gb, plus NIC mode totals and the servers COM has no data for
    (unknown, not absent). Use it for "table of each server's storage, GPU and NIC config" and for
    "how many servers have more than N TB" (storage_over_tb: storage_filter gives the counts)."""
    return await _com(account, lambda c: com.hardware_report(c, name_contains, connected, storage_over_tb, generation, storage_controller))


@mcp.tool(annotations=READ_ONLY)
async def com_list_nics(
    account: AccountName = None,
    name_contains: Annotated[str | None, Field(description="Case-insensitive substring of the server name.")] = None,
    connected: Annotated[bool | None, Field(description="Only servers whose iLO is (or isn't) connected to COM.")] = None,
    linked_only: Annotated[bool, Field(description="Only adapters with a port up.")] = False,
    generation: Generation = None,
    ports: Annotated[int | None, Field(description="Only adapters with exactly this many ports, e.g. 4.")] = None,
) -> dict[str, Any]:
    """Network adapter report for the whole COM fleet: per server each NIC/HBA (mode, ports, ports up,
    speeds, firmware, health), plus totals by mode, adapter model and link speed, and the servers COM
    has no network data for. Start here for any NIC, HBA, link speed or network mode question."""
    return await _com(account, lambda c: com.list_nics(c, name_contains, connected, linked_only, generation, ports))


@mcp.tool(annotations=READ_ONLY)
async def com_list_gpus(
    account: AccountName = None,
    name_contains: Annotated[str | None, Field(description="Case-insensitive substring of the server name.")] = None,
    connected: Annotated[bool | None, Field(description="Only servers whose iLO is (or isn't) connected to COM.")] = None,
    include_servers_without_gpus: Annotated[bool, Field(description="Also list servers with no GPU.")] = False,
    gpu_model: Annotated[str | None, Field(description="Only GPUs whose name contains this text, e.g. H100 or L40S.")] = None,
) -> dict[str, Any]:
    """GPU report for the whole COM fleet: per server the model, iLO firmware, health, CPU and GPUs
    (name, vendor, part number, count, health), plus totals by GPU model and the servers COM has
    no inventory for (GPUs unknown, not absent). Start here for any GPU question; set gpu_model
    (e.g. H100) to list only servers with that GPU."""
    return await _com(account, lambda c: com.list_gpus(c, name_contains, connected, include_servers_without_gpus, gpu_model))


@mcp.tool(annotations=READ_ONLY)
async def com_get_server(
    server: Annotated[str, Field(description="COM server name, serial number, iLO IP/hostname or id.")],
    account: AccountName = None,
) -> dict[str, Any]:
    """One COM server in detail: per-subsystem health, iLO, subscription, last firmware update,
    firmware inventory, CPU, GPUs, storage controllers and drives, network ports (mode, link, speed,
    MAC) and active alerts. For several servers use com_list_servers, com_list_gpus or com_list_nics."""
    return await _com(account, lambda c: com.get_server(c, server))


@mcp.tool(annotations=READ_ONLY)
async def com_list_alerts(
    account: AccountName = None,
    server: Annotated[str | None, Field(description="Only alerts for this server (name, serial, iLO IP or id).")] = None,
    severity: Annotated[Literal["CRITICAL", "WARNING", "OK"] | None, Field(description="Only this severity.")] = None,
    include_cleared: Annotated[bool, Field(description="Also return alerts that have cleared.")] = False,
    limit: Limit = 50,
) -> list[dict[str, Any]]:
    """Newest COM alerts (uncleared, 50 by default; each names its server). For "which servers have critical alerts" pass severity=CRITICAL; com_get_overview has the counts."""
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
    """Sign the user in to HPE GreenLake so the com_ tools work, with pop-ups: email, then an Okta
    Verify number (@hpe.com) or a secure password page (other accounts). Creates a GreenLake API
    client and saves it on this computer. Call it when a com_ tool says no COM account is configured
    or login failed, or when asked to sign in. Never ask for the password in chat."""
    return await chat_login.com_login(ctx, email, workspace, region)


SIGN_IN = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


@mcp.tool(annotations=SIGN_IN)
async def com_login_start(
    email: Annotated[str | None, Field(description="The user's GreenLake email, only if they said it (never guess). Omit to let them type it on a local page; if this server runs on another computer, ask in chat first.")] = None,
    workspace: Annotated[str | None, Field(description="GreenLake workspace name, if the user already said which.")] = None,
    region: Annotated[Literal["us-west", "eu-central", "ap-northeast"] | None, Field(description="COM region; detected if omitted.")] = None,
) -> dict[str, Any]:
    """Sign in to HPE GreenLake without pop-ups, for clients where com_login's pop-ups do not appear.
    Returns the step to relay in the chat: `enter_email` (url of a local page; skipped if you passed
    an email), `approve_push` (the `number` to tap in Okta Verify) or `enter_password` (url of a
    secure page; never ask for the password in chat). Then call com_login_status until `signed_in`
    or `failed`, without waiting for the user to reply."""
    return await chat_login.com_login_start(email, workspace, region)


@mcp.tool(annotations=SIGN_IN)
async def com_login_status(
    workspace: Annotated[str | None, Field(description="The user's choice when the last status was choose_workspace.")] = None,
) -> dict[str, Any]:
    """Next step of com_login_start: waits up to 45 s for the user, returns `approve_push`,
    `enter_password`, `choose_workspace` (with `workspaces`), `signed_in` (with the server count) or
    `failed`. If `waiting` is true, remind the user of the step and call again."""
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
    """Add one iLO or OneView to the inventory with pop-ups: a form (type, address, name, user name),
    then a secure page for the password. The login is checked before saving and the server is
    usable at once. Call it when the user wants to add a server or none is configured. Never ask
    for or pass a password."""
    return await chat_inventory.add_server(ctx, type, name, host, username)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Add an iLO or OneView without pop-ups", readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    )
)
async def add_server_start(
    host: Annotated[str | None, Field(description="IP address or name of the iLO or OneView, only if the user already said it. Omit it otherwise: the user then types it on a local page.")] = None,
    type: Annotated[Literal["ilo", "oneview"] | None, Field(description="ilo (one server) or oneview (OneView / Synergy Composer), only if the user said which. Omit it otherwise: it is detected from the address.")] = None,
    name: Annotated[str | None, Field(description="Short name for it, e.g. dl380-lab; defaults to the address. An existing name is replaced.")] = None,
    username: Annotated[str | None, Field(description="User name; default Administrator.")] = None,
) -> dict[str, Any]:
    """Add one iLO or OneView without pop-ups, for clients where add_server's pop-ups do not appear.
    Returns `enter_server` or `enter_password` with the `url` of a secure local page: tell the user
    to fill it in there (never in chat; never pass a password). Then call add_server_status until
    `saved` or `failed`, without waiting for the user to reply. The login is checked before saving.
    A new add ends the previous one; `previous_add` says how it ended: report that, not a guess."""
    return await chat_inventory.add_server_start(type, host, name, username)


@mcp.tool(annotations=SIGN_IN)
async def add_server_status() -> dict[str, Any]:
    """Next step of add_server_start. Waits up to 45 seconds for the user, then returns
    `enter_password` (again, with the reason, if the password was wrong), `saved`, or `failed`.
    If `waiting` is true nothing changed yet: remind the user of the step and call it again."""
    return await chat_inventory.add_server_status()


@mcp.tool(
    annotations=ToolAnnotations(
        title="Add multiple iLO servers", readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    )
)
async def add_servers_start() -> dict[str, Any]:
    """Add multiple iLOs from an IP range, pasted list, CSV, INI, text file or Excel workbook.
    Returns a local page link: show it to the user, who selects a file or enters addresses,
    reviews the list and types credentials there, never in chat. The page checks the selected
    logins and lets the user choose what to save. Call add_servers_status immediately and keep
    calling it until saved or failed; do not ask the user to confirm in chat."""
    return await chat_bulk.start()


@mcp.tool(annotations=SIGN_IN)
async def add_servers_status() -> dict[str, Any]:
    """Wait for the bulk add started by add_servers_start. Returns progress, saved results,
    or a failure. Keep calling while it is waiting; the browser page guides the user."""
    return await chat_bulk.status()


@mcp.tool(annotations=SIGN_IN)
async def greenlake_list_api_clients_start(
    email: Annotated[str | None, Field(description="The user's GreenLake email, only if they said it (never guess). Omit to use this computer's saved login or let them type it on a page.")] = None,
    workspace: Annotated[str | None, Field(description="GreenLake workspace name, if the user already said which.")] = None,
) -> dict[str, Any]:
    """List the user's own personal HPE GreenLake API clients (name, client ID, created, and whether
    this computer's saved login uses it), e.g. when they hit the limit. Signs in like
    com_login_start; changes nothing and shows no secrets. Relay each step, then call
    greenlake_api_clients_status until `listed`."""
    return await chat_api_clients.start(False, email, workspace)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Delete GreenLake API clients", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
    )
)
async def greenlake_delete_api_clients_start(
    email: Annotated[str | None, Field(description="The user's GreenLake email, only if they said it (never guess). Omit to use this computer's saved login or let them type it on a page.")] = None,
    workspace: Annotated[str | None, Field(description="GreenLake workspace name, if the user already said which.")] = None,
    preselect: Annotated[list[str] | None, Field(description="Words to pre-tick clients whose names contain them. The user still chooses and confirms on the page.")] = None,
) -> dict[str, Any]:
    """Delete personal HPE GreenLake API clients that the USER selects, e.g. after hitting the limit.
    Signs in like com_login_start, then a local page lists the clients with check boxes; the user
    ticks, reviews and confirms there. You cannot choose or confirm for them. Deletion is permanent
    and stops anything using those clients. Relay each step, then call greenlake_api_clients_status
    until `deleted`, `cancelled` or `failed`."""
    return await chat_api_clients.start(True, email, workspace, preselect)


@mcp.tool(annotations=READ_ONLY)
async def greenlake_api_clients_status(
    workspace: Annotated[str | None, Field(description="The user's choice when the last status was choose_workspace.")] = None,
) -> dict[str, Any]:
    """Next step of greenlake_list_api_clients_start or greenlake_delete_api_clients_start: waits up to
    45 s for the user, returns the step (`approve_push`, `enter_password`, `choose_workspace`,
    `select_api_clients`, `confirm_deletion`) or the result (`listed`, `deleted`, `cancelled`,
    `failed`). If `waiting` is true, remind the user and call again. Changes nothing itself."""
    return await chat_api_clients.status(workspace)


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
    """Synergy / Virtual Connect uplink status in one call: each uplink set (status, type, networks
    with VLAN) and uplink port (module, port, linked or not, Active/StandBy, speed, LLDP neighbor),
    plus issues (no networks, unlinked ports, no module redundancy) and related alerts. Use it for
    "uplink status"."""
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


_release_cache: tuple[float, str] | None = None


async def _cached_release_status() -> str:
    """_release_status, re-checked at most hourly so list_servers stays fast."""
    global _release_cache
    if _release_cache is None or time.monotonic() - _release_cache[0] > 3600:
        _release_cache = (time.monotonic(), await _release_status())
    return _release_cache[1]


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
        # stderr shows up in the AI client's MCP log (VS Code: MCP: List Servers > proliant > Show Output).
        print(f"proliant-mcp {__version__} started (stdio)", file=sys.stderr, flush=True)
        mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
