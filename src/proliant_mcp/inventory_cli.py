"""``proliant-mcp import FILE``: import iLO / OneView entries from a .ini or .csv in a terminal."""

from __future__ import annotations

import asyncio
import getpass
import sys

from proliant_mcp import inventory
from proliant_mcp.probe import probe_many


def run_import(file: str, replace: bool, check: bool = True) -> int:
    try:
        parsed = inventory.parse_inventory_file(file)
        print(f"Found {inventory.summarize(parsed, inventory.existing_names())}.")
        for problem in parsed.problems:
            print(f"  skipped: {problem}")
        entries = parsed.entries
        missing = [e for e in entries if not e.password]
        if missing and sys.stdin.isatty():
            password = getpass.getpass(f"Password shared by the {len(missing)} entries without one (Enter to skip): ")
            if password:
                entries = [inventory.make_entry(e.name, e.kind, e.host, e.username, e.password or password, e.verify_ssl) for e in entries]
        saved = inventory.save_entries(entries, replace_existing=replace)
    except inventory.InventoryError as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    print(f"Saved to {saved.path}: {len(saved.added)} added, {len(saved.replaced)} replaced, {len(saved.skipped)} already present (use --replace to overwrite).")
    if saved.copied_from:
        print(f"Your existing servers were copied from {saved.copied_from} first; that file is unchanged.")
    changed = set(saved.added) | set(saved.replaced)
    if not (check and changed):
        return 0

    from proliant_mcp import server  # tests swap server._transport

    results = asyncio.run(probe_many([e for e in entries if e.name in changed], server._transport))
    for r in results:
        print(f"  {r.name}  {'OK  ' if r.ok else 'FAILED  '}{r.detail}")
    return 0 if all(r.ok for r in results) else 1
