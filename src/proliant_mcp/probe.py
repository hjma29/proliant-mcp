"""Check that an inventory entry accepts its login (used when adding or importing servers)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from proliant_mcp import ilo
from proliant_mcp.config import OneViewAppliance, Server
from proliant_mcp.inventory import Entry
from proliant_mcp.oneview import OneViewClient, OneViewError, OneViewLoginError
from proliant_mcp.redfish import RedfishClient, RedfishError, RedfishLoginError

LOGIN_ERRORS = (RedfishLoginError, OneViewLoginError)
PROBE_ERRORS = (RedfishError, OneViewError)


@dataclass
class ProbeResult:
    name: str
    ok: bool
    detail: str
    login_refused: bool = False


async def probe(entry: Entry, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Log in and describe the target in one line. Raises RedfishError / OneViewError."""
    if entry.kind == "ilo":
        server = Server(entry.name, entry.host, entry.username, entry.password, entry.verify_ssl)
        async with RedfishClient(server, transport=transport) as client:
            summary = await ilo.server_summary(client)
        return f"{summary['model']}, {summary['ilo_version']}"
    appliance = OneViewAppliance(entry.name, entry.host, entry.username, entry.password, entry.verify_ssl)
    async with OneViewClient(appliance, transport=transport) as client:
        version = await client.get("/rest/appliance/nodeinfo/version")
    return f"{version.get('modelNumber')} {version.get('softwareVersion')}"


async def probe_many(entries: list[Entry], transport: httpx.AsyncBaseTransport | None = None, workers: int = 8) -> list[ProbeResult]:
    gate = asyncio.Semaphore(workers)

    async def one(entry: Entry) -> ProbeResult:
        async with gate:
            try:
                return ProbeResult(entry.name, True, await probe(entry, transport))
            except LOGIN_ERRORS as exc:
                return ProbeResult(entry.name, False, str(exc), login_refused=True)
            except PROBE_ERRORS as exc:
                return ProbeResult(entry.name, False, str(exc))

    return list(await asyncio.gather(*(one(e) for e in entries)))
