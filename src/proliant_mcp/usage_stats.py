"""Opt-in local counters of which tools are called, how long they take and whether they fail.

Off until the user turns it on. Stored only on this computer in ``~/.config/proliant-mcp/usage.sqlite3``:
tool name, client name, time, duration, ok or error and result size. Never arguments, results, server
names, addresses or credentials, and nothing is sent anywhere. Recording never makes a tool call fail.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from fastmcp.server.middleware import Middleware

from proliant_mcp.config import _home_config
from proliant_mcp.inventory import restrict_to_owner, write_text

RETENTION_DAYS = 90
BURST_GAP_SECONDS = 60
_PRUNE_EVERY = 500
_SETTINGS_REFRESH = 30.0  # seconds: another running server notices a change within this time
_OWN_TOOLS = {"get_usage_stats", "set_usage_stats"}


class UsageError(RuntimeError):
    """The usage database cannot be used."""


def _db_path() -> Path:
    return _home_config("proliant-mcp", "usage.sqlite3")


def _settings_path() -> Path:
    return _home_config("proliant-mcp", "usage-settings.json")


_state: dict[str, Any] = {"checked": None, "enabled": False, "writes": 0}


def enabled() -> bool:
    now = time.monotonic()
    if _state["checked"] is not None and now - _state["checked"] < _SETTINGS_REFRESH:
        return _state["enabled"]
    try:
        value = json.loads(_settings_path().read_text(encoding="utf-8")).get("enabled") is True
    except (OSError, ValueError, AttributeError):
        value = False
    _state.update(checked=now, enabled=value)
    return value


def _connect() -> sqlite3.Connection:
    path = _db_path()
    try:
        if path.is_symlink():
            raise UsageError("Usage database path is a symbolic link")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
            restrict_to_owner(path)
        db = sqlite3.connect(path, timeout=1)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS calls (ts REAL NOT NULL, tool TEXT NOT NULL, client TEXT NOT NULL, "
                "ms INTEGER NOT NULL, ok INTEGER NOT NULL, bytes INTEGER NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts)")
        except BaseException:
            db.close()
            raise
        return db
    except (OSError, sqlite3.Error) as exc:
        raise UsageError(f"Cannot open the usage database: {type(exc).__name__}") from exc


def record(tool: str, client: str, ms: float, ok: bool, size: int) -> None:
    """Add one call. Any failure is ignored: counting must never break a tool call."""
    try:
        db = _connect()
        try:
            with db:
                db.execute("INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?)", (time.time(), tool, client[:60], int(ms), int(ok), int(size)))
                _state["writes"] += 1
                if _state["writes"] % _PRUNE_EVERY == 1:
                    db.execute("DELETE FROM calls WHERE ts < ?", (time.time() - RETENTION_DAYS * 86400,))
        finally:
            db.close()
    except (UsageError, sqlite3.Error, OSError):
        pass


def _bursts(times: list[float]) -> list[dict[str, Any]]:
    """Runs of calls with no gap longer than BURST_GAP_SECONDS: roughly one question each."""
    runs: list[list[float]] = []
    for t in times:
        if runs and t - runs[-1][-1] <= BURST_GAP_SECONDS:
            runs[-1].append(t)
        else:
            runs.append([t])
    return [
        {"started": dt.datetime.fromtimestamp(r[0]).isoformat(timespec="seconds"), "calls": len(r), "seconds": round(r[-1] - r[0], 1)}
        for r in sorted(runs, key=len, reverse=True)[:5]
    ]


def summary(days: int = 30) -> dict[str, Any]:
    on = enabled()
    base: dict[str, Any] = {"enabled": on, "days": days, "path": str(_db_path())}
    if not _db_path().exists():
        return {**base, "total_calls": 0, "note": "No calls recorded yet." if on else "Counting is off; turn it on with set_usage_stats."}
    db = _connect()
    try:
        rows = db.execute("SELECT ts, tool, client, ms, ok, bytes FROM calls WHERE ts >= ? ORDER BY ts", (time.time() - days * 86400,)).fetchall()
    except sqlite3.Error as exc:
        raise UsageError(f"Cannot read the usage database: {type(exc).__name__}") from exc
    finally:
        db.close()
    per_tool: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for _, tool, _, ms, ok, size in rows:
        per_tool[tool].append((ms, ok, size))
    tools = [
        {
            "tool": name,
            "calls": len(v),
            "errors": sum(1 for _, ok, _ in v if not ok),
            "avg_ms": round(sum(ms for ms, _, _ in v) / len(v)),
            "max_ms": max(ms for ms, _, _ in v),
            "avg_kb": round(sum(size for _, _, size in v) / len(v) / 1000, 1),
        }
        for name, v in per_tool.items()
    ]
    tools.sort(key=lambda t: -t["calls"])
    return {
        **base,
        "total_calls": len(rows),
        "errors": sum(1 for r in rows if not r[4]),
        "by_tool": tools[:30],
        "by_client": dict(Counter(r[2] or "unknown" for r in rows).most_common(10)),
        "by_day": dict(sorted(Counter(dt.datetime.fromtimestamp(r[0]).strftime("%Y-%m-%d") for r in rows).items())[-14:]),
        "busiest_bursts": _bursts([r[0] for r in rows]),
        "note": f"Names and timings only. A burst is calls less than {BURST_GAP_SECONDS} s apart, about one question; "
        "a burst of dozens of calls usually means the assistant was lost. Kept for "
        f"{RETENTION_DAYS} days.",
    }


def configure(enable: bool, clear: bool = False) -> dict[str, Any]:
    removed = 0
    if enable or clear:
        db = _connect()
        try:
            with db:
                if clear:
                    removed = db.execute("DELETE FROM calls").rowcount
        except sqlite3.Error as exc:
            raise UsageError(f"Cannot update the usage database: {type(exc).__name__}") from exc
        finally:
            db.close()
    try:
        write_text(_settings_path(), json.dumps({"enabled": enable}) + "\n")
    except OSError as exc:
        raise UsageError(f"Cannot save the usage setting: {type(exc).__name__}") from exc
    _state.update(checked=time.monotonic(), enabled=enable)
    return {"enabled": enable, "cleared": removed if clear else None, "path": str(_db_path()),
            "note": "Other running proliant-mcp servers notice the change within 30 seconds. Nothing leaves this computer."}


def _client_name(context: Any) -> str:
    try:
        return str(context.fastmcp_context.session.client_params.client_info.name or "unknown")
    except AttributeError:
        return "unknown"


def _size(result: Any) -> int:
    return sum(len(getattr(block, "text", "") or "") for block in getattr(result, "content", None) or [])


class UsageMiddleware(Middleware):
    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        name = context.message.name
        if name in _OWN_TOOLS or not enabled():
            return await call_next(context)
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception:
            await asyncio.to_thread(record, name, _client_name(context), (time.perf_counter() - started) * 1000, False, 0)
            raise
        await asyncio.to_thread(record, name, _client_name(context), (time.perf_counter() - started) * 1000, True, _size(result))
        return result
