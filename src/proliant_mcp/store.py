"""Inventory answers from the local SQLite cache, with their age.

Inventory that rarely changes (COM's server list and per-server hardware, each iLO's model and serial, OneView's
server hardware) is kept in ``inventory_cache`` (owner-only file, no passwords or tokens). A tool that reads
inventory runs inside ``session(refresh)``. ``fetch_many`` then serves the entries younger than the cache lifetime
from SQLite, reads only the missing or older ones live, and stores them. When a live read fails, an entry up to
``inventory_cache.STALE_KEEP`` old is served instead and marked stale. Outside a session everything is read live,
as before. ``data_age`` says where the answer came from and how old it is, so the assistant can tell the user and
offer a refresh. A cache hit does not log in to the iLO, OneView or COM: the data is what that source said at the
time shown. ``poll`` refreshes everything on a timer, so answers stay served from SQLite.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from proliant_mcp import inventory_cache

STALE_AFTER_MINUTES = 5  # from this age the assistant should say how old the data is


@dataclass
class Session:
    refresh: bool
    ready: tuple[bool, int, int | None, str | None] | None = None
    oldest: float | None = None
    cached: int = 0
    live: int = 0
    stale: int = 0
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
        out["note"] = " ".join([
            *self.notes,
            f"If minutes_old is {STALE_AFTER_MINUTES} or more, tell the user in one short line how old the inventory is and "
            "that they can ask for a refresh (call again with refresh=true). Health and power here are as of that time too.",
        ])
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


def with_age(result: Any, current: Session) -> Any:
    if isinstance(result, dict) and current.used():
        result["data_age"] = current.summary()
    return result


async def _ready(current: Session) -> tuple[bool, int, int | None, str | None]:
    if current.ready is None:
        current.ready = await inventory_cache.prepare()
        if current.ready[3]:
            current.notes.append(current.ready[3])
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
) -> dict[str, Any]:
    """id -> value. Fresh cached values first; the rest from ``fetch(id)`` (live, in parallel) and stored.

    A fetch that raises one of *errors* falls back to a stale cached value if there is one; otherwise the exception
    itself is the value, for the caller to report. Outside a session every id is fetched live."""
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
            found = await inventory_cache.get_many(list(keys.values()), ttl)
        except inventory_cache.CacheError as exc:
            _note(current, exc)
    out: dict[str, Any] = {}
    for i in ids:
        if keys[i] in found:
            value, collected = found[keys[i]]
            out[i] = value
            current.cached += 1
            current.oldest = collected if current.oldest is None else min(current.oldest, collected)
    missing = [i for i in ids if i not in out]
    if not missing:
        return out
    fetched = dict(zip(missing, await asyncio.gather(*(live(i) for i in missing))))
    collected = time.time()
    good = {i: v for i, v in fetched.items() if not isinstance(v, BaseException)}
    failed = [i for i, v in fetched.items() if isinstance(v, BaseException)]
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
) -> Any:
    """One value through ``fetch_many``; raises the fetch error when there is no stale value to serve."""
    value = (await fetch_many(kind, scope, [""], lambda _: fetch(), errors))[""]
    if isinstance(value, BaseException):
        raise value
    return value


async def poll_once(transport: Any = None) -> dict[str, Any]:
    """Read every source's inventory live and store it: COM server lists and hardware, iLO identity, OneView hardware."""
    from proliant_mcp import com, lookup
    from proliant_mcp.config import load_com

    started = time.time()
    with session(refresh=True) as current:
        enabled, _, _, note = await _ready(current)
        if not enabled:
            return {"stored": 0, "error": note or "The local cache is turned off (set_inventory_cache)."}

        async def com_account(account: Any) -> tuple[str, str | None]:
            try:
                async with com.ComClient(account, transport=transport) as c:
                    rows = await com.server_rows(c, cached=True)
                    ids = [r["id"] for r in rows]
                    await com._hardware_for(c, ids, network=True)
                return f"com:{account.name}", None
            except com.ComError as exc:
                return f"com:{account.name}", str(exc)

        results = await asyncio.gather(
            lookup._ilo_rows(transport), lookup._oneview_rows(transport), *(com_account(a) for a in load_com().values())
        )
        errors = {**results[0][1], **results[1][1], **{name: err for name, err in results[2:] if err}}
        return {"stored": current.live, "seconds": round(time.time() - started, 1), **({"errors": errors} if errors else {})}


def run_poll(interval_minutes: float, once: bool) -> int:
    """``proliant-mcp poll``: keep the local cache warm so answers come from SQLite (Ctrl+C to stop)."""
    enabled, ttl = inventory_cache.settings()
    if not enabled:
        print("The local cache is turned off; turn it on with the set_inventory_cache tool first.")
        return 1
    if not once and interval_minutes * 60 >= ttl:
        print(f"Note: the cache lifetime ({ttl // 60} min) is not longer than the poll interval ({interval_minutes:g} min), "
              "so some answers will be read live. Raise it with set_inventory_cache (ttl_seconds) or poll more often.")
    while True:
        result = asyncio.run(poll_once())
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"{stamp} stored {result['stored']} entries in {result.get('seconds', 0)} s"
              + (f"; errors: {result['errors']}" if result.get("errors") else ""), flush=True)
        if once:
            return 1 if result.get("error") else 0
        time.sleep(interval_minutes * 60)
