# Agent instructions for hpe-proliant-mcp

Public repository. Read this before changing anything.

1. **No credentials, ever.** No real passwords, API keys, tokens, client
   secrets or internal IPs/hostnames in code, tests, docs or examples. Use
   RFC 5737 addresses (`192.0.2.x`) and `changeme`-style placeholders. Real
   credentials live only in `~/.config/proliant-mcp/inventory.ini` or env vars.
2. **Read-only by default.** New tools that change a server (power, UID,
   BIOS, network, firmware) must be clearly separated, annotated
   `destructiveHint`, and opt-in. Follow HPE's iLO Redfish rules (dual
   `Oem.Hpe` PATCH for NIC changes, `@Redfish.Settings` pending pattern).
3. **Workflow:** commit directly to `main` and push. Run `uv run pytest`
   first; it uses a fake iLO and needs no hardware.
4. Layout: `redfish.py` (HTTP/session), `ilo.py` (Redfish -> normalized
   JSON), `server.py` (MCP tools only), `skills/` (agent skills).
