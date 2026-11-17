# Changelog

All notable changes are documented here. Each release on GitHub carries the
notes for its version and a `proliant-mcp.tar.gz` package that `uvx` installs
directly (see README: Install and Update).

---

## v0.4.0 — 2026-11-17

### New Features
- `com_login` tool: sign in to HPE GreenLake from the AI chat with pop-ups (MCP elicitation). It asks for your email. For `@hpe.com` accounts it shows the number to tap in Okta Verify, falling back to the password when push isn't set up. Other accounts (e.g. gmail.com) type their password into a masked field on a one-time sign-in page served by proliant-mcp on `127.0.0.1`, so the password never passes through the AI client or model. If Okta also wants a push, the number pop-up follows. When you have several workspaces it asks which one. The `com_*` tools use the new login immediately, with no restart. Works with clients on MCP 2025-11-25 (live pop-ups) and 2026-07-28 (input-required rounds).
- `proliant-mcp login` (terminal) now signs in non-HPE accounts with their password, the same way as proliant-cli.

### Enhancements
- When no COM account is configured or the COM login fails, the assistant is told to call `com_login`.

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
