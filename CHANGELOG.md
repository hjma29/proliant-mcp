# Changelog

All notable changes are documented here. Each release on GitHub carries the
notes for its version and a `proliant-mcp.tar.gz` package that `uvx` installs
directly (see the README).

---

## v0.31.1 — 2026-11-28

### Enhancements
- `com_server_usage` accepts up to 180 days (was 90), the longest period COM keeps utilization and energy data for; COM answers HTTP 400 "start-date should be within past 180 days" beyond that. "Which server was busiest this year" is now one call for the last 180 days.

---

## v0.31.0 — 2026-11-28

### New Features
- `com_server_usage`: the busiest COM servers over the last N days (1 to 90, default 30), with average and peak CPU utilization and energy (kWh, share of the fleet, cost) per server, ranked by CPU or energy, plus the fleet's kWh, cost and CO2e. It reads COM's utilization and energy data, which COM collects from each iLO, so "which server is busiest" or "which uses the most power" is one call. Servers COM has no data for (not connected, no subscription) are counted, not ranked. On the lab: dl560-11 averaged 99.2% CPU and used 1,426 kWh (18% of the fleet's 7,803 kWh) over 30 days.

---

## v0.30.1 — 2026-11-27

### Enhancements
- `com_list_servers_table` groups by subscription (`group_by=subscription`: one row per tier, such as `SUBSCRIBED ENHANCED`, with the server count and names), and the `subscription` column now comes with exact totals in `counts.by_subscription`. Before, "which servers have the Enhanced subscription" had no counts, so the assistant counted the 42 rows by hand. On the lab: 15 Enhanced, 25 Standard, 2 Required. COM reports the subscription tier, not the iLO license key.

---

## v0.30.0 — 2026-11-27

### Enhancements
- One COM list tool by default. `com_list_servers_table` (filters, `group_by`, `nic_mode`, exact counts) now answers every fleet list question, and `com_list_servers`, `com_list_gpus`, `com_list_nics`, `com_list_memory` and `com_hardware_report` are hidden, so there are 56 tools instead of 61 and one answer shape for the same question (a 2-port NIC question used to be answered per server in one chat and per adapter in another, depending on which tool the model chose). In tests the assistants answered as many questions right with one tool: Qwen 49 of 58 against 47 with several tools, Claude Sonnet 5.5 27 of 29 (after the v0.29.2 `list_servers` note) against 28 of 29. Set `PROLIANT_MCP_COM_LIST=several` to bring the five tools back; it was `single` (experimental) before, and `single` is now the default.

---

## v0.29.2 — 2026-11-27

### Enhancements
- `list_servers` now says what it covers: `servers` lists only the iLOs added directly, each COM account holds its own larger fleet (use `com_get_overview` and `com_list_servers_table` for its counts, models, health, GPUs, NICs, storage and firmware), and OneView has the `oneview_*` tools. In an A/B test, assistants that called `list_servers` first saw a few iLO servers and kept using the iLO tools (`get_fleet_summary`, `get_storage`, `get_processors`), so "show me servers with GPUs" or "which servers have MegaRAID MR416" was answered from those few servers instead of the whole COM fleet. The note is runtime text, so the tool definitions do not grow.

---

## v0.29.1 — 2026-11-27

### Enhancements
- With `PROLIANT_MCP_COM_LIST=single` the server instructions now say the iLO tools (`get_fleet_summary`, `get_network_adapters`, `get_inventory_snapshot`) cover only the iLO servers added to the install, not the COM fleet, so a question about COM models or hardware goes to `com_list_servers_table` and not to an iLO tool that sees a few servers.

---

## v0.29.0 — 2026-11-27

### New Features
- `com_list_servers_table` can group: `group_by` (`nic`, `gpu`, `memory`, `cpu`, `storage_controller`, `ilo_version`, `model`) returns one row per adapter, GPU, DIMM kind, CPU, controller, iLO firmware version or model with the server count and names. Exact fleet totals come with it in `counts`: servers with GPUs and GPUs by model, adapters and linked ports per mode (so "how many Fibre Channel ports are linked" is `18 of 22` without counting rows).
- New filters `nic_mode` (`InfiniBand`, `FibreChannel`, `Ethernet`) and `linked_only` (a link up) on `com_list_servers_table`: "which servers have InfiniBand adapters" and "how many Fibre Channel ports are linked" are one call.

### Enhancements
- The same question gives the same answer whichever way the assistant asks: a single `nic_ports`, `nic_mode`, `gpu_model` or `cpu_contains` filter with no `columns` (or only name, model, health and its own column, which is what assistants usually send) answers grouped by adapter, GPU or CPU. Before, `com_list_servers_table nic_ports=2` listed every server while `com_list_nics ports=2` grouped by adapter, so two chats answered the same question in two shapes. Pass other columns for one row per server.
- `get_fleet_summary`, `get_network_adapters` and `get_inventory_snapshot` say they cover only directly configured iLO servers and point to the COM tools, because in a test assistants that searched for "InfiniBand" or "server models" landed on them and answered from the 3 iLO servers.

---

## v0.28.1 — 2026-11-27

### Enhancements
- The age line on cached answers is one fixed short line the assistant copies: `Note: data collected 46 min ago, say 'refresh' for the latest.` (the age reads as minutes, hours or days). Before, it was a free-form sentence of two or three lines that varied between answers and clients. Saying "refresh" makes the assistant call again with `refresh=true`.

---

## v0.28.0 — 2026-11-27

### New Features
- `com_list_servers_table` answers most fleet questions in one call: new filters `gpu_model`, `cpu_contains` (for example `AMD`), `nic_ports`, `dimm_maker`, `direct_attached` and `storage_over_tb`, which combine with the existing ones, and a `memory` column. Each filter adds its own column by default and counts servers COM has no inventory for as unknown. On the lab it gives the same answers as the separate tools: 17 servers with Hynix DIMMs, 21 with a 2-port adapter, 18 with direct-attached drives, 7 with AMD CPUs.
- Experimental: `PROLIANT_MCP_COM_LIST=single` exposes only `com_list_servers_table` for COM lists (the other COM list tools are hidden and the server instructions point only at it), to test whether one list tool is easier for a model than several.

---

## v0.27.2 — 2026-11-27

### Bug Fixes
- A DIMM question (for example "which servers have Hynix DIMMs") went to the 51 KB raw `com_list_servers` instead of `com_list_memory`: the server instructions said to use `com_list_servers` for analysis across servers and did not name the memory tool next to it, and the raw tool's description did not mention it. Both now point to `com_list_memory`.

---

## v0.27.1 — 2026-11-26

### Bug Fixes
- `data_age` is now the first field of every cached answer, not the last. OpenCode keeps only about 50 KB of a tool result and cut the end off a 51 KB `com_list_servers` answer, so the model never saw that the data was cached and told the user it was live.

---

## v0.27.0 — 2026-11-26

### New Features
- `com_list_memory`: DIMMs across the COM fleet in one call, from COM's cached inventory. Without arguments it lists the DIMM makers with exact server and DIMM counts; with `manufacturer` (for example `Hynix`, any case) it returns a table of the servers that have them (DIMM count, size, type, speed, part number). Hynix and SK Hynix count as one maker, empty slots are not DIMMs, and servers COM has no memory data for are listed as unknown. Before, "how many servers have Hynix DIMMs" needed a raw `com_api_get` per server.

### Enhancements
- COM hardware summaries now include installed memory (DIMMs by maker, size, type and speed), so the cached hardware entries are read again once after the upgrade.

---

## v0.26.1 — 2026-11-26

### Bug Fixes
- `com_list_nics` with `ports` and the `list_servers` lists for a big fleet now return a real Markdown table (adapter or model, count, servers) instead of plain text lines, which assistants wrapped in a code block that did not wrap in the chat.

---

## v0.26.0 — 2026-11-26

### New Features
- Cache first: answers come from the local cache at any age (inventory and state are kept up to 7 days; they were 1 hour and 5 minutes) and every cached answer tells the assistant to end the reply with when the data was collected and to offer a live refresh (`refresh=true`).
- Automatic refresh: the running server refreshes everything the tools read about every 60 minutes, so cached answers stay recent without a cron job or timer. Several open chats share one refresh (a claim row in the cache) and wake at random times, so each iLO, OneView and COM sees one refresh per hour. A refresh only replaces what it read successfully; a source that is down keeps its last answer with its age. `set_inventory_cache` takes `poll_minutes` (0 turns it off, up to 1440); `get_inventory_cache_status` shows it.

### Enhancements
- A source whose sign-in was refused (wrong iLO or OneView password, COM login) is skipped by the automatic refresh for a day, so it cannot lock the account. `refresh=true` and `proliant-mcp poll` still try it.
- `set_inventory_cache` lifetimes go up to 7 days.

---

## v0.25.0 — 2026-11-26

### New Features
- Almost every read tool now answers from the local SQLite cache: 37 tools across iLO, COM and OneView, plus `list_servers` filters and `find_server`. Measured on a lab of 42 COM servers, 3 iLOs and one OneView appliance, 35 of them took 31.9 s live together and 0.7 s from the cache (12 to 66 ms each), with no request to any source. Examples: `get_ilo_logins` 6.9 s to 42 ms, `get_fleet_event_summary` 2.3 s to 28 ms, `com_get_overview` 1.7 s to 15 ms.
- Two lifetimes: inventory (hardware, firmware, catalogs, lookups) 1 hour, state (health, power, summaries, alerts, jobs, event logs, logins, links) 5 minutes. `set_inventory_cache` takes `state_ttl_seconds`; `get_inventory_cache_status` shows both. Every cached tool takes `refresh=true` to read live, and every answer has `data_age`; the assistant says how old cached state is.
- `proliant-mcp poll` now reads every source the tools read (each iLO with one login, each COM account, each OneView appliance): on the lab one refresh stores 83 entries in 11.4 s. `--interval 5` keeps state answers warm too.

### Enhancements
- State is never served stale: an iLO that cannot be read is reported, not hidden behind an old "OK". Inventory still falls back to the last answer, marked stale. A refused login (iLO 401/403, GreenLake sign-in) is always reported.
- A firmware update (`ilo_firmware_update_start`) drops that server's cached reads before and after the update, so new versions show at once.
- `find_server` looks in the cached lists first and again live when nothing matches, so a server added since is still found.
- Tools that returned a list (`get_firmware_inventory`, `get_storage`, `get_network_adapters`, `get_fleet_summary`, `com_list_groups`, `com_list_firmware_bundles`, `com_list_alerts`, `com_list_jobs`, and the OneView lists) now return `{"result": [...], "data_age": {...}}`; readers of `result` are unaffected.

### Bug Fixes
- The poller stopped reading a server after one of its reads failed; it now carries on with the rest.
- A busy iLO (HTTP 503 at login) or a GreenLake sign-in outage (5xx) was reported as a wrong password or a deleted API client.

---

## v0.24.0 — 2026-11-26

### New Features
- Local inventory cache behind the tools people use. `com_list_gpus`, `com_list_nics`, `com_hardware_report`, `com_list_servers_table` with hardware columns and `list_servers` with a filter now answer from the local SQLite cache when it is fresh (on, 1 hour), reading only what is missing or expired. A cached answer makes no request at all (the GreenLake token is fetched on the first real request). Measured on a lab of 42 COM servers: `com_list_gpus` 1.5 s live (up to 3.7 s on a session's first call) against 16 ms cached. Each answer has `data_age` (`source`, `as_of`, `minutes_old`); from 5 minutes old the assistant says how old the inventory is and offers a refresh in the same reply, and `refresh=true` reads live. When a live read fails, the last answer (kept up to 7 days) comes back marked stale with the error, instead of no answer. Health, power and alerts stay live.
- `proliant-mcp poll` keeps the cache warm: it refreshes COM's server list and hardware, each iLO's model and serial and OneView's server hardware every 30 minutes (`--interval`), or once with `--once` for cron or a timer. On the lab one refresh stores 47 entries in 2.6 s.
- `list_servers` takes `generation` (for example `13` or `Gen13`) and `model` (for example `DL380`): it counts and lists the matching servers across the iLO inventory, every COM account and every OneView appliance in one call, merged by serial number, so a server in both the iLO inventory and COM is counted once and shows both sources. "How many Gen13 servers do I have" took 7 calls before.
- `com_list_nics` takes `ports` (for example `4` for "servers with 4-port NICs"): exact counts and the servers grouped by adapter model with ports up, instead of the full report. In a 3-run test the model wrote the answer in 3.1 s instead of 6.8 s, with the same facts. Long lists show 3 names per model and return the rest in `remaining`, so the full list needs no second call.

### Enhancements
- A filter that matches nothing says what does exist: `list_servers` and `com_list_servers_table` return `available` (server count, generations and models) instead of only "No servers match".
- The inventory cache lifetime is 1 hour by default (was 15 minutes). A cached answer does not log in to the source; `get_inventory_snapshot` still logs in to the iLO every time.

### Bug Fixes
- A COM 403 error did not carry its HTTP status. COM's 403 or 404 for one server's inventory is now kept in the cache as that server's answer, instead of being read again on every call.

---

## v0.23.0 — 2026-11-25

### New Features
- iLO firmware update, the first tools that change a server. `ilo_firmware_list_updates` (read-only) shows each iLO's installed firmware and the newer versions in HPE's firmware repository, with the steps each needs and minutes: a package's `MinimumActiveVersion` can force an older one first (iLO 7 1.21.00 cannot take 1.25.02 directly, it goes through 1.24.00). `ilo_firmware_update_start` opens a local page where the user ticks the servers, picks a version for each, chooses sequential or parallel and reviews the plan with a time estimate per phase; nothing is flashed unless they click Start there. The page and `ilo_firmware_update_status` then show each server's step, phase (downloading, flashing with percent, activating, resetting, verifying) and minutes left. The update runs on its own inside proliant-mcp, so an assistant can hand the polling to a background sub-agent; progress is also saved to a file another proliant-mcp process can read. Measured phase times are saved and later estimates use their median.

---

## v0.22.2 — 2026-11-24

### New
- A `proliant://guide` resource that says which tool answers which kind of question (iLO, COM, OneView) and what COM, iLO and OneView mean here. In a 50-question test with a small local model (Qwen3.6 35B in OpenCode), a third of the failures were the model concluding "I have no access to any server tools" after `list_mcp_resources` came back empty; clients that list resources now find this guide.
- `com_hardware_report` returns `no_inventory`: the servers COM has no hardware data for at all (storage, GPU and NIC). "How many servers have no hardware inventory" had two different answers (5 with no storage data, 18 with no GPU data).

### Changed
- `com_list_alerts` says that it returns 50 alerts by default and that `severity=CRITICAL` answers "which servers have critical alerts" (133 alerts were active, so the 9 critical ones were mostly cut off).

---

## v0.22.1 — 2026-11-24

### Changed
- The local tool-call counters (`get_usage_stats`, `set_usage_stats`, added in v0.22.0 as opt-in) are now on by default, like the inventory cache: nothing is lost before you think to look at it. They hold tool names and timings only, on your computer only; `set_usage_stats` with `enabled: false` turns them off and `clear: true` deletes them.

---

## v0.22.0 — 2026-11-24

### New
- `storage_controller` filter (for example `NS204`, `MR416` or `P408`; any case, dashes and spaces ignored) on `com_list_servers_table` and `com_hardware_report`. "Show me servers with NS204" used to depend on the assistant knowing to read the storage controller column of the whole fleet: in a test of 10 free models and 3 assistants, models that searched by name answered "none" or invented servers. Now it is one call that returns exactly the matching servers with their controllers (10 of 42 in 3 s on a live fleet) and counts `servers_with`, `servers_without` and `servers_unknown` (servers COM has no storage inventory for are unknown, not absent).
- Opt-in local tool-call counters: `set_usage_stats` turns them on or off or deletes them, `get_usage_stats` shows calls, errors and time per tool and client, calls per day and the longest bursts of calls (dozens of calls in a row means the assistant was lost). Opt-in in this version (on by default from v0.22.1). Stored only on your computer in `~/.config/proliant-mcp/usage.sqlite3`: tool name, client name, time, duration, ok or error and result size; never arguments, results, server names or credentials, and nothing is sent anywhere. Rows older than 90 days are removed; counting never makes a tool call fail.

### Changed
- `oneview_list_uplink_sets`: a switch that still advertises the default LLDP name `switch` (Cisco ACI/NX-OS leaves do until they are renamed) is now shown with its chassis MAC, `switch (78:0c:f0:7d:8a:c3) Eth1/5`, and each port has `neighbor_chassis_id`. A new `neighbor_switches` list groups the neighbors into switches by system name and chassis MAC (ignoring the last byte, since ACI leaves use a different MAC on every port), with software version and each connection. OneView reports only what the switch sends, so the real leaf name has to be set on the switch side.

---

## v0.21.0 — 2026-11-23

### New
- `generation` filter (for example `Gen11`, `11` or `GEN_11`) on `com_list_servers_table`, `com_list_servers`, `com_list_nics` and `com_hardware_report`.
- `com_list_servers_table` columns `storage_controllers` (controller model and firmware) and `nic_firmware` (adapter model and firmware). "Gen11 servers with their NIC and storage controller model and firmware version" used to take an assistant 30 steps (the raw server JSON, a script, then one `com_get_server` per server) because no tool had a generation filter or the controller firmware. It is now one call: 26 Gen11 servers in 1.9 s.

---

## v0.20.1 — 2026-11-22

### Fixed
- An assistant that lists tools by name and first sentence (Hermes Agent shows `name: first sentence`, clipped to 80 characters) chose `com_list_servers`, the 35 KB raw JSON tool, for "show me my servers list in com". The hint to use `com_list_servers_table` was at the end of the description and was cut off. Both descriptions now start with the guidance: `com_list_servers` opens with "Raw COM JSON for analysis; to list servers use com_list_servers_table", and `com_list_servers_table` with "List or show the COM servers". A test keeps it that way. The wrong choice cost a 47 s context compression and a 40 s answer.

---

## v0.20.0 — 2026-11-22

### Changed
- `com_list_servers_table` is much quicker for a model to show. The model has to write out whatever the tool returns, at about 100 tokens a second on a single L40S, so the size of the answer is the wait: a 49-server answer written as a full table took 40 s. With no `columns`, a fleet of more than 15 servers now comes back as a compact list: a health summary, a small table of only the servers that are not OK, and the OK servers grouped by model. Every server appears once with its model and health. That is 779 tokens (about 6 s) instead of 1,335 for a plain three-column table. Pass `columns` to get a real table of exactly the columns you ask for.
- The default columns are name, model and health (were six), health is the word without an emoji, and the model drops the repeated "HPE ProLiant Compute" prefix. `columns` adds power, connected, iLO IP, iLO firmware, firmware bundle, and the new hardware columns `gpu`, `storage`, `nics` and `cpu_model`, which read COM's cached inventory only when requested (`unknown` where COM has none, never shown as none). The counts follow the columns shown: always by health, plus power, connected, or GPUs by model when those columns are in the table.

---

## v0.19.0 — 2026-11-22

### New
- `com_list_servers_table` returns the COM servers as a ready-made Markdown table with exact counts (total, by health, power and model, connected to COM). With the raw JSON of `com_list_servers`, an assistant has to write every row itself and miscounts: on a 49-server fleet with a local model, answering "show me my servers list in com" took 122 s, and the counts it gave were wrong. The table result is about 4.7 KB instead of 33 KB and the counts are exact. Default columns are name, model, health, power, connected and iLO IP; `columns` adds more. `com_list_servers` is unchanged, and the server instructions now point assistants to the table tool when the user wants to see the list.
- `com_list_gpus` takes `gpu_model` (for example `H100` or `L40S`, any case): only servers with a matching GPU are listed, with only those GPUs, and the totals count only them. "Which server has the H100s?" now returns one small row instead of the whole 49-server report.

### Changed
- Shorter tool descriptions and server instructions. Every tool's name, description and parameters are sent to the model on every turn (about 11,000 tokens for the 55 tools), so the longest descriptions were rewritten: the tool definitions are about 8% smaller and the server instructions about 23% smaller, with the same guidance. A new test keeps them from growing back.

---

## v0.18.0 — 2026-11-21

### Fixes
- Remote servers (for example an MCP server on a gateway, used from a desktop on another computer): the sign-in page is on the server's own 127.0.0.1, so the user cannot open it. The first sign-in step now tells the assistant not to wait for it: ask the user for their email in the chat and call `com_login_start` again with it. An `@hpe.com` account then needs only the Okta Verify number, relayed in the chat, and no page. Every page link also says how to forward the port with `ssh -L`. Passwords still never go through the chat.

### New
- `PROLIANT_MCP_PAGE_HOST` (off by default) serves the sign-in, password and selection pages on an address of the server's computer that you can reach, for example a Tailscale address, instead of `127.0.0.1`. Set it in the MCP server's environment when proliant-mcp runs on a remote gateway with no SSH. The pages are plain HTTP, so use it only on a private encrypted network. Links then use that address, and no browser is opened on the server.

---

## v0.17.0 — 2026-11-21

### New
- `greenlake_list_api_clients_start` lists your own personal GreenLake API clients, and `greenlake_delete_api_clients_start` deletes the ones you choose (both finish with `greenlake_api_clients_status`). Each GreenLake user can only have a few personal API clients and `com_login` makes one per computer, so old ones pile up. Both sign you in like `com_login_start`; the delete tool shows a page on your own computer with check boxes, and nothing is deleted until you tick the clients and confirm there. The client this computer's saved login uses is flagged and never pre-ticked. When `com_login` cannot create a client, its error now points to these tools.

---

## v0.16.0 — 2026-11-21

### New
- Add a local browser flow for adding multiple iLOs from IPv4 ranges, lists, CSV, INI, text, or Excel files (`add_servers_start`, `add_servers_status`). Review targets before checking logins and choose which results to save; passwords stay out of chat.
- Add a local hardware snapshot cache, **on by default** (15 minutes), with `get_inventory_snapshot` (iLO processors, memory, firmware) and `get_com_inventory_snapshot` (COM hardware inventory). Repeat questions are answered in about 0.1 s instead of about 3 s on a real iLO. Results say whether they came from the cache or live and when they were collected, and `refresh=true` forces a live read. The cache is created on first use in an owner-only folder, never stores passwords or tokens, and falls back to a live read if it cannot be used. Turn it off, change its lifetime or clear it with `set_inventory_cache` / `clear_inventory_cache`. Health, power, alerts and the other existing tools stay live.

### Fixes
- Windows: the cache folder is secured without PowerShell's `Set-Acl`, which cannot load on some machines.

---

## v0.15.12 — 2026-11-20

### Fixes
- Starting a new add (`add_server_start`) now tells the assistant how the previous one really ended, in `previous_add`: `saved`, `failed`, or `stopped` if it was still waiting for the user. Before, an assistant that started a second add before polling the first could tell the user the first server "was not added" when its page had already saved it.
- The sign-in page's workspace step now says the sign-in worked and that one more click is needed. Before, it looked stuck after the Okta Verify approval.

### Docs
- AGENTS.md: Claude Desktop on Windows (MSIX) config paths, and setting `UV_PYTHON_INSTALL_DIR` outside AppData so uv's Python download works there (astral-sh/uv#19622).

---

## v0.15.11 — 2026-11-20

### Fixes
- In chats without pop-ups, the sign-in and add-server page now opens in the user's default browser by itself (Windows, macOS, and Linux desktops; never a console browser; turn off with `PROLIANT_MCP_NO_BROWSER=1`). Each step also tells the assistant to show the link as a clickable link before calling any tool. Before, an assistant could say "the link is on the local server" and wait, leaving the user nothing to click.

---

## v0.15.10 — 2026-11-20

### Fixes
- Adding a server detects whether the address is an iLO or HPE OneView / Synergy Composer (from pages they serve without a login) and uses that, with a note, when the given type is wrong. Before, `add_server_start` assumed iLO when no type was given, so a OneView address failed with "rejected that user name or password" whatever the password. `add_server_start` no longer defaults the type.

---

## v0.15.9 — 2026-11-20

### Improvements
- Smoother sign-in and add-server in chats without pop-ups (Claude Desktop, VS Code Autopilot). One local page now walks the user through every step and moves on by itself: email, the Okta Verify number, the password (again on the same page if it was wrong), a clickable list of workspaces, and the result. The assistant is told to keep polling instead of asking the user to type "I submitted" or "done", and the workspace can still be named in the chat. The page checks required fields itself, not only in the browser.

---

## v0.15.8 — 2026-11-20

### Fixes
- Install commands now pin `uvx --python 3.13`. On a machine without Python, uvx downloaded the newest one (3.15), where a dependency (pyyaml) has no ready-made packages and fails to build, and where uv's Windows Python download can fail with "Missing expected target directory for Python minor version link" (astral-sh/uv#19622). The CI and release install checks use the same pin.

### Improvements
- README and AGENTS.md add the Claude Code command (`claude mcp add --scope user ...`). AGENTS.md explains that a project `.mcp.json` only loads in that folder.

---

## v0.15.7 — 2026-11-19

### Improvements
- The local password page (used by the sign-in and add-server pop-ups and by their no-pop-up links) has an eye button to show or hide the password while typing. The page still runs no other script: only this toggle is allowed, with a new random nonce on each page load.

---

## v0.15.6 — 2026-11-19

### Improvements
- Install steps for VS Code now use the **Copilot Global** MCP config (`~/.copilot/mcp-config.json`, `mcpServers` key) that Copilot's agent runtime reads, instead of VS Code's profile `mcp.json`, which current VS Code marks as legacy and asks to migrate. `AGENTS.md` tells installing agents to write that file, keep other servers in it, and remove an old `proliant` entry from the legacy file.

---

## v0.15.5 — 2026-11-19

### Fixes
- The server failed to start in clients that probe with a 2026-07-28 `server/discover` request and then fall back to the `initialize` handshake on the same connection (seen with Copilot in VS Code): "failed to initialize MCP client: JSON-RPC error: -32022: connection is serving the 2026-07-28 protocol". The connection now switches to the handshake protocol when `initialize` arrives.

---

## v0.15.4 — 2026-11-19

### Fixes
- GreenLake sign-in with a password (gmail.com and other non-HPE accounts) now says the account is locked and how to unlock it when GreenLake refuses the email before the password, instead of a confusing error, and does not ask for the password again.

---

## v0.15.3 — 2026-11-19

### Improvements
- The `list_servers` tool description starts with `[proliant-mcp X.Y.Z]`, so tool lists such as Copilot's `/mcp show proliant` show the running version.

---

## v0.15.2 — 2026-11-19

### Improvements
- On start the server writes `proliant-mcp X.Y.Z started (stdio)` to stderr, so the AI client's MCP log shows which version is running.

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
