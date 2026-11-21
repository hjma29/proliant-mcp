"""Parse local bulk iLO input without reading or writing user files on disk."""

from __future__ import annotations

import csv
import io
import ipaddress
import re
import zipfile
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from proliant_mcp import inventory
from proliant_mcp.inventory import Entry, InventoryError, Parsed

_HOST = re.compile(r"^(?=.{1,253}$)[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?$")
_RANGE = re.compile(r"^(\d+(?:\.\d+){3})\s*[-–]\s*(\d+(?:\.\d+){3}|\d{1,3})$")
_MAX_UNPACKED = 20_000_000


def _address(value: str) -> str:
    value = value.strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if not _HOST.fullmatch(value) or ".." in value or re.fullmatch(r"[\d.]+", value):
            raise InventoryError(f"Invalid IP address or hostname: {value[:80]}")
        return value
    if address.version != 4:
        raise InventoryError("Only IPv4 addresses and hostnames are supported")
    return str(address)


def _text_entries(text: str) -> Parsed:
    entries: list[Entry] = []
    problems: list[str] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        try:
            match = _RANGE.fullmatch(line)
            if match:
                first_text, last_text = match.groups()
                if "." not in last_text:
                    last_text = first_text.rsplit(".", 1)[0] + "." + last_text
                first, last = ipaddress.IPv4Address(first_text), ipaddress.IPv4Address(last_text)
                count = int(last) - int(first) + 1
                if count < 1:
                    raise InventoryError("Range ends before it starts")
                if count > inventory.MAX_ENTRIES - len(entries):
                    raise InventoryError(f"More than {inventory.MAX_ENTRIES} addresses")
                entries.extend(inventory.make_entry("", "ilo", str(ipaddress.IPv4Address(int(first) + n))) for n in range(count))
            elif "/" in line and len(line.split()) == 1:
                network = ipaddress.ip_network(line, strict=False)
                if network.version != 4:
                    raise InventoryError("Only IPv4 networks are supported")
                if network.num_addresses > inventory.MAX_ENTRIES:
                    raise InventoryError(f"Network has more than {inventory.MAX_ENTRIES} addresses")
                hosts = list(network.hosts())
                if len(hosts) > inventory.MAX_ENTRIES - len(entries):
                    raise InventoryError(f"More than {inventory.MAX_ENTRIES} addresses")
                entries.extend(inventory.make_entry("", "ilo", str(host)) for host in hosts)
            else:
                parts = line.split()
                if len(parts) == 1:
                    name, host = "", parts[0]
                elif len(parts) == 2:
                    name, host = parts
                else:
                    raise InventoryError("Use one address, or a name followed by an address")
                entries.append(inventory.make_entry(name, "ilo", _address(host)))
        except (ValueError, InventoryError) as exc:
            problems.append(f"Line {line_number}: {exc}")
        if len(entries) > inventory.MAX_ENTRIES:
            raise InventoryError(f"More than {inventory.MAX_ENTRIES} addresses")
    return Parsed(entries, problems)


def _xlsx(raw: bytes) -> tuple[bytes, str]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if len(archive.infolist()) > 1000 or sum(i.file_size for i in archive.infolist()) > _MAX_UNPACKED:
                raise InventoryError("Excel workbook expands beyond the size limit")
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
        try:
            sheet = workbook.worksheets[0]
            if sheet.max_row and sheet.max_row > inventory.MAX_ENTRIES + 1:
                raise InventoryError(f"Excel sheet exceeds {inventory.MAX_ENTRIES} rows")
            if sheet.max_column and sheet.max_column > 64:
                raise InventoryError("Excel sheet exceeds 64 columns")
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            for row in sheet.iter_rows(max_row=inventory.MAX_ENTRIES + 2, max_col=min(sheet.max_column or 64, 64)):
                if not any(cell.value is not None for cell in row):
                    continue
                if any(cell.data_type == "f" for cell in row):
                    raise InventoryError("Excel formulas are not supported; save values instead")
                writer.writerow(["" if cell.value is None else str(cell.value) for cell in row])
            return output.getvalue().encode("utf-8"), sheet.title
        finally:
            workbook.close()
    except InventoryError:
        raise
    except (zipfile.BadZipFile, InvalidFileException, DefusedXmlException, ParseError, IndexError, KeyError, ValueError, OSError) as exc:
        raise InventoryError(f"Cannot read Excel workbook: {type(exc).__name__}") from exc


def parse_input(raw: bytes, filename: str = "") -> tuple[Parsed, str]:
    if len(raw) > inventory.MAX_FILE_BYTES:
        raise InventoryError("Input exceeds the 1 MB limit")
    if not raw:
        raise InventoryError("Choose a file or enter a list of addresses")
    note = ""
    if raw.startswith(b"PK\x03\x04"):
        raw, sheet = _xlsx(raw)
        note = f"Read the first Excel sheet: {sheet}."
        parsed = inventory.parse_inventory_content(raw, filename or "Excel sheet")
    else:
        text = inventory._decode(raw)
        first = next((line for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#", ";"))), "")
        table = any(d in first for d in (",", ";", "\t")) or inventory._normalize_header(first) in inventory._COLUMNS["host"]
        if filename.lower().endswith(".ini") or first.lstrip().startswith("[") or table:
            parsed = inventory.parse_inventory_content(raw, filename or "list")
        else:
            parsed = _text_entries(text)
    if any(entry.kind != "ilo" for entry in parsed.entries):
        raise InventoryError("This page adds iLOs only; remove non-iLO entries")
    if any(entry.password for entry in parsed.entries):
        raise InventoryError("Remove passwords from the file and enter the shared password on the next page")
    unique: list[Entry] = []
    names: set[str] = set()
    hosts: set[str] = set()
    for entry in parsed.entries:
        host = _address(entry.host)
        if entry.name.lower() in names or host.lower() in hosts:
            parsed.problems.append(f"{entry.name}: duplicate name or address")
            continue
        names.add(entry.name.lower())
        hosts.add(host.lower())
        unique.append(inventory.make_entry(entry.name, "ilo", host, entry.username, verify_ssl=entry.verify_ssl))
    if len(unique) > inventory.MAX_ENTRIES:
        raise InventoryError(f"More than {inventory.MAX_ENTRIES} addresses")
    if not unique:
        detail = "; ".join(parsed.problems[:3])
        raise InventoryError("No usable iLO addresses" + (f": {detail}" if detail else ""))
    return Parsed(unique, parsed.problems), note
