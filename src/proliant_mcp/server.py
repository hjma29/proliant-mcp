"""HPE ProLiant MCP server (iLO Redfish, read-only tools)."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Awaitable, Callable, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from proliant_mcp import ilo
from proliant_mcp.config import Server, config_path, load_servers
from proliant_mcp.redfish import RedfishClient, RedfishError

INSTRUCTIONS = """\
Tools for HPE ProLiant servers through their iLO (Redfish) management processor.
Start with list_servers to learn the configured server names, then pass a name as
`server`. All tools are read-only. If a tool returns an error for a server, report it
and suggest checking that the iLO is reachable; never invent values."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

ServerName = Annotated[str, Field(description="Server name from list_servers (or its iLO host/IP).")]

mcp = FastMCP("proliant-mcp", instructions=INSTRUCTIONS)

# Tests swap this to inject a fake iLO (httpx.MockTransport).
_transport = None


def _servers() -> dict[str, Server]:
    servers = load_servers()
    if not servers:
        raise ToolError(
            "No iLO servers configured. Set ILO_HOST/ILO_USERNAME/ILO_PASSWORD, or create "
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


@mcp.tool(annotations=READ_ONLY)
def list_servers() -> dict[str, Any]:
    """List the configured ProLiant servers (name and iLO host). Credentials are never returned."""
    servers = _servers()
    return {
        "config_file": str(config_path()) if config_path() else None,
        "servers": [{"name": s.name, "ilo_host": s.host} for s in servers.values()],
    }


@mcp.tool(annotations=READ_ONLY)
async def get_ilo_info(server: ServerName) -> dict[str, Any]:
    """iLO details: model (iLO 5/6/7), firmware version and date, license, Compute Ops
    Management connection status, and the iLO's own network (hostname, IP, MAC, DHCP)."""
    return await _run(server, ilo.ilo_info)


@mcp.tool(annotations=READ_ONLY)
async def get_server_summary(server: ServerName) -> dict[str, Any]:
    """Server identity and state: model, product ID, serial, power and POST state, health,
    UID light, BIOS and iLO versions, CPU count/model and total memory."""
    return await _run(server, ilo.server_summary)


@mcp.tool(annotations=READ_ONLY)
async def get_fleet_summary() -> list[dict[str, Any]]:
    """get_server_summary for every configured server at once. Unreachable servers are
    returned with an `error` field instead of failing the whole call."""

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
async def get_event_log(
    server: ServerName,
    log: Annotated[Literal["iml", "iel"], Field(description="iml = Integrated Management Log (server hardware events); iel = iLO Event Log.")] = "iml",
    limit: Annotated[int, Field(ge=1, le=500)] = 20,
    severity: Annotated[Literal["OK", "Warning", "Critical"] | None, Field(description="Only entries of this severity.")] = None,
) -> list[dict[str, Any]]:
    """Newest entries from the IML or iLO event log, optionally filtered by severity."""
    return await _run(server, lambda c: ilo.event_log(c, log, limit, severity))


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


async def _check(connect: bool) -> int:
    """Self-test for installers: version, tools, config, and optionally a login to each iLO."""
    from proliant_mcp import __version__

    print(f"proliant-mcp {__version__}: {len(await mcp.list_tools())} tools")
    try:
        servers = load_servers()
    except FileNotFoundError as exc:
        print(exc)
        return 1
    print(f"config file: {config_path() or '(none; using ILO_* environment variables)'}")
    if not servers:
        print("no iLO servers configured yet (see README: Configure your iLOs)")
        return 0
    failed = 0
    for s in servers.values():
        line = f"  {s.name}  {s.host}"
        if connect:
            try:
                async with RedfishClient(s, transport=_transport) as client:
                    summary = await ilo.server_summary(client)
                line += f"  OK  {summary['model']}, iLO {summary['ilo_version']}"
            except RedfishError as exc:
                failed += 1
                line += f"  FAILED  {exc}"
        print(line)
    return 1 if failed else 0


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="proliant-mcp", description=__doc__)
    parser.add_argument("--check", action="store_true", help="print version, tool count and configured servers, then exit")
    parser.add_argument("--connect", action="store_true", help="with --check: also log in to each iLO")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default 8000)")
    args = parser.parse_args()
    if args.check:
        raise SystemExit(asyncio.run(_check(args.connect)))
    if args.http:
        # No authentication on this endpoint: keep it on localhost or a trusted lab network.
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
