# Agent instructions for proliant-mcp

Public repository. Read this before changing anything.

1. **No credentials, ever.** No real passwords, API keys, tokens, client
   secrets or internal IPs/hostnames in code, tests, docs or examples. Use
   RFC 5737 addresses (`192.0.2.x`) and `changeme`-style placeholders. Real
   credentials live only in `~/.config/proliant-mcp/inventory.ini` (or
   proliant-cli's config) or env vars. Raw-GET tools must pass results
   through `util.redact`.
2. **Read-only by default.** New tools that change a server, a COM group or a
   OneView resource (power, UID, BIOS, network, firmware, profiles) must be clearly separated, annotated
   `destructiveHint`, and opt-in. Follow HPE's iLO Redfish rules (dual
   `Oem.Hpe` PATCH for NIC changes, `@Redfish.Settings` pending pattern).
3. **Workflow:** commit directly to `main` and push. Run `uv run pytest`
   first; it uses a fake iLO, OneView and COM (`tests/fake_*.py`) and needs
   no hardware or GreenLake account.
4. Layout: `config.py` (inventory: iLOs, OneView appliances, COM accounts),
   `redfish.py` + `ilo.py` (iLO), `oneview.py` (OneView client + views),
   `com.py` (COM client + views), `login.py` (`proliant-mcp login`, see below),
   `server.py` (MCP tools only), `skills/`.
   COM/OneView logic is ported from proliant-cli (`proliant/com`,
   `proliant/oneview`); this repo returns JSON for AI agents, not tables.
   Tool names: iLO tools unprefixed, `com_*`, `oneview_*`.
   `proliant-mcp login` is a terminal command, deliberately not an MCP tool: it
   creates a GreenLake API client, so a person runs it. It ports proliant-cli's
   email + Okta Verify push sign-in (undocumented web endpoints; keep the README
   caveat) and stores only the API client in `~/.config/proliant-mcp/com/login.json`.
5. **Releases** (proliant-cli style, no binaries): the version lives only in
   `pyproject.toml`. For each customer-visible change add a line under a
   `## vX.Y.Z — YYYY-MM-DD` section of `CHANGELOG.md` (New Features /
   Enhancements / Bug Fixes). To release: bump `pyproject.toml`, run
   `uv lock`, commit and push, then `git tag vX.Y.Z && git push origin vX.Y.Z`.
   `.github/workflows/release.yml` checks the tag matches `pyproject.toml`,
   runs the tests, publishes the release with that CHANGELOG section and
   `proliant-mcp.tar.gz`, and installs it on Windows/Linux/macOS. Customers
   on the `releases/latest` URL get it on their next AI client restart.
