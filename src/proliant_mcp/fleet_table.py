"""Ready-made Markdown table and exact counts for a list of COM servers.

A model that is asked for a table from the raw JSON has to write every row itself (slow) and
tends to miscount. Here the server builds the table and the counts, so the model only shows them.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

HEALTH_MARKS = {"OK": "🟢", "WARNING": "🟡", "CRITICAL": "🔴"}
DEFAULT_COLUMNS = ["name", "model", "health"]


def _health(s: dict[str, Any]) -> str:
    return s.get("health") or "UNKNOWN"


def _yes_no(value: Any) -> str:
    return "Yes" if value else "No" if value is not None else "-"


def _subscription(s: dict[str, Any]) -> str | None:
    sub = s.get("subscription") or {}
    return " ".join(x for x in (sub.get("state"), sub.get("tier")) if x) or None


def _hw(s: dict[str, Any]) -> dict[str, Any]:
    return s.get("hardware") or {}


def _cpu_model(s: dict[str, Any]) -> str | None:
    hw = _hw(s)
    if not hw.get("cpu_gpu_known"):
        return "unknown"
    return ", ".join(f"{c['count']} x {c['model']}" for c in hw.get("cpu") or []) or "none"


def _gpu(s: dict[str, Any]) -> str | None:
    hw = _hw(s)
    if not hw.get("cpu_gpu_known"):
        return "unknown"
    return ", ".join(f"{g['count']} x {g['name']}" for g in hw.get("gpus") or []) or "none"


def _storage(s: dict[str, Any]) -> str | None:
    storage = _hw(s).get("storage") or {}
    if not storage.get("known"):
        return "unknown"
    groups = [d for c in storage.get("controllers") or [] for d in c.get("drives") or []] + list(storage.get("direct_drives") or [])
    return f"{', '.join(groups)} ({storage.get('total_gb', 0) / 1000:g} TB)" if groups else "no drives"


def _nics(s: dict[str, Any]) -> str | None:
    network = (_hw(s).get("network") or {})
    if not network.get("known"):
        return "unknown"
    return ", ".join(f"{a['name']} {a['ports']}p {a['mode'] or ''}".strip() for a in network.get("adapters") or []) or "none"


# column key -> (heading, value)
COLUMNS: dict[str, tuple[str, Callable[[dict[str, Any]], Any]]] = {
    "name": ("Name", lambda s: s.get("name")),
    "model": ("Model", lambda s: s.get("model")),
    "health": ("Health", lambda s: f"{HEALTH_MARKS.get(_health(s), '⚪')} {_health(s)}"),
    "power": ("Power", lambda s: s.get("power_state")),
    "connected": ("Connected", lambda s: _yes_no(s.get("connected"))),
    "ilo_ip": ("iLO IP", lambda s: (s.get("ilo") or {}).get("ip")),
    "ilo_host": ("iLO host", lambda s: (s.get("ilo") or {}).get("hostname")),
    "os": ("OS", lambda s: s.get("os")),
    "serial": ("Serial", lambda s: s.get("serial_number")),
    "cpu": ("CPU", lambda s: s.get("processor")),
    "generation": ("Gen", lambda s: s.get("generation")),
    "subscription": ("Subscription", _subscription),
    "oneview": ("OneView", lambda s: s.get("oneview")),
    "ilo_version": ("iLO firmware", lambda s: (s.get("ilo") or {}).get("version")),
    "firmware_bundle": ("Firmware bundle", lambda s: s.get("firmware_bundle_id")),
    "cpu_model": ("CPU model", _cpu_model),
    "gpu": ("GPUs", _gpu),
    "storage": ("Storage", _storage),
    "nics": ("NICs", _nics),
}

# These read COM's cached hardware inventory, one request per server, so they are fetched only when asked for.
HARDWARE_COLUMNS = {"cpu_model", "gpu", "storage", "nics"}


def pick_columns(columns: list[str] | None) -> list[str]:
    return [c for c in (columns or DEFAULT_COLUMNS) if c in COLUMNS] or DEFAULT_COLUMNS


def _cell(value: Any) -> str:
    text = "-" if value is None or value == "" else str(value)
    return " ".join(text.replace("|", "\\|").split())


def build(rows: list[dict[str, Any]], columns: list[str] | None = None) -> dict[str, Any]:
    """Counts and a Markdown table for COM server rows (as returned by ``com.server_row``)."""
    keys = pick_columns(columns)
    table = "No servers match."
    if rows:
        head = "| " + " | ".join(COLUMNS[k][0] for k in keys) + " |\n|" + "---|" * len(keys)
        body = ["| " + " | ".join(_cell(COLUMNS[k][1](s)) for k in keys) + " |" for s in rows]
        table = "\n".join([head, *body])
    counts: dict[str, Any] = {"by_health": dict(Counter(_health(s) for s in rows).most_common())}
    # Only the counts that match a shown column, so a short table comes with a short answer.
    if "power" in keys:
        counts["by_power"] = dict(Counter(s.get("power_state") or "UNKNOWN" for s in rows).most_common())
    if "connected" in keys:
        counts["connected_to_com"] = {"yes": sum(1 for s in rows if s.get("connected")), "no": sum(1 for s in rows if not s.get("connected"))}
    if "gpu" in keys:
        gpus: Counter[str] = Counter()
        for s in rows:
            for g in _hw(s).get("gpus") or []:
                gpus[g["name"]] += g["count"]
        counts["gpus_by_model"] = dict(gpus.most_common())
        counts["servers_with_unknown_gpus"] = sum(1 for s in rows if not _hw(s).get("cpu_gpu_known"))
    return {
        "total": len(rows),
        "counts": counts,
        "columns": [COLUMNS[k][0] for k in keys],
        "table": table,
        "note": "Show `table` to the user exactly as written, with the counts. The counts are exact (computed by the server): "
        "quote them, do not recount the rows. 'unknown' means COM has no inventory for that server, not that it has none. "
        "For more detail pass `columns`.",
    }
