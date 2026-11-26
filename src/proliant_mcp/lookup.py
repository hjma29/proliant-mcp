"""``list_servers`` with ``generation`` or ``model``: the servers of every source, counted once.

Reads the iLO inventory (identity only), every COM account and every OneView appliance in parallel and
merges them by serial number, so a server that is both in the iLO inventory and in COM (or OneView) is one
server with two sources, not two. A filter that matches nothing says what does exist instead.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from typing import Any

import httpx

from proliant_mcp import com, fleet_table, ilo, oneview
from proliant_mcp.com import ComClient, ComError
from proliant_mcp.config import load_com, load_oneview, load_servers
from proliant_mcp.oneview import OneViewClient, OneViewError
from proliant_mcp.redfish import RedfishClient, RedfishError

_GEN = re.compile(r"\bgen\s*(\d+)\b", re.I)
SAMPLE_PER_MODEL = 3


def generation_of(value: str | None, model: str | None) -> str | None:
    """'11' from COM's 'GEN_11', OneView's 'Gen11' or a model name such as 'ProLiant DL380 Gen11'."""
    digits = com._generation_number(value)
    if digits:
        return digits
    found = _GEN.search(model or "")
    return found.group(1) if found else None


async def _ilo_rows(transport: httpx.AsyncBaseTransport | None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    async def one(server: Any) -> tuple[dict[str, Any] | None, str | None]:
        try:
            async with RedfishClient(server, transport=transport) as client:
                return {"source": "ilo", "label": f"ilo:{server.name}", "name": server.name, **await ilo.identity(client)}, None
        except RedfishError as exc:
            return None, f"ilo:{server.name}: {exc}"

    done = await asyncio.gather(*(one(s) for s in load_servers().values()))
    return [r for r, _ in done if r], {e.split(": ", 1)[0]: e.split(": ", 1)[1] for _, e in done if e}


async def _com_rows(transport: httpx.AsyncBaseTransport | None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    async def one(account: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
        try:
            async with ComClient(account, transport=transport) as c:
                servers = await c.get_all("/servers")
        except ComError as exc:
            return [], {f"com:{account.name}": str(exc)}
        rows = [com.server_row(s) for s in servers]
        return [
            {"source": "com", "label": f"com:{account.name}", "name": r["name"], "model": r["model"], "serial_number": r["serial_number"],
             "generation": r["generation"], "power_state": r["power_state"]}
            for r in rows
        ], {}

    done = await asyncio.gather(*(one(a) for a in load_com().values()))
    return [r for rows, _ in done for r in rows], {k: v for _, errs in done for k, v in errs.items()}


async def _oneview_rows(transport: httpx.AsyncBaseTransport | None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    async def one(appliance: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
        try:
            async with OneViewClient(appliance, transport=transport) as c:
                hardware = await c.get_all("/rest/server-hardware")
        except OneViewError as exc:
            return [], {f"oneview:{appliance.name}": str(exc)}
        rows = [oneview.hardware_row(h, {}, {}) for h in hardware]
        return [
            {"source": "oneview", "label": f"oneview:{appliance.name}", "name": r["server_name"] or r["name"], "model": r["model"],
             "serial_number": r["serial_number"], "generation": r["generation"], "power_state": r["power_state"]}
            for r in rows
        ], {}

    done = await asyncio.gather(*(one(a) for a in load_oneview().values()))
    return [r for rows, _ in done for r in rows], {k: v for _, errs in done for k, v in errs.items()}


def merge(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per physical server: rows with the same serial number are joined, the first (iLO) name wins."""
    by_serial: dict[str, dict[str, Any]] = {}
    merged: list[dict[str, Any]] = []
    for r in rows:
        serial = (r.get("serial_number") or "").strip().upper()
        entry = by_serial.get(serial) if serial else None
        if entry is None:
            entry = {"name": r["name"], "model": None, "serial_number": r.get("serial_number"), "power_state": None, "generation": None,
                     "sources": [], "source_types": []}
            merged.append(entry)
            if serial:
                by_serial[serial] = entry
        entry["sources"].append(r["label"])
        entry["source_types"].append(r["source"])
        for key in ("model", "power_state"):
            entry[key] = entry[key] or r.get(key)
        entry["generation"] = entry["generation"] or generation_of(r.get("generation"), r.get("model"))
    for entry in merged:
        entry["generation"] = entry["generation"] or generation_of(None, entry["model"])
    return merged


def _available(servers: list[dict[str, Any]]) -> dict[str, Any]:
    gens = Counter(f"Gen{s['generation']}" if s["generation"] else "unknown" for s in servers)
    models = Counter(fleet_table._short_model(s["model"]) or "unknown" for s in servers)
    return {
        "servers": len(servers),
        "generations": dict(sorted(gens.items(), key=lambda kv: (kv[0] == "unknown", kv[0]))),
        "models": dict(models.most_common(12)),
    }


def _text(matches: list[dict[str, Any]]) -> tuple[str, dict[str, str], str]:
    """A short table up to COMPACT_ABOVE servers, otherwise the grouped list with a few names per model."""
    def sources(s: dict[str, Any]) -> str:
        return "+".join(dict.fromkeys(s["source_types"]))

    if len(matches) <= fleet_table.COMPACT_ABOVE:
        rows = [f"| {s['name']} | {fleet_table._short_model(s['model']) or '-'} | {s['power_state'] or '-'} | {sources(s)} |" for s in matches]
        return "\n".join(["| Name | Model | Power | Source |", "|---|---|---|---|", *rows]), {}, ""
    groups: dict[str, list[str]] = {}
    for s in matches:
        groups.setdefault(fleet_table._short_model(s["model"]) or "unknown model", []).append(f"{s['name']} ({sources(s)})")
    lines, remaining, hidden = fleet_table.grouped_lines(sorted(groups.items(), key=lambda kv: -len(kv[1])), SAMPLE_PER_MODEL)
    return "\n".join(lines), remaining, f" Then tell the user {hidden} more servers match and offer the full list. Do NOT show `remaining` unless asked; then show every name with no new tool call."


async def lookup(generation: str | None, model: str | None, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    (ilo_rows, ilo_err), (com_rows, com_err), (ov_rows, ov_err) = await asyncio.gather(
        _ilo_rows(transport), _com_rows(transport), _oneview_rows(transport)
    )
    errors = {**ilo_err, **com_err, **ov_err}
    servers = merge([*ilo_rows, *com_rows, *ov_rows])
    wanted_gen = com._generation_number(generation)
    wanted_model = (model or "").strip().lower()
    matches = [
        s for s in servers
        if (not wanted_gen or s["generation"] == wanted_gen) and (not wanted_model or wanted_model in (s["model"] or "").lower())
    ]
    by_source = Counter(t for s in matches for t in dict.fromkeys(s["source_types"]))
    counts: dict[str, Any] = {
        "servers_checked": len(servers),
        "by_source": {k: by_source.get(k, 0) for k in ("ilo", "com", "oneview")},
        "in_more_than_one_source": sum(1 for s in matches if len(set(s["source_types"])) > 1),
    }
    if wanted_gen:
        counts["unknown_generation"] = sum(1 for s in servers if not s["generation"])
    table, remaining, more = _text(matches) if matches else ("No server matches.", {}, "")
    note = (
        "Show `table` as written and quote `counts`: they are exact. Servers in more than one source are merged by serial number "
        "and counted once; `total` is unique servers, `by_source` counts each source separately. unknown_generation servers have "
        "no model or generation in any source, so they are not counted as matches." + more
    )
    return {
        "filter": {"generation": generation, "model": model},
        "total": len(matches),
        "counts": counts,
        "layout": "sample" if remaining else "all",
        "table": table,
        **({"remaining": remaining} if remaining else {}),
        **({"available": _available(servers)} if not matches else {}),
        **({"source_errors": errors} if errors else {}),
        "note": note + (" Sources that failed are in source_errors; the counts leave them out." if errors else ""),
    }


async def com_available(c: ComClient) -> dict[str, Any]:
    """What a COM account has, for a filter that matched nothing: server count, generations and models."""
    rows = [com.server_row(s) for s in await c.get_all("/servers")]
    return _available([{"generation": generation_of(r["generation"], r["model"]), "model": r["model"]} for r in rows])
