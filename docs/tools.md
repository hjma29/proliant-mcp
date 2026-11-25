# Tools

**Start here:** `list_servers` returns every configured iLO, OneView appliance and COM account.
`find_server` tells you which server a name, iLO IP or hostname, serial or OS hostname belongs to,
searching the iLO inventory, every COM account and every OneView appliance at once; for an address
none of them knows, it reads that iLO's login-free identity pages (iLO model, server model, serial).

## iLO (one server)

| Tool | What it returns |
|---|---|
| `get_server_summary` | Model, product ID, serial, power and POST state, health, UID, BIOS and iLO version, CPU/memory totals, and the OS: whether one is running and which (from the HPE agent), boot progress (stuck in POST or BIOS setup), OS loader entries (`Ubuntu`, `VMware ESXi`) and the UEFI boot order |
| `get_fleet_summary` | `get_server_summary` (with OS and boot state) for all configured iLOs in parallel; failures are per server |
| `get_ilo_info` | iLO model and firmware version/date, license, COM connection status, iLO IP/MAC/DHCP/DNS |
| `get_health` | Overall and per-subsystem health, fan and power-supply redundancy |
| `get_firmware_inventory` | Every firmware component and version |
| `get_processors` | CPUs and GPUs |
| `get_memory` | Total, populated/empty slots, each DIMM |
| `get_inventory_snapshot` | Processors, memory and firmware for one or more iLOs, served from the local cache when fresh (on by default, 15 minutes); each category says `cache` or `live` and when it was collected; `refresh=true` forces a live read. Not for health or power |
| `get_storage` | Controllers, drives, RAID volumes |
| `get_network_adapters` | NICs, ports, MACs, link, LLDP neighbor (switch and port) |
| `get_event_log` | Newest IML (hardware), IEL (iLO) or Security Log entries, filter by severity (exact or `min_severity`), time window (`since`/`until`: `7d`, `24h`, a date) and message text; entries with a wrong iLO clock are flagged and listed last; `summary` groups identical messages with counts and first/last time |
| `get_fleet_event_summary` | Significant events for many iLOs in one call: per server, Warning and Critical (by default) IML, IEL or Security Log entries grouped by message, most severe first |
| `get_ilo_logins` | Who logged in to the iLOs, for many servers in one call: logins per day, by type, user and source IP/host, failed logins and lockout alerts; says when a log wrapped before the window start; proliant-mcp's own sessions are counted separately (`include_this_computer` to count them) |
| `redfish_get` | Raw read-only GET of any `/redfish/v1/...` resource |
| `get_usage_stats`, `set_usage_stats` | Local counters of which tools are called (on by default, can be turned off): calls, errors and time per tool and client, calls per day and the longest bursts of calls. Only names and timings, on this computer (see [Configuration](configuration.md)) |
| `get_inventory_cache_status`, `set_inventory_cache`, `clear_inventory_cache` | Check, turn off or change the lifetime of, or clear the local snapshot cache (see [Configuration](configuration.md)); the last two only change files on this computer |

## HPE Compute Ops Management (fleet)

| Tool | What it returns |
|---|---|
| `com_get_overview` | Fleet report: counts by health, power, connection, generation, model; servers needing attention; disconnected servers; soonest subscription expirations; active alerts by severity |
| `com_list_servers` | Every COM server (filter by health, connected, name): serial, model, health, power, iLO, OS, subscription, firmware bundle. `include_hardware` adds CPU, GPU and storage controller / drive columns |
| `com_list_servers_table` | The COM servers ready to show, plus exact counts, for when you want to see the list. With no `columns`, a fleet of more than 15 servers comes back as a compact list (health summary, a table of the servers that are not OK, the OK ones grouped by model), which a model writes out in a few seconds instead of re-typing 49 table rows. With `columns` you get a real table of exactly the columns you ask for: name, model, health, power, connected, iLO IP, iLO firmware, firmware bundle, OS, serial, or GPUs, storage drives, storage controllers (model and firmware), NICs, NIC firmware (model and firmware) and CPU model (these read COM's cached inventory per server, about 2 s for 49 servers, and say `unknown` where COM has none). `generation` (for example `Gen11`) keeps one server generation; `storage_controller` (for example `NS204` or `MR416`, any case, dashes and spaces ignored) keeps the servers that have such a controller, shows their controllers by default and counts servers without it and servers COM has no storage inventory for (unknown, not absent); `com_list_servers`, `com_list_nics` and `com_hardware_report` take it too. The counts match the columns shown and are computed by the server, not guessed |
| `greenlake_list_services` | Every HPE GreenLake service and where each is provisioned in your workspace (Compute Ops Management, Data Services, Aruba Central, ...) |
| `com_hardware_report` | Whole-fleet hardware table, compact: servers with identical hardware grouped, with model, CPU, storage controllers and drives, direct-attached drives, GPUs and NICs (with mode), plus NIC mode totals and `no_inventory` (servers COM has no hardware data for at all); `storage_controller` keeps only servers with that controller (for example `NS204`) |
| `com_list_nics` | NIC / HBA report for the whole fleet in one call: per server each adapter's mode (Ethernet, InfiniBand, FibreChannel), ports up and link speeds; totals by mode (and the most used), by adapter model (cards, ports, ports up, speeds) and link speed |
| `com_list_gpus` | GPU report for the whole fleet in one call (`gpu_model`, e.g. `H100`, lists only servers with that GPU): per server model, iLO firmware, CPU and GPUs, totals by GPU model, servers with no cached inventory |
| `get_com_inventory_snapshot` | COM hardware inventory for the fleet (optionally filtered by name), served from the local cache when fresh; each server says `cache` or `live` and when it was collected |
| `com_get_server` | One server by name, serial, iLO IP or id: per-subsystem health, firmware inventory, CPU and GPUs (with serial numbers), NIC ports (mode, link, speed, MAC), last firmware update, active alerts |
| `com_list_alerts` | Newest alerts, uncleared by default; filter by server and severity |
| `com_list_jobs` | Newest jobs (firmware updates, inventory, OneView sync) with state and result |
| `com_list_groups` | Server groups, members and firmware / iLO-settings compliance |
| `com_list_firmware_bundles` | SPPs and patch bundles available in COM |
| `com_api_get` | Raw read-only GET of any `/compute-ops-mgmt/...` resource (secrets redacted) |

## HPE OneView / Synergy

| Tool | What it returns |
|---|---|
| `oneview_get_appliance` | Appliance model and version, appliance resource health, resource counts, server status, active alerts by severity |
| `oneview_list_server_hardware` | Servers: status, power, profile, enclosure/bay, CPU, memory, ROM, OS, iLO |
| `oneview_get_server_hardware` | One server by name, OS hostname, serial or iLO IP: firmware components and active alerts |
| `oneview_list_server_profiles` | Profiles: assigned hardware, template and compliance, firmware baseline |
| `oneview_list_alerts` | Newest alerts (active by default) with corrective action |
| `oneview_list_enclosures` | Synergy frames: status, bays, Composers, power, PSUs, failed fans |
| `oneview_list_interconnects` | Virtual Connect / switch modules: model, firmware, status, IP |
| `oneview_list_uplink_sets` | Synergy uplink status in one call: each uplink set's status, networks (with VLAN) and ports (module, port, linked, Active / StandBy role, speed, LLDP neighbor switch and port), with issues (no networks, unlinked ports, no module redundancy) and related alerts |
| `oneview_list_firmware_baselines` | SPP / Synergy Service Packs uploaded to OneView |
| `oneview_rest_get` | Raw read-only GET of any `/rest/...` resource (sessions and users blocked, secrets redacted) |

## Setup (pop-ups in the chat)

| Tool | What it does |
|---|---|
| `add_server` | Add one iLO or OneView: pop-up for type, address, name and user name, then a secure page for the password; the login is checked before saving |
| `import_inventory` | Import many from an `.ini` or `.csv` file whose path you type in a pop-up; optional shared password and login checks |
| `com_login` | Sign in to HPE GreenLake with pop-ups (email, then Okta Verify number or a secure password page); creates a GreenLake API client for you (see [Configuration](configuration.md)) |
| `com_login_start`, `com_login_status` | The same sign-in for clients that show no pop-ups: the assistant asks for your email and tells you the Okta Verify number (or gives you the password page link) in the chat |
| `add_server_start`, `add_server_status` | `add_server` for clients that show no pop-ups: the assistant asks for the address in the chat and gives you the secure password page link |
| `add_servers_start`, `add_servers_status` | Add many iLOs from an IPv4 range, a list, or a CSV, INI, text or Excel file on a local page: preview, one shared login, check each iLO, choose what to save |
| `greenlake_list_api_clients_start`, `greenlake_api_clients_status` | List your own personal GreenLake API clients (name, client ID, created, and whether this computer's saved login uses it). Signs you in like `com_login_start`; changes nothing, shows no secrets. Useful when you hit GreenLake's limit on API clients |
| `greenlake_delete_api_clients_start` (then `greenlake_api_clients_status`) | Delete personal GreenLake API clients **you choose**: after sign-in, a page on your own computer lists them with check boxes; you tick, review and confirm there. The AI cannot pick or confirm for you. Marked destructive; anything using a deleted client stops working |

Every other tool is annotated `readOnlyHint`. The setup tools change no server: `add_server`,
`add_server_start` and `import_inventory` only edit the inventory file on this computer, `com_login` also creates a
GreenLake API client, and `greenlake_delete_api_clients_start` deletes the API clients you tick and confirm on its page. None of them takes a password (or, for `import_inventory`, a file path) as an
argument, so the AI cannot see a password or choose which file is read.

The server also publishes one MCP resource, `proliant://guide`: a short text saying which tool answers which kind of question. Assistants that list resources (for example OpenCode's `list_mcp_resources`) find the server and its tools that way.
