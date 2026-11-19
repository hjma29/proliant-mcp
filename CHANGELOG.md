# Changelog

All notable changes are documented here. Each release on GitHub carries the
notes for its version and a `proliant-mcp.tar.gz` package that `uvx` installs
directly (see the README).

---

## v0.15.1 — 2026-11-19

### Improvements
- `list_servers` returns the running proliant-mcp version and whether a newer release exists, so you can ask the assistant "which proliant-mcp version is running" (VS Code's `/mcp list` does not show it). The release check runs at most once an hour.

---

## v0.15.0 — 2026-11-19

### Improvements
- `add_server` no longer fails when the chat cannot show pop-ups or cancels them at once (VS Code Autopilot). It continues without pop-ups: one local page asks for the type, address, user name and password, checks the login and saves it. `add_server_start` no longer needs the address.
- `com_login` falls back to the no-pop-up sign-in only when the pop-up was cancelled or is unsupported, not after a real sign-in failure.
- GPU reports (`com_list_gpus`, `com_hardware_report`, `com_list_servers`) name Intel Data Center GPU Max and Flex cards from their PCI ID, instead of iLO's generic "Intel Video Controller".

---

## v0.14.4 — 2026-11-19

### Fixes
- `com_login` no longer fails when the chat cannot show pop-ups or cancels them at once (for example VS Code in autonomous mode, where question tools are answered "user not available"). It continues without pop-ups, and when the email is unknown the user types it on a one-time local page (`enter_email`), so the assistant never has to ask for it. `com_login_start` now accepts no email.

---

## v0.14.3 — 2026-11-19

### Fixes
- `com_login` with an email (or one saved earlier) now continues without pop-ups when the client cannot show them or cancels them at once, instead of failing. Without an email, the error tells the assistant to ask for it in a plain chat message, because question tools can be auto-answered "user not available".

---

## v0.14.2 — 2026-11-19

### Improvements
- README install now has two options: paste one sentence into your AI agent, or install manually in two steps for Windows and macOS. `AGENTS.md` tells agents how to install `uv` when it is missing.

---

## v0.14.1 — 2026-11-19

### Improvements
- The README (and so the PyPI page) is now short: what it is, a two-step install for Windows and macOS, and how to connect servers in the chat. Details moved to `docs/` (tools, configuration, advanced).
- Removed the Google ADK example.

---

## v0.14.0 — 2026-11-18

### New Features
- `greenlake_list_services`: every HPE GreenLake service in the catalog and where each is set up in your workspace, in one call (about 2 s, 3 KB): name, whether it is provisioned, regions with status (`PROVISIONED`, `FAILED`, ...) and description, plus counts. It answers "what services do I have in GreenLake". It uses the same sign-in as the `com_` tools; an API client that may only use Compute Ops Management gets a message telling the assistant to sign in again with `com_login`.

---

## v0.13.0 — 2026-11-18

### New Features
- `com_hardware_report` now reports `total_storage_gb` per configuration (the raw capacity of every drive: RAID, boot and direct-attached) and takes `storage_over_tb`: it keeps only servers with strictly more than that many TB and adds `storage_filter` with how many servers are over, not over or unknown. "How many servers have more than 1 TB of storage" is one call (3 s) with no script; unknown servers (no storage inventory in COM) are counted separately, never as small. `com_get_server` and the storage summaries include `total_gb`, and `drives_without_size` flags drives COM reports without a capacity.

---

## v0.12.0 — 2026-11-18

### New Features
- `oneview_list_uplink_sets`: Synergy / Virtual Connect uplink status in one call (about 2 s and 6 KB, against 176 KB of raw port data). Per uplink set: status, type, networks with VLAN, reachability, and each uplink port with its module, link, Active / StandBy role, speed and LLDP neighbor switch and port. Issues are spelled out (no networks, unlinked ports, all ports on one module) next to the related active alerts.
- `find_server`: which server is this name, iLO IP or hostname, serial or OS hostname? Searches the iLO inventory, every COM account and every OneView appliance (servers and interconnect modules) at once. For an address none of them knows, it reads that iLO's login-free identity pages and reports its iLO model, firmware, hostname, server model and serial, or that it can't be reached from this computer.
- `get_fleet_event_summary`: significant events for many iLOs in one call, from the IML, iLO Event Log or Security Log, Warning and Critical by default, grouped by message with counts and first/last times.

### Enhancements
- `get_server_summary` and `get_fleet_summary` report the OS: a plain `state` (running which OS, OS booted without the HPE agent, stopped in BIOS setup, still in POST, no boot entries), name and version when the HPE agent (AMS) reports them, boot progress, OS loader entries such as `Ubuntu` or `VMware ESXi`, and the UEFI boot order.
- `get_event_log` reads the Security Log (`log="sl"`, iLO 6 and later), filters by `min_severity`, and with `summary` groups identical messages (most severe and most frequent first).
- `get_ilo_logins` no longer counts proliant-mcp's own Redfish sessions as logins: they are reported in `this_computer_logins` (`include_this_computer` counts them again).
- `com_list_nics` reports cards, ports, ports up and link speeds per adapter model, and the most used model by cards and by linked ports.

---

## v0.11.0 — 2026-11-18

### New Features
- `com_list_nics`: network adapters (NICs and FC HBAs) for the whole COM fleet in one call (about 2 s and 14 KB for 49 servers). Per server each adapter's name, mode (Ethernet, InfiniBand or FibreChannel, from each port's network technology, so an NVIDIA/Mellanox VPI card shows the mode it runs in), ports, ports up, link speeds, firmware and health. Totals by mode (adapters, ports, linked ports and the most used mode), by adapter model and by link speed, and the servers COM has no network inventory for (unknown, not absent).

### Enhancements
- `com_hardware_report` lists each configuration's NICs with their mode and port count, and adds NIC mode totals, so one call answers "each server's GPU and NIC config".
- `com_get_server` adds the network adapters with each port's mode, link state, speed and MAC address.
- Adapter names come from whichever of Model, SKU or Name is descriptive (for example `10/25Gb 2-port SFP28 BCM57414 OCP3 Adapter`, not `BCM57414`).

---

## v0.10.0 — 2026-11-18

### New Features
- `get_ilo_logins`: who logged in to the iLOs, for every configured iLO (or the ones named) in one call. It reports logins per day (total and per server, in this computer's time zone or `utc_offset_hours`), and by login type, user and source IP/host. It also lists failed logins and lockout alerts such as "more than 3 unauthorized login attempts". It says when an iLO Event Log no longer reaches back to the start of the window (the log wrapped, so counts are a minimum), and marks the sources that are the computer running proliant-mcp, since the iLO logs its own Redfish sessions as Browser logins.
- `add_server_start` and `add_server_status`: add an iLO or OneView in AI clients that show no pop-ups (Claude Desktop, some Codex versions, harnesses that cancel pop-ups). The assistant asks for the address in the chat, then gives the user the link to the same secure password page on this computer. The login is checked before anything is saved, a wrong password is asked again, and each call returns within 45 seconds.

### Enhancements
- `get_event_log` filters by time (`since` and `until`: `7d`, `24h`, a date or an ISO time) and by message text (`contains`), applied before `limit`.
- `add_server` errors, the "no iLO configured" error and the server instructions point to `add_server_start` when pop-ups do not appear, not to editing files in a terminal.

### Bug Fixes
- `get_event_log` no longer lists entries stamped while the iLO clock was wrong (`0000-00-00`, 2001 or 2039 dates after an RTC reset) as the newest. They are marked `time_invalid`, listed last, and left out of time filters.

---

## v0.9.0 — 2026-11-18

### New Features
- `com_hardware_report`: a whole-fleet hardware table in one compact call (about 3 s and 8 KB for 49 servers, against 50 KB for `com_list_servers` with `include_hardware`, which is too large for an assistant to read in one piece). Servers with identical hardware are grouped into one configuration listing their names, model, CPU, storage controllers with the drives behind them, direct-attached drives and GPUs, plus which servers COM has no storage or GPU data for (unknown, not absent). It answers "table of each server's storage controller and GPU config" directly.

---

## v0.8.0 — 2026-11-18

### New Features
- Storage controllers and drives in the COM fleet data: `com_list_servers` with `include_hardware` (and `com_get_server`, with drive serial numbers) now return each server's RAID and boot controllers (model, firmware, health) with the drives behind them (e.g. `6 x 960GB SSD`), and the drives attached directly to the CPU. One call builds a fleet table of storage and GPU configuration (about 3 s for 49 servers). Classification follows proliant-cli: HPE Storage Ids `DE*` are controllers, `DA*` direct-attached; empty bays are ignored.

### Bug Fixes
- Servers whose full COM inventory was never collected but whose storage was (common on Gen10 and Synergy) are no longer reported as having no inventory: `cpu_gpu_known` and `storage.known` say which parts are available, and `com_list_gpus` lists only servers with unknown GPUs as unavailable.

---

## v0.7.0 — 2026-11-18

### New Features
- `com_login_start` and `com_login_status`: sign in to HPE GreenLake in clients that show no MCP pop-ups (Claude Desktop, some Codex versions, harnesses that cancel pop-ups). Each call returns the next step for the assistant to tell the user in the chat: the number to tap in Okta Verify, a link to the one-time password page on the computer running the server, or the workspaces to choose from. Calls return within 45 seconds, inside Codex's default 60 s tool timeout.

### Enhancements
- When a COM login fails (HTTP 401, for example after the API client was deleted in GreenLake), is missing, or the workspace has no COM, the error and the server instructions tell the assistant to sign the user in right away in the chat with `com_login` or `com_login_start`, not to send them to a browser or terminal.
- `com_login` errors suggest `com_login_start` when no pop-up was shown.

---

## v0.6.2 — 2026-11-17

### Bug Fixes
- Compute Ops Management no longer fails with "HTTP 404 Routing details for the customer not found" when the login's region has no COM for its workspace (for example a proliant-cli login last used on another workspace). The server checks the other regions with the same login and uses the one where the workspace has the most servers. If no region has COM, the error names the workspace and the login file in use and says to sign in again with `com_login`.
- `com_login` and `proliant-mcp login` choose the region where the workspace's servers are, not us-west by default, when COM is set up in several regions.

### Enhancements
- `proliant-mcp --check --connect` shows, per COM account, the region actually used, the workspace and which login file it came from; `list_servers` shows the region in use.

---

## v0.6.1 — 2026-11-17

### Enhancements
- Published on PyPI: install and run with `uvx proliant-mcp@latest`, pin with `uvx proliant-mcp@0.6.1`. Each release is uploaded from the GitHub release workflow by trusted publishing (no token stored), and the release check installs it from PyPI on Windows, Linux and macOS. The GitHub release download still works for networks that cannot reach PyPI.
- Releases now include a wheel, so installs no longer build the package.
- README links work on the PyPI project page.

---

## v0.6.0 — 2026-11-17

### New Features
- `add_server` tool: add one iLO or OneView to your inventory from the AI chat. A pop-up asks for the type, address, name and user name, then a one-time page on your own computer (`127.0.0.1`) asks for the password in a masked field, so it never passes through the AI client or model. The login is checked against the server first (a wrong password is asked again, up to three times; an unreachable address saves nothing) and the tools use the server straight away.
- `import_inventory` tool and `proliant-mcp import FILE` command: import many iLO and OneView entries from an `.ini` or `.csv` file (Excel: Save As > CSV). The user types the file's path in a pop-up; the AI cannot choose the file. Column names such as `IP Address`, `Server Name`, `User` and `Pass` are recognised, `;` and tab separators and Excel's ANSI/UTF-16 encodings are read, and invalid rows are reported and skipped. It confirms what it found, asks once for a password shared by entries that have none, and optionally checks each login. Existing entries are kept unless "Replace" is ticked (`--replace`).
- The inventory file is written owner-only (`chmod 600`, `icacls` on Windows), edited section by section so comments and other entries are untouched, and a proliant-cli inventory is copied, never modified.

### Enhancements
- A wrong iLO or OneView password is now told apart from an unreachable server, so `add_server` and the imports can say which.
- When no iLO or OneView is configured, the error tells the assistant to use `add_server` or `import_inventory`.

---

## v0.5.0 — 2026-11-17

### New Features
- `com_list_gpus`: GPU report for the whole COM fleet in one call (about 3 s for 49 servers): per server model, generation, iLO firmware, health, CPU and GPUs (name, vendor, part number, count, health), totals by GPU model, and the servers COM has no cached hardware inventory for (their GPUs are unknown, not absent).
- `com_list_servers` takes `include_hardware` to add CPU and GPU columns to the fleet table. `com_get_server` now includes the CPU and GPUs with serial numbers.

### Bug Fixes
- Fleet questions such as "show servers with iLO firmware, model and GPU" no longer need one `com_get_server` or raw `com_api_get` call per server and pages of raw inventory JSON; the tool descriptions and server instructions point assistants at the fleet tools.
- `com_login` API client names now carry a per-install ID, so two installs that share a hostname (for example WSL and its Windows host) no longer delete each other's client when signing in again, which made the other install fail with HTTP 401. Existing clients keep working; sign in again if one was already removed.

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
