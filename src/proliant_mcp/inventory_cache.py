"""Local SQLite cache for hardware inventory (owner-only file, never passwords or tokens).

Used by the snapshot tools and by ``store`` (the COM hardware tools and list_servers filters). Entries are
served while younger than the lifetime and kept up to STALE_KEEP so an answer can still be given, marked stale,
when a source cannot be reached.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from proliant_mcp.config import _home_config
from proliant_mcp.inventory import write_text

DEFAULT_TTL = 3600
STALE_KEEP = 7 * 86400  # how long entries stay for a stale fallback after their lifetime ends
MAX_TTL = 86400
MAX_ROWS = 2000
MAX_VALUE_BYTES = 1_000_000
MAX_PAYLOAD_BYTES = 16_000_000
_APP_ID = 0x504D4350


class CacheError(RuntimeError):
    """The cache cannot be used safely."""


def _settings_path() -> Path:
    return _home_config("proliant-mcp", "cache-settings.json")


def _db_path() -> Path:
    return _home_config("proliant-mcp", "cache", "inventory.sqlite3")


def _secure_directory() -> None:
    directory = _db_path().parent
    try:
        if directory.is_symlink():
            raise CacheError("Cache directory cannot be a symbolic link")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if sys.platform == "win32":
            script = (
                "$ErrorActionPreference='Stop'; "
                "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User; "
                "$acl=New-Object System.Security.AccessControl.DirectorySecurity; "
                "$acl.SetAccessRuleProtection($true,$false); $acl.SetOwner($sid); "
                "$rule=[System.Security.AccessControl.FileSystemAccessRule]::new"
                "($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'); "
                "$acl.SetAccessRule($rule); "
                "[System.IO.Directory]::SetAccessControl($env:PROLIANT_CACHE_DIR, $acl)"
            )
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                env={**os.environ, "PROLIANT_CACHE_DIR": str(directory)},
                check=True, capture_output=True, timeout=15,
            )
        else:
            os.chmod(directory, 0o700)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CacheError(f"Cannot secure cache directory: {type(exc).__name__}") from exc


def settings() -> tuple[bool, int]:
    """(enabled, lifetime in seconds). The cache is on with the default lifetime until the user changes it."""
    path = _settings_path()
    if not path.exists():
        return True, DEFAULT_TTL
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CacheError(f"Cannot read cache settings: {type(exc).__name__}") from exc
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool or type(value.get("ttl_seconds")) is not int:
        raise CacheError("Invalid cache settings")
    ttl = value["ttl_seconds"]
    if not 1 <= ttl <= MAX_TTL:
        raise CacheError(f"Cache lifetime must be between 1 and {MAX_TTL} seconds")
    return value["enabled"], ttl


def configure(enabled: bool, ttl_seconds: int | None = None) -> dict[str, Any]:
    if ttl_seconds is None:
        _, ttl_seconds = settings()
    if not 1 <= ttl_seconds <= MAX_TTL:
        raise CacheError(f"Cache lifetime must be between 1 and {MAX_TTL} seconds")
    if enabled:
        _secure_directory()
        db = _connect(True)
        assert db is not None
        db.close()
    else:
        _bump_generation()
    try:
        write_text(_settings_path(), json.dumps({"enabled": enabled, "ttl_seconds": ttl_seconds}) + "\n")
    except OSError as exc:
        raise CacheError(f"Cannot save cache settings: {type(exc).__name__}") from exc
    return {"enabled": enabled, "ttl_seconds": ttl_seconds, "path": str(_db_path()),
            "note": "The local cache contains hardware inventory, not passwords. Disabling does not erase existing data."}


def ensure_ready() -> None:
    """Create the private cache directory and database on first use (the cache is on by default)."""
    if _db_path().exists():
        return
    _secure_directory()
    db = _connect(True)
    assert db is not None
    db.close()


def key(*parts: object) -> str:
    """Stable partition key; never include passwords, client secrets, or tokens."""
    data = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def _connect(create: bool) -> sqlite3.Connection | None:
    path = _db_path()
    if not path.exists() and not create:
        return None
    try:
        if path.is_symlink():
            raise CacheError("Cache path is not a regular file")
        if create:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(path, flags, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                os.chmod(path, 0o600)
        if path.is_symlink() or not path.is_file():
            raise CacheError("Cache path is not a regular file")
        db = sqlite3.connect(path.absolute().as_uri() + "?mode=rw", uri=True, timeout=1)
        try:
            db.execute("BEGIN IMMEDIATE")
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app_id == 0 and version == 0 and not tables and create:
                db.execute(f"PRAGMA application_id = {_APP_ID}")
                db.execute("CREATE TABLE snapshots (key TEXT PRIMARY KEY, collected_at REAL NOT NULL, payload TEXT NOT NULL)")
                db.execute("CREATE TABLE cache_state (generation INTEGER NOT NULL)")
                db.execute("INSERT INTO cache_state VALUES (0)")
                db.execute("PRAGMA user_version = 1")
            elif app_id != _APP_ID or version != 1:
                raise CacheError("Cache database has an unexpected format; it was not changed")
            db.commit()
        except BaseException:
            db.close()
            raise
        return db
    except (OSError, sqlite3.Error) as exc:
        raise CacheError(f"Cannot open inventory cache: {type(exc).__name__}") from exc


def _get(cache_key: str, ttl: int) -> tuple[Any, float] | None:
    db = _connect(False)
    if db is None:
        return None
    try:
        with db:
            row = db.execute("SELECT payload, collected_at FROM snapshots WHERE key = ?", (cache_key,)).fetchone()
        if row is None:
            return None
        age = time.time() - row[1]
        if age < 0 or age >= ttl:
            return None
        return json.loads(row[0]), row[1]
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise CacheError(f"Cannot read inventory cache: {type(exc).__name__}") from exc
    finally:
        db.close()


def _generation() -> int:
    db = _connect(False)
    if db is None:
        raise CacheError("Enabled cache database is missing")
    try:
        return int(db.execute("SELECT generation FROM cache_state").fetchone()[0])
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise CacheError(f"Cannot read cache generation: {type(exc).__name__}") from exc
    finally:
        db.close()


def _put(cache_key: str, data: Any, collected: float, ttl: int, generation: int) -> bool:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(payload.encode("utf-8")) > MAX_VALUE_BYTES:
        raise CacheError("Hardware snapshot exceeds the cache size limit")
    return _write([(cache_key, payload)], collected, ttl, generation) == 1


def _put_many(items: list[tuple[str, Any]], collected: float, ttl: int, generation: int) -> int:
    """Store several values in one transaction; values over the size limit are skipped. Returns how many were saved."""
    payloads = []
    for cache_key, data in items:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(payload.encode("utf-8")) <= MAX_VALUE_BYTES:
            payloads.append((cache_key, payload))
    return _write(payloads, collected, ttl, generation) if payloads else 0


def _write(payloads: list[tuple[str, str]], collected: float, ttl: int, generation: int) -> int:
    db = _connect(True)
    assert db is not None
    try:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT generation FROM cache_state").fetchone()[0] != generation:
            db.rollback()
            return 0
        with db:
            now = time.time()
            db.execute("DELETE FROM snapshots WHERE collected_at < ? OR collected_at > ?", (now - max(ttl, STALE_KEEP), now))
            saved = 0
            for cache_key, payload in payloads:
                saved += db.execute(
                    "INSERT INTO snapshots VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                    "collected_at = excluded.collected_at, payload = excluded.payload "
                    "WHERE excluded.collected_at >= snapshots.collected_at",
                    (cache_key, collected, payload),
                ).rowcount
            db.execute(
                "DELETE FROM snapshots WHERE key IN "
                "(SELECT key FROM snapshots ORDER BY collected_at DESC LIMIT -1 OFFSET ?)",
                (MAX_ROWS,),
            )
            size = db.execute("SELECT COALESCE(SUM(length(CAST(payload AS BLOB))), 0) FROM snapshots").fetchone()[0]
            while size > MAX_PAYLOAD_BYTES:
                oldest = db.execute("SELECT key, length(CAST(payload AS BLOB)) FROM snapshots ORDER BY collected_at LIMIT 1").fetchone()
                db.execute("DELETE FROM snapshots WHERE key = ?", (oldest[0],))
                size -= oldest[1]
        return saved
    except sqlite3.Error as exc:
        raise CacheError(f"Cannot write inventory cache: {type(exc).__name__}") from exc
    finally:
        db.close()


def _get_many(keys: list[str], max_age: float) -> dict[str, tuple[Any, float]]:
    """The entries younger than *max_age* seconds, read in one query: key -> (value, collected_at)."""
    db = _connect(False)
    if db is None or not keys:
        if db is not None:
            db.close()
        return {}
    try:
        now = time.time()
        found: dict[str, tuple[Any, float]] = {}
        with db:
            for start in range(0, len(keys), 500):
                chunk = keys[start:start + 500]
                marks = ",".join("?" * len(chunk))
                for cache_key, payload, collected in db.execute(
                    f"SELECT key, payload, collected_at FROM snapshots WHERE key IN ({marks})", chunk
                ):
                    if 0 <= now - collected < max_age:
                        found[cache_key] = (json.loads(payload), collected)
        return found
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise CacheError(f"Cannot read inventory cache: {type(exc).__name__}") from exc
    finally:
        db.close()


def _bump_generation(delete: bool = False) -> int:
    db = _connect(False)
    if db is None:
        return 0
    try:
        with db:
            db.execute("UPDATE cache_state SET generation = generation + 1")
            return db.execute("DELETE FROM snapshots").rowcount if delete else 0
    except sqlite3.Error as exc:
        raise CacheError(f"Cannot clear inventory cache: {type(exc).__name__}") from exc
    finally:
        db.close()


def clear() -> int:
    return _bump_generation(delete=True)


def timestamp(value: float) -> str:
    return dt.datetime.fromtimestamp(value, dt.UTC).isoformat()


async def get(cache_key: str, ttl: int) -> tuple[Any, float] | None:
    return await asyncio.to_thread(_get, cache_key, ttl)


async def prepare() -> tuple[bool, int, int | None, str | None]:
    """(enabled, ttl, generation, note). A cache that cannot be used never blocks a live read."""
    try:
        enabled, ttl = settings()
        if not enabled:
            return False, ttl, None, None
        await asyncio.to_thread(ensure_ready)
        return True, ttl, await generation(), None
    except CacheError as exc:
        return False, DEFAULT_TTL, None, f"Local cache unavailable ({exc}); data was read live."


async def generation() -> int:
    return await asyncio.to_thread(_generation)


async def put(cache_key: str, data: Any, collected: float, ttl: int, generation: int) -> bool:
    return await asyncio.to_thread(_put, cache_key, data, collected, ttl, generation)


async def get_many(keys: list[str], max_age: float) -> dict[str, tuple[Any, float]]:
    return await asyncio.to_thread(_get_many, keys, max_age)


async def put_many(items: list[tuple[str, Any]], collected: float, ttl: int, generation: int) -> int:
    return await asyncio.to_thread(_put_many, items, collected, ttl, generation)
