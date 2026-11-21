"""Opt-in cached hardware inventory snapshots; live operational tools stay unchanged."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from proliant_mcp import com, ilo, inventory_cache
from proliant_mcp.config import Server
from proliant_mcp.redfish import RedfishClient, RedfishError

_ILO_PARTS: dict[str, Callable[[RedfishClient], Awaitable[Any]]] = {
    "processors": ilo.processors,
    "memory": ilo.memory,
    "firmware": ilo.firmware_inventory,
}


async def _snapshot(
    cache_key: str, enabled: bool, ttl: int, generation: int | None,
    refresh: bool, fetch: Callable[[], Awaitable[Any]],
) -> dict[str, Any]:
    if enabled and not refresh:
        hit = await inventory_cache.get(cache_key, ttl)
        if hit is not None:
            data, collected = hit
            return {"source": "cache", "collected_at": inventory_cache.timestamp(collected),
                    "expires_at": inventory_cache.timestamp(collected + ttl), "data": data}
    data = await fetch()
    collected = time.time()
    stored = False
    if enabled:
        assert generation is not None
        stored = await inventory_cache.put(cache_key, data, collected, ttl, generation)
    return {"source": "live", "collected_at": inventory_cache.timestamp(collected),
            "expires_at": inventory_cache.timestamp(collected + ttl) if stored else None,
            **({"cache_note": "Not stored because the cache changed or a newer snapshot was already stored."}
               if enabled and not stored else {}), "data": data}


async def ilo_snapshot(
    servers: list[Server], categories: list[str], refresh: bool, transport: Any,
) -> dict[str, Any]:
    enabled, ttl = inventory_cache.settings()
    generation = await inventory_cache.generation() if enabled else None
    gate = asyncio.Semaphore(8)

    async def one(server: Server) -> dict[str, Any]:
        async with gate:
            try:
                # Authenticate even for a cache hit; cached data must not bypass iLO login.
                async with RedfishClient(server, transport=transport) as client:
                    result = {}
                    for part in categories:
                        cache_key = inventory_cache.key("ilo", 1, server.name, server.host, server.username, server.verify_ssl, part)
                        result[part] = await _snapshot(
                            cache_key, enabled, ttl, generation, refresh,
                            lambda part=part: _ILO_PARTS[part](client),
                        )
                    return {"server": server.name, "inventory": result}
            except RedfishError as exc:
                return {"server": server.name, "error": str(exc)}

    return {
        "cache_enabled": enabled,
        "note": "Hardware snapshots can be stale, including component health. Use live health/status tools for current state.",
        "servers": await asyncio.gather(*(one(server) for server in servers)),
    }


async def com_snapshot(client: com.ComClient, name_contains: str | None, refresh: bool) -> dict[str, Any]:
    enabled, ttl = inventory_cache.settings()
    generation = await inventory_cache.generation() if enabled else None
    rows = [com.server_row(server) for server in await client.get_all("/servers")]
    if name_contains:
        rows = [row for row in rows if name_contains.lower() in (row["name"] or "").lower()]
    gate = asyncio.Semaphore(8)

    async def one(row: dict[str, Any]) -> dict[str, Any]:
        async with gate:
            try:
                cache_key = inventory_cache.key(
                    "com", 1, client.account.client_id, client.account.workspace, client.region, row["id"],
                )

                async def fetch() -> dict[str, Any]:
                    return com.hardware_summary(await client.get(f"/servers/{row['id']}/inventory"))

                value = await _snapshot(cache_key, enabled, ttl, generation, refresh, fetch)
                return {"server": row["name"], "inventory": value}
            except com.ComError as exc:
                return {"server": row["name"], "error": str(exc)}

    return {
        "cache_enabled": enabled,
        "note": "These are hardware snapshots of COM's own inventory. Component health may be stale; use live health/status tools for current state.",
        "servers": await asyncio.gather(*(one(row) for row in rows)),
    }
