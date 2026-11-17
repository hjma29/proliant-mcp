# Changelog

All notable changes are documented here. Each release on GitHub carries the
notes for its version and a `proliant-mcp.tar.gz` package that `uvx` installs
directly (see README: Install and Update).

---

## v0.3.0 — 2026-11-17

### New Features
- `proliant-mcp login`: sign in to HPE GreenLake with your email and an Okta Verify push (tap the number shown), with no API client to create by hand. It creates a temporary GreenLake API client in the chosen workspace, detects the Compute Ops Management region, saves the client to `~/.config/proliant-mcp/com/login.json` (owner-only) and checks that COM answers. Options: `--email`, `--workspace`, `--region`. `proliant-mcp logout` removes the saved login.
- The saved login is used when no COM account is configured in the inventory or environment, ahead of proliant-cli's COM login.

### Enhancements
- A missing or rejected COM login now tells you to run `proliant-mcp login`.

---

## v0.2.0 — 2026-11-17

### New Features
- **HPE Compute Ops Management (COM)**: 8 read-only `com_*` tools: `com_get_overview` (fleet report by health, power, connection, generation and model; servers needing attention; soonest subscription expirations; active alerts), `com_list_servers`, `com_get_server`, `com_list_alerts`, `com_list_jobs`, `com_list_groups`, `com_list_firmware_bundles` and `com_api_get` (raw GET, secrets redacted).
- **HPE OneView / Synergy**: 9 read-only `oneview_*` tools: `oneview_get_appliance`, `oneview_list_server_hardware`, `oneview_get_server_hardware`, `oneview_list_server_profiles`, `oneview_list_alerts`, `oneview_list_enclosures`, `oneview_list_interconnects`, `oneview_list_firmware_baselines` and `oneview_rest_get` (raw GET; sessions, users, console/SSO URLs and keys are blocked, secrets redacted).
- Inventory file: `type = oneview` and `type = com` sections, plus `ONEVIEW_*` and `HPECOM_*` environment variables. Without a COM account configured, the GreenLake API client from proliant-cli's `proliant com login` is reused.
- `list_servers` now returns configured iLOs, OneView appliances and COM accounts.
- New `hpe-compute-fleet` agent skill.

### Enhancements
- Versioned GitHub releases with these notes. Install from the latest release URL so an AI client restart picks up new releases, or pin a version (README: Install, Update).
- `proliant-mcp --check` shows the latest GitHub release and says when an update is available.
- The server reports its version to MCP clients.

---

## v0.1.0 — 2026-11-15

### New Features
- Read-only MCP server for HPE ProLiant iLO 5/6/7 over Redfish: `get_server_summary`, `get_fleet_summary`, `get_ilo_info`, `get_health`, `get_firmware_inventory`, `get_processors` (CPUs and GPUs), `get_memory`, `get_storage`, `get_network_adapters` (with LLDP switch and port), `get_event_log` (IML/IEL) and `redfish_get`.
- No-clone install with `uvx` on Windows, macOS and Linux; no git needed.
- `proliant-mcp --check` self-test (`--connect` also logs in to each iLO).
- Inventory file shared with proliant-cli; Windows editors' UTF-16/BOM files are read correctly.
- `proliant-ilo` agent skill and a Google ADK example agent.
