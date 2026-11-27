"""Answers from the local SQLite cache, with their age.

Two kinds of data, with their own lifetime (``inventory_cache.settings`` and ``state_ttl``):

- inventory, which rarely changes (hardware, firmware, catalogs, server lists for lookups);
- state, which can change any time (health, power, alerts, jobs, event logs, logins, links).

Both are cache first: they are served from SQLite whatever their age (default lifetime 7 days, ``STALE_KEEP``),
and every answer says when it was collected so the assistant can tell the user and offer a live refresh. A lifetime
can be shortened with ``set_inventory_cache``.

A tool runs inside ``session(refresh)``. ``fetch_many`` serves the entries younger than their lifetime from SQLite,
reads only the missing or older ones live and stores them. When a live inventory read fails, an entry up to
``inventory_cache.STALE_KEEP`` old is served instead and marked stale; state never is (an unreachable server is the
answer), and neither is a sign-in error (``no_stale``). Outside a session everything is read live. ``data_age``
says where the answer came from and how old it is. A cache
hit does not log in to the iLO, OneView or COM. ``run_poll`` refreshes everything on a timer (optional).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import random
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from proliant_mcp import inventory_cache


@dataclass
class Session:
    refresh: bool
    ready: tuple[bool, int, int | None, str | None] | None = None
    state_ttl: int = inventory_cache.DEFAULT_STATE_TTL
    oldest: float | None = None
    cached: int = 0
    live: int = 0
    stale: int = 0
    state_cached: bool = False
    notes: list[str] = field(default_factory=list)

    def used(self) -> bool:
        return bool(self.cached or self.live or self.stale)

    def summary(self) -> dict[str, Any]:
        """``data_age`` for a tool result."""
        if not self.cached and not self.stale:
            return {"source": "live", **({"note": " ".join(self.notes)} if self.notes else {})}
        minutes = round((time.time() - (self.oldest or time.time())) / 60)
        out: dict[str, Any] = {
            "source": "local cache" if not self.live else "local cache and live",
            "as_of": inventory_cache.timestamp(self.oldest or time.time()),
            "minutes_old": minutes,
        }
        if self.stale:
            out["stale_entries"] = self.stale
            out["warning"] = (
                f"{self.stale} entries could not be read live and are older than the cache lifetime; they show what the "
                "source said at as_of."
            )
        advice = (
            "Cached answer, collected at as_of. End the reply with exactly this one line and nothing else about the data's age: "
            f"\"Note: data collected {_age_text(minutes)} ago, say 'refresh' for the latest.\" "
            "When the user says refresh (or asks for current data), call again with refresh=true."
        )
        out["note"] = " ".join([*self.notes, advice])
        return out


_session: contextvars.ContextVar[Session | None] = contextvars.ContextVar("proliant_store_session", default=None)


@contextlib.contextmanager
def session(refresh: bool = False) -> Iterator[Session]:
    """Serve inventory from the local cache for the duration of one tool call (refresh=True reads live, then stores)."""
    current = Session(refresh)
    token = _session.set(current)
    try:
        yield current
    finally:
        _session.reset(token)


def _age_text(minutes: int) -> str:
    """'46 min', '3 h', '2 days': short enough to read at a glance."""
    if minutes < 60:
        return f"{max(minutes, 0)} min"
    if minutes < 48 * 60:
        return f"{round(minutes / 60)} h"
    return f"{round(minutes / 1440)} days"


def with_age(result: Any, current: Session) -> Any:
    """``data_age`` goes first: a client that cuts a long result (OpenCode keeps about 50 KB) must not cut the age."""
    if isinstance(result, dict) and current.used():
        return {"data_age": current.summary(), **{k: v for k, v in result.items() if k != "data_age"}}
    return result


async def _ready(current: Session) -> tuple[bool, int, int | None, str | None]:
    if current.ready is None:
        current.ready = await inventory_cache.prepare()
        if current.ready[3]:
            current.notes.append(current.ready[3])
        try:
            current.state_ttl = await asyncio.to_thread(inventory_cache.state_ttl)
        except inventory_cache.CacheError as exc:
            _note(current, exc)
    return current.ready


def _note(current: Session, exc: Exception) -> None:
    message = f"Local cache unavailable ({exc}); data was read live."
    if message not in current.notes:
        current.notes.append(message)


async def fetch_many(
    kind: str,
    scope: tuple[Any, ...],
    ids: list[str],
    fetch: Callable[[str], Awaitable[Any]],
    errors: tuple[type[BaseException], ...] = (),
    state: bool = False,
) -> dict[str, Any]:
    """id -> value. Fresh cached values first; the rest from ``fetch(id)`` (live, in parallel) and stored.

    ``state`` values (health, power, alerts, logs, links) use the shorter state lifetime and never fall back to a
    stale value. Otherwise a fetch that raises one of *errors* falls back to a stale cached value if there is one
    (except errors marked ``no_stale``); without one the exception itself is the value, for the caller to report.
    Outside a session every id is fetched live."""
    current = _session.get()

    async def live(i: str) -> Any:
        try:
            return await fetch(i)
        except errors as exc:
            return exc

    if current is None:
        return dict(zip(ids, await asyncio.gather(*(live(i) for i in ids))))
    enabled, ttl, generation, _ = await _ready(current)
    keys = {i: inventory_cache.key("store", 1, kind, *scope, i) for i in ids}
    found: dict[str, tuple[Any, float]] = {}
    if enabled and not current.refresh:
        try:
            found = await inventory_cache.get_many(list(keys.values()), min(current.state_ttl, ttl) if state else ttl)
        except inventory_cache.CacheError as exc:
            _note(current, exc)
    out: dict[str, Any] = {}
    for i in ids:
        if keys[i] in found:
            value, collected = found[keys[i]]
            out[i] = value
            current.cached += 1
            current.state_cached |= state
            current.oldest = collected if current.oldest is None else min(current.oldest, collected)
    missing = [i for i in ids if i not in out]
    if not missing:
        return out
    fetched = dict(zip(missing, await asyncio.gather(*(live(i) for i in missing))))
    collected = time.time()
    good = {i: v for i, v in fetched.items() if not isinstance(v, BaseException)}
    failed = [] if state else [i for i, v in fetched.items() if isinstance(v, BaseException) and not getattr(v, "no_stale", False)]
    current.live += len(good)
    if enabled and good:
        assert generation is not None
        try:
            await inventory_cache.put_many([(keys[i], v) for i, v in good.items()], collected, ttl, generation)
        except inventory_cache.CacheError as exc:
            _note(current, exc)
    stale: dict[str, tuple[Any, float]] = {}
    if enabled and failed:
        try:
            stale = await inventory_cache.get_many([keys[i] for i in failed], inventory_cache.STALE_KEEP)
        except inventory_cache.CacheError as exc:
            _note(current, exc)
    for i in missing:
        if i in good:
            out[i] = good[i]
        elif keys[i] in stale:
            value, when = stale[keys[i]]
            out[i] = value
            current.stale += 1
            current.oldest = when if current.oldest is None else min(current.oldest, when)
            reason = f"Live read failed: {fetched[i]}"
            if reason not in current.notes and len(current.notes) < 3:
                current.notes.append(reason)
        else:
            out[i] = fetched[i]
    return out


async def fetch_one(
    kind: str,
    scope: tuple[Any, ...],
    fetch: Callable[[], Awaitable[Any]],
    errors: tuple[type[BaseException], ...] = (),
    state: bool = False,
) -> Any:
    """One value through ``fetch_many``; raises the fetch error when there is no stale value to serve."""
    value = (await fetch_many(kind, scope, [""], lambda _: fetch(), errors, state))[""]
    if isinstance(value, BaseException):
        raise value
    return value


ILO_KINDS = ("identity", "info", "firmware", "processors", "memory", "storage", "summary", "health", "network")


def ilo_scope(server: Any) -> tuple[str, str, str]:
    """Cache partition for one iLO: a renamed, readdressed or re-credentialed server starts afresh."""
    return (server.name, server.host, server.username)


async def forget_ilo(server: Any) -> None:
    """Drop one iLO's cached reads (after its firmware changed), so the next answer is read live."""
    keys = [inventory_cache.key("store", 1, f"ilo_{kind}", *ilo_scope(server), "") for kind in ILO_KINDS]
    with contextlib.suppress(inventory_cache.CacheError):
        await inventory_cache.delete(keys)


async def poll_ready() -> str | None:
    """None when the cache can be filled, otherwise why not."""
    with session(refresh=True) as current:
        enabled, _, _, note = await _ready(current)
    return None if enabled else note or "The local cache is turned off (set_inventory_cache)."


async def auto_poll(
    poll: Callable[[], Awaitable[dict[str, Any]]],
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    rand: Callable[[float, float], float] = random.uniform,
) -> None:
    """Refresh the whole cache about every ``poll_minutes`` while this server runs (cancel it to stop).

    Each running server wakes at random times and ``claim_poll`` lets one of them refresh, so the load on the iLOs,
    OneView and COM is one refresh per interval and spreads over time. A refresh only replaces what it read
    successfully: when a source is down its last answer stays in the cache, with its age."""
    await sleep(rand(30, 120))
    while True:
        minutes = inventory_cache.DEFAULT_POLL_MINUTES
        try:
            minutes = await asyncio.to_thread(inventory_cache.poll_minutes)
            enabled, _ = await asyncio.to_thread(inventory_cache.settings)
            if minutes and enabled and await asyncio.to_thread(inventory_cache.claim_poll, minutes * 60):
                result = await poll()
                print(f"proliant-mcp: refreshed the local cache ({result.get('stored', 0)} entries"
                      + (f", {len(result['errors'])} sources failed" if result.get("errors") else "") + ")", file=sys.stderr, flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a failed refresh must never stop the server
            print(f"proliant-mcp: cache refresh failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        await sleep(rand(0.2, 0.3) * minutes * 60 if minutes else 600)


def run_poll(interval_minutes: float, once: bool, poll: Callable[[], Awaitable[dict[str, Any]]]) -> int:
    """``proliant-mcp poll``: keep the local cache warm so answers come from SQLite (Ctrl+C to stop)."""
    enabled, ttl = inventory_cache.settings()
    if not enabled:
        print("The local cache is turned off; turn it on with the set_inventory_cache tool first.")
        return 1
    if not once and interval_minutes * 60 >= ttl:
        print(f"Note: the cache lifetime ({ttl // 60} min) is not longer than the poll interval ({interval_minutes:g} min), "
              "so some answers will be read live. Raise it with set_inventory_cache (ttl_seconds) or poll more often.")
    while True:
        result = asyncio.run(poll())
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"{stamp} stored {result.get('stored', 0)} entries in {result.get('seconds', 0)} s"
              + (f"; errors: {result['errors']}" if result.get("errors") else "")
              + (f"; {result['error']}" if result.get("error") else ""), flush=True)
        if once:
            return 1 if result.get("error") else 0
        time.sleep(interval_minutes * 60)
