"""Editing the inventory file: add or replace iLO and OneView entries, and import them from
an ``.ini`` or ``.csv`` file (Excel: File > Save As > CSV).

Nothing here touches the network; ``probe.py`` checks logins. Passwords are never put in
error messages, notes or results.
"""

from __future__ import annotations

import configparser
import contextlib
import csv
import io
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from proliant_mcp.config import _home_config, _read_text, _truthy

MAX_FILE_BYTES = 1_000_000
MAX_ENTRIES = 500
KINDS = ("ilo", "oneview")


class InventoryError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str
    host: str
    username: str = "Administrator"
    password: str = field(default="", repr=False)
    verify_ssl: bool = False


# ── validation ────────────────────────────────────────────────────────────────

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def make_entry(name: str, kind: str, host: str, username: str = "", password: str = "", verify_ssl: bool = False) -> Entry:
    """Validate and normalize one entry; raises InventoryError with a user-facing reason."""
    host = re.sub(r"^https?://", "", (host or "").strip(), flags=re.IGNORECASE).split("/")[0].strip()
    if not host:
        raise InventoryError("no host (the iLO or OneView IP address or name)")
    if len(host) > 253 or re.search(r"[\s\"'<>;,]", host):
        raise InventoryError(f"'{host[:60]}' is not a valid host")
    name = (name or "").strip() or host
    if len(name) > 64 or _CONTROL.search(name) or "[" in name or "]" in name:
        raise InventoryError(f"'{name[:60]}' is not a valid name (max 64 characters, no brackets)")
    if name.lower() == "defaults":
        raise InventoryError("'defaults' is reserved; choose another name")
    if kind not in KINDS:
        raise InventoryError(f"type must be ilo or oneview, not '{kind}'")
    username = (username or "").strip() or "Administrator"
    if _CONTROL.search(username):
        raise InventoryError("the username has an invalid character")
    if _CONTROL.search(password):
        raise InventoryError("the password has a line break or other control character")
    if password != password.strip():
        raise InventoryError("passwords starting or ending with a space cannot be stored in the inventory file (use environment variables)")
    return Entry(name, kind, host, username, password, verify_ssl)


def render_section(entry: Entry) -> str:
    lines = [f"[{entry.name}]", f"type = {entry.kind}", f"host = {entry.host}", f"username = {entry.username}"]
    if entry.password:
        lines.append(f"password = {entry.password}")
    if entry.verify_ssl:
        lines.append("verify_ssl = true")
    return "\n".join(lines) + "\n"


# ── the inventory file ────────────────────────────────────────────────────────

_HEADER = re.compile(r"^\[([^\]]+)\]")


def _is_filler(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith(("#", ";"))


def upsert_sections(text: str, entries: list[Entry], replace_existing: bool = True) -> tuple[str, list[str], list[str], list[str]]:
    """Add or replace one ini section per entry, leaving everything else byte for byte.

    Returns (new text, added names, replaced names, skipped names)."""
    lines = text.splitlines(keepends=True)
    added: list[str] = []
    replaced: list[str] = []
    skipped: list[str] = []
    for entry in entries:
        start = end = None
        for i, line in enumerate(lines):
            match = _HEADER.match(line)
            if not match:
                continue
            if start is not None:
                end = i
                break
            if match.group(1).strip().lower() == entry.name.lower():
                start = i
        section = render_section(entry)
        if start is None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            if lines and lines[-1].strip():
                lines.append("\n")
            lines.append(section)
            added.append(entry.name)
            continue
        if not replace_existing:
            skipped.append(entry.name)
            continue
        end = len(lines) if end is None else end
        while end > start + 1 and _is_filler(lines[end - 1]):  # keep blank lines and comments that belong to the next section
            end -= 1
        lines[start:end] = [section]
        replaced.append(entry.name)
    return "".join(lines), added, replaced, skipped


def restrict_to_owner(path: Path) -> None:
    """Best effort: only the current user can read the file (it holds passwords)."""
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    user = os.environ.get("USERNAME")
    if sys.platform == "win32" and user:
        with contextlib.suppress(Exception):
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                capture_output=True,
                timeout=15,
                check=False,
            )


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    restrict_to_owner(tmp)
    os.replace(tmp, path)


def inventory_target() -> tuple[Path, Path | None]:
    """(file to write, existing file to start from when it differs).

    The file the server already reads when it is ours or named by PROLIANT_MCP_CONFIG. A
    proliant-cli inventory is never edited: its content is copied into our file first, so the
    servers already known to the MCP server stay and proliant-cli is unchanged."""
    explicit = os.environ.get("PROLIANT_MCP_CONFIG")
    if explicit:
        return Path(explicit).expanduser(), None
    ours = _home_config("proliant-mcp", "inventory.ini")
    if ours.is_file():
        return ours, None
    cli = _home_config("proliant-cli", "inventory.ini")
    return ours, cli if cli.is_file() else None


@dataclass
class SaveResult:
    path: Path
    added: list[str]
    replaced: list[str]
    skipped: list[str]
    copied_from: Path | None = None


def save_entries(entries: list[Entry], replace_existing: bool = True) -> SaveResult:
    path, copy_from = inventory_target()
    source = path if path.is_file() else copy_from
    try:
        text = _read_text(source) if source else ""
    except (OSError, UnicodeDecodeError) as exc:
        raise InventoryError(f"Cannot read {source}: {exc.__class__.__name__}") from exc
    new_text, added, replaced, skipped = upsert_sections(text, entries, replace_existing)
    changed = bool(added or replaced)
    copied_from = copy_from if changed and not path.is_file() else None
    if changed:
        write_text(path, new_text)
    return SaveResult(path, added, replaced, skipped, copied_from)


# ── importing a file ──────────────────────────────────────────────────────────

_COLUMNS = {
    "name": ("name", "server", "server name", "display name", "label"),
    "host": ("host", "ip", "ip address", "address", "ilo ip", "ilo host", "ilo address", "ilo", "hostname", "fqdn", "management ip", "bmc ip"),
    "type": ("type", "kind", "product"),
    "username": ("username", "user", "user name", "login", "account"),
    "password": ("password", "pass", "passwd", "pwd"),
    "verify_ssl": ("verify ssl", "verify tls", "verify certificate", "verify"),
}


def _normalize_header(value: str) -> str:
    return re.sub(r"[\s_\-]+", " ", (value or "").strip().lower())


def _kind_of(raw: str) -> str:
    value = _normalize_header(raw)
    if not value or value.startswith(("ilo", "bmc")):
        return "ilo"
    if any(word in value for word in ("oneview", "composer", "synergy")):
        return "oneview"
    if value.startswith("com") or "greenlake" in value:
        raise InventoryError("Compute Ops Management accounts are not imported; use the com_login tool")
    raise InventoryError(f"type must be ilo or oneview, not '{raw[:30]}'")


def _decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")  # Excel's "CSV (Comma delimited)" on Windows


@dataclass
class Parsed:
    entries: list[Entry]
    problems: list[str]


def _from_ini(text: str, name: str) -> Parsed:
    parser = configparser.ConfigParser(default_section="defaults", interpolation=None)
    try:
        parser.read_string(text, source=name)
    except configparser.ParsingError as exc:
        line = exc.errors[0][0] if getattr(exc, "errors", None) else "?"
        raise InventoryError(f"{name} is not a valid ini file (check line {line})") from None
    except configparser.Error as exc:
        raise InventoryError(f"{name} is not a valid ini file ({exc.__class__.__name__})") from None
    entries: list[Entry] = []
    problems: list[str] = []
    for section in parser.sections():
        sec = parser[section]
        try:
            kind_raw = sec.get("type", "oneview" if section.lower() == "oneview" else "ilo")
            if _normalize_header(kind_raw).startswith("com"):
                raise InventoryError("Compute Ops Management accounts are not imported; use the com_login tool")
            entries.append(make_entry(section, _kind_of(kind_raw), sec.get("host", ""), sec.get("username", ""), sec.get("password", ""),
                                      _truthy(sec.get("verify_ssl"))))
        except InventoryError as exc:
            problems.append(f"[{section[:40]}]: {exc}")
    return Parsed(entries, problems)


def _from_csv(text: str, name: str) -> Parsed:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise InventoryError(f"{name} is empty")
    delimiter = max(",;\t", key=lambda d: lines[0].count(d))
    if lines[0].count(delimiter) == 0 and _normalize_header(lines[0]) not in _COLUMNS["host"]:
        raise InventoryError(f"{name} does not look like a table with a header row (name, type, host, username, password)")
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    header = [_normalize_header(h) for h in next(reader)]
    index: dict[str, int] = {}
    for field_name, aliases in _COLUMNS.items():
        for alias in aliases:
            if alias in header:
                index[field_name] = header.index(alias)
                break
    if "host" not in index:
        raise InventoryError(f"{name} has no host column; found columns: {', '.join(h for h in header if h)[:200]}")
    entries: list[Entry] = []
    problems: list[str] = []
    for number, row in enumerate(reader, start=2):
        def cell(key: str, row: list[str] = row) -> str:
            i = index.get(key)
            return row[i].strip() if i is not None and i < len(row) else ""

        try:
            entries.append(make_entry(cell("name"), _kind_of(cell("type")), cell("host"), cell("username"), cell("password"), _truthy(cell("verify_ssl"))))
        except InventoryError as exc:
            problems.append(f"row {number}: {exc}")
    return Parsed(entries, problems)


def parse_inventory_file(path_text: str) -> Parsed:
    """Entries from an .ini or .csv file (the format is detected from the content)."""
    path = Path(path_text.strip().strip("\"'")).expanduser()
    try:
        if not path.is_file():
            raise InventoryError(f"File not found: {path}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise InventoryError(f"{path.name} is larger than {MAX_FILE_BYTES // 1000} KB; is it really an inventory?")
        raw = path.read_bytes()
    except OSError as exc:
        raise InventoryError(f"Cannot read {path}: {exc.__class__.__name__}") from exc
    return parse_inventory_content(raw, path.name)


def parse_inventory_content(raw: bytes, name: str) -> Parsed:
    """Parse inventory bytes without creating a temporary file."""
    if len(raw) > MAX_FILE_BYTES:
        raise InventoryError(f"{name} is larger than {MAX_FILE_BYTES // 1000} KB; is it really an inventory?")
    if raw.startswith((b"PK\x03\x04", b"\xd0\xcf\x11\xe0")):
        raise InventoryError(f"{name} is an Excel workbook. In Excel use File > Save As > CSV, then import the .csv")
    text = _decode(raw)
    parsed = _from_ini(text, name) if re.search(r"^\s*\[[^\]]+\]\s*$", text, re.MULTILINE) else _from_csv(text, name)

    seen: set[str] = set()
    unique: list[Entry] = []
    for entry in parsed.entries:
        if entry.name.lower() in seen:
            parsed.problems.append(f"{entry.name}: listed more than once; the first one is used")
            continue
        seen.add(entry.name.lower())
        unique.append(entry)
    if len(unique) > MAX_ENTRIES:
        raise InventoryError(f"{name} has {len(unique)} entries; the limit is {MAX_ENTRIES}")
    if not unique:
        detail = "; ".join(parsed.problems[:3])
        raise InventoryError(f"No usable servers in {name}" + (f" ({detail})" if detail else ""))
    return Parsed(unique, parsed.problems)


def with_password(entries: list[Entry], password: str) -> list[Entry]:
    return [Entry(e.name, e.kind, e.host, e.username, e.password or password, e.verify_ssl) for e in entries]


def _names(names: list[str], limit: int = 40) -> str:
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")


def summarize(parsed: Parsed, existing: set[str]) -> str:
    ilo = sum(1 for e in parsed.entries if e.kind == "ilo")
    ov = len(parsed.entries) - ilo
    present = [e.name for e in parsed.entries if e.name.lower() in existing]
    text = f"{len(parsed.entries)} entries ({ilo} iLO, {ov} OneView)"
    if present:
        text += f"; {len(present)} already in your inventory ({_names(present, 8)})"
    if parsed.problems:
        text += f"; {len(parsed.problems)} skipped as invalid"
    return text


def existing_names() -> set[str]:
    """Names already in the inventory file that save_entries would write to."""
    path, copy_from = inventory_target()
    source = path if path.is_file() else copy_from
    if not source:
        return set()
    try:
        return {m.group(1).strip().lower() for line in _read_text(source).splitlines() if (m := _HEADER.match(line))}
    except (OSError, UnicodeDecodeError):
        return set()
