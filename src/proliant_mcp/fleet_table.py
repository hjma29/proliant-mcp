"""Ready-made Markdown table and exact counts for a list of COM servers.

A model that is asked for a table from the raw JSON has to write every row itself (slow) and
tends to miscount. Here the server builds the table and the counts, so the model only shows them.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

HEALTH_MARKS = {"OK": "🟢", "WARNING": "🟡", "CRITICAL": "🔴"}
DEFAULT_COLUMNS = ["name", "model", "health", "power", "connected", "ilo_ip"]


def _health(s: dict[str, Any]) -> str:
    return s.get("health") or "UNKNOWN"


def _yes_no(value: Any) -> str:
    return "Yes" if value else "No" if value is not None else "-"


def _subscription(s: dict[str, Any]) -> str | None:
    sub = s.get("subscription") or {}
    return " ".join(x for x in (sub.get("state"), sub.get("tier")) if x) or None


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
}


def _cell(value: Any) -> str:
    text = "-" if value is None or value == "" else str(value)
    return " ".join(text.replace("|", "\\|").split())


def build(rows: list[dict[str, Any]], columns: list[str] | None = None) -> dict[str, Any]:
    """Counts and a Markdown table for COM server rows (as returned by ``com.server_row``)."""
    keys = [c for c in (columns or DEFAULT_COLUMNS) if c in COLUMNS]
    if not keys:
        keys = DEFAULT_COLUMNS
    table = "No servers match."
    if rows:
        head = "| " + " | ".join(COLUMNS[k][0] for k in keys) + " |\n|" + "---|" * len(keys)
        body = ["| " + " | ".join(_cell(COLUMNS[k][1](s)) for k in keys) + " |" for s in rows]
        table = "\n".join([head, *body])
    return {
        "total": len(rows),
        "counts": {
            "by_health": dict(Counter(_health(s) for s in rows).most_common()),
            "by_power": dict(Counter(s.get("power_state") or "UNKNOWN" for s in rows).most_common()),
            "connected_to_com": {"yes": sum(1 for s in rows if s.get("connected")), "no": sum(1 for s in rows if not s.get("connected"))},
            "by_model": dict(Counter(s.get("model") or "unknown" for s in rows).most_common()),
        },
        "columns": [COLUMNS[k][0] for k in keys],
        "table": table,
        "note": "Show `table` to the user exactly as written, with the counts. The counts are exact (computed by the server): "
        "quote them, do not recount the rows. Ask for more columns with `columns`, or use com_list_servers when you must "
        "analyze the data.",
    }
