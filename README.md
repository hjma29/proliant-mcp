# proliant-mcp

[![PyPI](https://img.shields.io/pypi/v/proliant-mcp)](https://pypi.org/project/proliant-mcp/)

**Install / run:** `uvx proliant-mcp@latest` (from [PyPI](https://pypi.org/project/proliant-mcp/); needs only
[uv](https://docs.astral.sh/uv/), no git). MCP client config: `"command": "uvx", "args": ["proliant-mcp@latest"]`.
Supports **HPE iLO, Compute Ops Management and OneView**, all read-only. Details: [Install](#install).

An [MCP](https://modelcontextprotocol.io) server for **HPE compute**, built with
[FastMCP](https://gofastmcp.com). It gives AI assistants and agents (GitHub
Copilot, Claude, Google ADK, ...) live, read-only access to three HPE
management sources:

| Source | What it is | Scope | Tools |
|---|---|---|---|
| **HPE iLO** (Redfish) | The management processor in every ProLiant server | One server, deepest detail | unprefixed (`get_server_summary`, ...) |
| **HPE Compute Ops Management** (COM) | HPE GreenLake cloud service for ProLiant fleets | Whole fleet, any site | `com_*` |
| **HPE OneView** | On-prem appliance for ProLiant / Synergy (Synergy Composer) | Servers, profiles, frames, interconnects | `oneview_*` |

Ask things like *"which servers in COM are critical and why"*, *"any active
OneView alerts on the Synergy frame"*, or *"what switch port is dl345-gen12's
NIC on"*, and the assistant answers from live data. Every tool returns
compact JSON shaped for an AI model, not a human table.

Tested against iLO 5/6/7 (Synergy 480 Gen10, DL380 Gen11, DL325/DL345 Gen12),
Synergy Composer2 running OneView 11.10, and COM (`us-west`). The COM and OneView
logic comes from [proliant-cli](https://github.com/hjma29/proliant-cli), the
human-facing CLI for the same three sources.

> Status: **v0.6, read-only.** Nothing in this version changes a server. See [CHANGELOG.md](https://github.com/hjma29/proliant-mcp/blob/main/CHANGELOG.md).

## Tools

**Start here:** `list_servers` returns every configured iLO, OneView appliance and COM account.

### iLO (one server)

| Tool | What it returns |
|---|---|
| `get_server_summary` | Model, product ID, serial, power and POST state, health, UID, BIOS and iLO version, CPU/memory totals |
| `get_fleet_summary` | `get_server_summary` for all configured iLOs in parallel; failures are per server |
| `get_ilo_info` | iLO model and firmware version/date, license, COM connection status, iLO IP/MAC/DHCP/DNS |
| `get_health` | Overall and per-subsystem health, fan and power-supply redundancy |
| `get_firmware_inventory` | Every firmware component and version |
| `get_processors` | CPUs and GPUs |
| `get_memory` | Total, populated/empty slots, each DIMM |
| `get_storage` | Controllers, drives, RAID volumes |
| `get_network_adapters` | NICs, ports, MACs, link, LLDP neighbor (switch and port) |
| `get_event_log` | Newest IML (hardware) or IEL (iLO) entries, filter by severity, time window (`since`/`until`: `7d`, `24h`, a date) and message text; entries with a wrong iLO clock are flagged and listed last |
| `get_ilo_logins` | Who logged in to the iLOs, for many servers in one call: logins per day, by type, user and source IP/host, failed logins and lockout alerts; says when a log wrapped before the window start and marks proliant-mcp's own sessions |
| `redfish_get` | Raw read-only GET of any `/redfish/v1/...` resource |

### HPE Compute Ops Management (fleet)

| Tool | What it returns |
|---|---|
| `com_get_overview` | Fleet report: counts by health, power, connection, generation, model; servers needing attention; disconnected servers; soonest subscription expirations; active alerts by severity |
| `com_list_servers` | Every COM server (filter by health, connected, name): serial, model, health, power, iLO, OS, subscription, firmware bundle. `include_hardware` adds CPU, GPU and storage controller / drive columns |
| `com_hardware_report` | Whole-fleet hardware table, compact: servers with identical hardware grouped, with model, CPU, storage controllers and drives, direct-attached drives and GPUs |
| `com_list_gpus` | GPU report for the whole fleet in one call: per server model, iLO firmware, CPU and GPUs, totals by GPU model, servers with no cached inventory |
| `com_get_server` | One server by name, serial, iLO IP or id: per-subsystem health, firmware inventory, CPU and GPUs (with serial numbers), last firmware update, active alerts |
| `com_list_alerts` | Newest alerts, uncleared by default; filter by server and severity |
| `com_list_jobs` | Newest jobs (firmware updates, inventory, OneView sync) with state and result |
| `com_list_groups` | Server groups, members and firmware / iLO-settings compliance |
| `com_list_firmware_bundles` | SPPs and patch bundles available in COM |
| `com_api_get` | Raw read-only GET of any `/compute-ops-mgmt/...` resource (secrets redacted) |

### HPE OneView / Synergy

| Tool | What it returns |
|---|---|
| `oneview_get_appliance` | Appliance model and version, appliance resource health, resource counts, server status, active alerts by severity |
| `oneview_list_server_hardware` | Servers: status, power, profile, enclosure/bay, CPU, memory, ROM, OS, iLO |
| `oneview_get_server_hardware` | One server by name, OS hostname, serial or iLO IP: firmware components and active alerts |
| `oneview_list_server_profiles` | Profiles: assigned hardware, template and compliance, firmware baseline |
| `oneview_list_alerts` | Newest alerts (active by default) with corrective action |
| `oneview_list_enclosures` | Synergy frames: status, bays, Composers, power, PSUs, failed fans |
| `oneview_list_interconnects` | Virtual Connect / switch modules: model, firmware, status, IP |
| `oneview_list_firmware_baselines` | SPP / Synergy Service Packs uploaded to OneView |
| `oneview_rest_get` | Raw read-only GET of any `/rest/...` resource (sessions and users blocked, secrets redacted) |

### Setup (pop-ups in the chat)

| Tool | What it does |
|---|---|
| `add_server` | Add one iLO or OneView: pop-up for type, address, name and user name, then a secure page for the password; the login is checked before saving |
| `import_inventory` | Import many from an `.ini` or `.csv` file whose path you type in a pop-up; optional shared password and login checks |
| `com_login` | Sign in to HPE GreenLake with pop-ups (email, then Okta Verify number or a secure password page); creates a GreenLake API client for you (see Configure > HPE Compute Ops Management) |
| `com_login_start`, `com_login_status` | The same sign-in for clients that show no pop-ups: the assistant asks for your email and tells you the Okta Verify number (or gives you the password page link) in the chat |
| `add_server_start`, `add_server_status` | `add_server` for clients that show no pop-ups: the assistant asks for the address in the chat and gives you the secure password page link |

Every other tool is annotated `readOnlyHint`. The setup tools change no server: `add_server`,
`add_server_start` and `import_inventory` only edit the inventory file on this computer, and `com_login` also creates a
GreenLake API client. None of them takes a password (or, for `import_inventory`, a file path) as an
argument, so the AI cannot see a password or choose which file is read.

## Install

The only prerequisite is [uv](https://docs.astral.sh/uv/), which also fetches
Python 3.12+ when needed. **No git, no admin rights and no clone are required**;
CI proves this on clean Windows, Linux and macOS machines.

### Windows (PowerShell)

```powershell
# 1. Install uv for your user only (no admin). Or: winget install --id astral-sh.uv
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Open a **new** PowerShell window, then check that it installs and starts (should print `proliant-mcp <version>: 39 tools` and `(up to date)`):

```powershell
uvx proliant-mcp@latest --check
```

### macOS / Linux

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uvx proliant-mcp@latest --check
```

Configure at least one iLO, OneView appliance or COM account (next section), then check that each one accepts the login:

```bash
uvx proliant-mcp@latest --check --connect
```

It prints each iLO (model, iLO version), OneView appliance (model, software version) and
COM account (server count), or the reason a login failed.

### Add it to your AI client (stdio)

VS Code / GitHub Copilot (**MCP: Open User Configuration**), Claude Desktop
(**Settings > Developer > Edit Config**) and other clients all take the same command:

```json
{"servers": {"proliant": {"type": "stdio", "command": "uvx", "args": ["proliant-mcp@latest"]}}}
```

Claude Desktop uses the top-level key `mcpServers` instead of `servers`. Credentials come from
your inventory file (next section), so none go in this JSON. If the client cannot find `uvx`,
restart it after installing uv, or use the full path (`%USERPROFILE%\.local\bin\uvx.exe`).

### Update

`proliant-mcp@latest` makes `uvx` check [PyPI](https://pypi.org/project/proliant-mcp/) for a new
release each time the AI client starts the server (a fraction of a second when nothing changed).
To update, **restart the AI client** (in VS Code: **MCP: List Servers > proliant > Restart**).
Plain `proliant-mcp` without `@latest` keeps using the copy `uvx` cached first.

- **What's new:** [CHANGELOG.md](https://github.com/hjma29/proliant-mcp/blob/main/CHANGELOG.md) and the
  [releases page](https://github.com/hjma29/proliant-mcp/releases).
- **Which version am I on:** `--check` prints it, plus the latest release and
  `UPDATE AVAILABLE` if you are behind.
- **Pin a version** (no surprise changes; you upgrade by editing the version):
  `proliant-mcp@0.6.1` in place of `proliant-mcp@latest`.
- **No access to PyPI** (some corporate networks): every release is also attached to GitHub.
  Use `"args": ["--refresh-package", "proliant-mcp", "--from", "https://github.com/hjma29/proliant-mcp/releases/latest/download/proliant-mcp.tar.gz", "proliant-mcp"]`.
- **Unreleased changes** from `main`:
  `"args": ["--from", "https://github.com/hjma29/proliant-mcp/archive/refs/heads/main.zip", "proliant-mcp"]`
  (picks up every push on restart; may include unfinished work).

To hack on it: `git clone https://github.com/hjma29/proliant-mcp && cd proliant-mcp && uv sync && uv run pytest`.

## Configure

Credentials are read from the machine running the server, never from this
repository, and never sent to the AI model. That machine must reach each iLO
and OneView appliance over HTTPS (443), and `*.api.greenlake.hpe.com` /
`sso.common.cloud.hpe.com` for COM. Configure any mix of the three.

### Inventory file

**Add servers from the chat** (easiest): ask your assistant to *"add an iLO"* or *"add a OneView"*. It
calls the `add_server` tool, which shows a pop-up for the type, address, name and user name, then
opens a secure page on your own computer (`127.0.0.1`) for the password. The password goes straight
to proliant-mcp, never through the AI client or model. It is **checked against the server before
anything is saved**, so a wrong password or address is caught at once. The tools use the new server
immediately, no restart. In AI clients without pop-ups (Claude Desktop, some Codex versions) the
assistant asks for the address in the chat and uses `add_server_start` / `add_server_status`
instead: it gives you the same secure page link, and tells you when the server is saved.

**Import a list** (many servers): ask your assistant to *"import my servers from a file"*. The
`import_inventory` tool pops up for the **path** of an `.ini` or `.csv` file (the AI cannot choose the
file; you type it), shows what it found, and optionally asks once for a password shared by entries
that have none, then checks each login. The file stays where it is. For a spreadsheet, use **File >
Save As > CSV** in Excel; the columns are `name, type, host, username, password`
([example](https://github.com/hjma29/proliant-mcp/blob/main/examples/inventory.example.csv)), and common variants such as `IP Address` or `Server Name`
are recognised (`.xlsx` files are not read directly). Same thing in a terminal:
`proliant-mcp import servers.csv` (`--replace` overwrites entries that already exist, `--no-check` skips the login checks).

Both write `~/.config/proliant-mcp/inventory.ini` (or the file `PROLIANT_MCP_CONFIG` names), readable
only by you, and leave other content in the file as it is. If you only had a proliant-cli inventory,
its content is copied there first; the proliant-cli file is never changed. Passwords are stored in
that file in plain text, so delete a CSV you imported once you no longer need it.

**Or edit the file by hand.** Copy [examples/inventory.example.ini](https://github.com/hjma29/proliant-mcp/blob/main/examples/inventory.example.ini) to
`~/.config/proliant-mcp/inventory.ini` (Windows: `%USERPROFILE%\.config\proliant-mcp\inventory.ini`),
fill it in, and restrict it to your user. Or point `PROLIANT_MCP_CONFIG` at any path.
The file can be saved by Notepad or PowerShell (UTF-8 or UTF-16).

```ini
[defaults]
username = Administrator
password = changeme

; an iLO (type = ilo is the default)
[dl380-gen11]
host = 192.0.2.10

; a OneView appliance / Synergy Composer
[synergy]
type = oneview
host = 192.0.2.100

; a Compute Ops Management workspace
[greenlake]
type = com
client_id = ...
client_secret = ...
region = us-west
```

Keep comments on their own lines; a `#` or `;` after a value becomes part of the value.

```powershell
# Windows: create, edit, then lock to your user
New-Item -ItemType Directory -Force "$env:USERPROFILE\.config\proliant-mcp" | Out-Null
notepad "$env:USERPROFILE\.config\proliant-mcp\inventory.ini"
icacls "$env:USERPROFILE\.config\proliant-mcp\inventory.ini" /inheritance:r /grant:r "$($env:USERNAME):(R,W)"
```

```bash
# macOS / Linux
chmod 600 ~/.config/proliant-mcp/inventory.ini
```

A [proliant-cli](https://github.com/hjma29/proliant-cli) inventory at `~/.config/proliant-cli/inventory.ini` is also read.

### iLO

A read-only iLO account (Login privilege only) is enough for every tool. iLO uses a
self-signed certificate by default, so TLS verification is off unless you set
`verify_ssl = true`. One iLO can also come from environment variables:
`ILO_HOST`, `ILO_USERNAME`, `ILO_PASSWORD` (`ILO_NAME` sets its name, default `default`).

```bash
export ILO_HOST=192.0.2.10 ILO_USERNAME=Administrator ILO_PASSWORD='...'
```

### HPE OneView

A OneView user with the read-only role is enough. One appliance can also come from
environment variables: `ONEVIEW_HOST`, `ONEVIEW_USERNAME`, `ONEVIEW_PASSWORD`
(`ONEVIEW_NAME`). The REST API version is negotiated automatically.

### HPE Compute Ops Management

**Sign in from the chat** (easiest): ask your assistant to *"sign in to HPE GreenLake"*, or just
ask a COM question; it calls the `com_login` tool, which shows pop-ups in the chat:

1. Your HPE GreenLake email.
2. **`@hpe.com` accounts:** the number to tap in **Okta Verify** on your phone.
   **Other accounts (e.g. gmail.com):** a sign-in page opens with a masked password field. The page
   is served by proliant-mcp on your own computer (`127.0.0.1`), so the password goes straight to
   it, never through the AI client or model. If Okta also wants a push, the number pop-up follows.
3. Which workspace, if you have several. The COM region is detected.

It then creates a temporary GreenLake API client (`GLP-proliant-mcp-<host>-...`) in that workspace
and saves its ID and secret to `~/.config/proliant-mcp/com/login.json` (readable only by you). The
`com_*` tools use it straight away, no restart. Your email, password and session are never stored.
Signing in again replaces this computer's previous client.

**AI clients without pop-ups** (Claude Desktop, some Codex versions): the assistant signs you in
with `com_login_start` / `com_login_status` instead. It asks for your email in the chat, tells you the
number to tap in Okta Verify (or gives you a link to the password page, which opens on the computer
running the server), and confirms when you are signed in. You never leave the chat. When a COM
login stops working (for example after you delete the API client in GreenLake), the assistant
starts this sign-in by itself.

Codex ends tool calls after 60 seconds by default; the sign-in tools stay under that, but you can
raise it with `tool_timeout_sec = 300` in the `[mcp_servers.proliant]` section of `config.toml`.

The same sign-in also runs in a terminal:

```bash
uvx proliant-mcp@latest login
```

(`--email`, `--workspace NAME`, `--region us-west|eu-central|ap-northeast`; `proliant-mcp logout`
forgets the saved login, and you can delete the API client under **Manage Workspace > API**.)

The account needs permission to create API clients in the workspace. The sign-in is the same one
the GreenLake web page uses, which HPE does not publish as an API, so for unattended or shared
setups, or if HPE changes the sign-in, use an API client instead:

**Or use an API client.** In HPE GreenLake open **Manage Workspace > API**, create a personal API
client for the Compute Ops Management service in your region with a Compute Ops Management role (an
observer role is enough for these read-only tools), and copy the client ID and secret. Region is
`us-west`, `eu-central` or `ap-northeast`. Put it in the inventory (`type = com`) or set
`HPECOM_CLIENT_ID`, `HPECOM_CLIENT_SECRET` and `HPECOM_REGION`.

Order of use: inventory / environment variables, then the saved `proliant-mcp login`, then
proliant-cli's COM login (`proliant com login`), which is reused automatically if you have it.
`proliant-mcp --check --connect` shows which one is in use, its workspace and the region. If the
login's region has no COM for that workspace, the server uses the region that has it; if no region
does, sign in again and pick a workspace with Compute Ops Management.

## Test it

### 1. Unit tests (no hardware)

```bash
uv run pytest
```

Runs the tools against a fake iLO, OneView appliance and COM API (`httpx.MockTransport`)
through a real MCP client. No hardware or GreenLake account needed.

### 2. From the terminal

```bash
uv run fastmcp list src/proliant_mcp/server.py                     # tools and schemas
uv run fastmcp call src/proliant_mcp/server.py list_servers
uv run fastmcp call src/proliant_mcp/server.py get_ilo_info server=dl380-gen11
uv run fastmcp call src/proliant_mcp/server.py com_get_overview
uv run fastmcp call src/proliant_mcp/server.py oneview_list_alerts
```

### 3. MCP Inspector (web UI)

```bash
npx @modelcontextprotocol/inspector uv run --directory "$PWD" proliant-mcp
```

Open the printed URL, click **Connect**, then **Tools** to call each tool by hand.

### 4. VS Code / GitHub Copilot

Open this folder in VS Code. [.vscode/mcp.json](https://github.com/hjma29/proliant-mcp/blob/main/.vscode/mcp.json) registers
the server as `proliant`; start it from the MCP view (or the code lens in that
file), switch Copilot Chat to **Agent** mode, and ask:

- "Give me a COM fleet health report. Which servers need attention and why?"
- "Which COM servers are not connected, and when do subscriptions expire?"
- "Any active OneView alerts? What's the corrective action?"
- "Show the Synergy frame: power, Composers, interconnect firmware"
- "Which OneView profiles are not on the newest Synergy Service Pack?"
- "Any critical IML events on dl380-gen11?"
- "Which switch port is each NIC on dl345-gen12 connected to?"

To use it from any workspace, add the same entry to your user MCP config
with an absolute path in `--directory`.

Claude Desktop / Claude Code / other clients use the same command:
`uv run --directory /path/to/proliant-mcp proliant-mcp`.

### 5. Over HTTP

```bash
uv run proliant-mcp --http --host 127.0.0.1 --port 8000   # endpoint: http://127.0.0.1:8000/mcp
```

The HTTP endpoint has **no authentication**. Keep it on localhost or a
trusted management network.

## Google ADK

[examples/adk_agent/agent.py](https://github.com/hjma29/proliant-mcp/blob/main/examples/adk_agent/agent.py) is a
[Google ADK](https://google.github.io/adk-docs/) agent that starts this server
over stdio with `McpToolset`:

```bash
export GOOGLE_API_KEY=...            # or Vertex AI env vars
uv run --with "google-adk[mcp]" adk web examples   # pick "adk_agent"
```

Set `ADK_MODEL` to choose a different Gemini model. To use the HTTP mode
instead, swap `StdioConnectionParams` for
`StreamableHTTPConnectionParams(url="http://127.0.0.1:8000/mcp")`.

## Skills

Agent skills that tell an assistant which tool to use for which question, plus workflows:

- [skills/proliant-ilo/SKILL.md](https://github.com/hjma29/proliant-mcp/blob/main/skills/proliant-ilo/SKILL.md): one server through its iLO
  (firmware audit, health triage, finding a server's switch port).
- [skills/hpe-compute-fleet/SKILL.md](https://github.com/hjma29/proliant-mcp/blob/main/skills/hpe-compute-fleet/SKILL.md): fleets through
  Compute Ops Management and OneView (fleet health report, alert triage, firmware
  baseline drift, finding one server across all three sources).

Copy them into your agent's skills folder (for example `~/.copilot/skills/` or
`.github/skills/`).

## Roadmap

1. **Read-only (now):** iLO, Compute Ops Management and OneView inventory, health,
   alerts, jobs and firmware baselines.
2. **Actions,** opt-in and annotated destructive: power and UID (iLO, COM, OneView),
   one-time boot, COM firmware update jobs.
3. **More reports:** COM server inventory (storage, NICs, PCI), OneView networks and
   uplink sets, COM sustainability / energy reports.
4. **Skills** for deploying and managing ProLiant servers (OS deployment,
   firmware updates, BIOS baselines).

## Security

- No credentials are stored in this repository; `.gitignore` blocks `*.ini`
  (except the example) and `.env` files.
- Tools never return passwords or client secrets. `redfish_get` refuses session and
  account resources; `oneview_rest_get` refuses sessions and users; the raw COM and
  OneView GET tools redact keys such as `activationKey` and `password`.
- Each iLO and OneView tool call opens and closes its own session. COM access tokens
  are cached in memory only, until they expire.

## License

[MIT](https://github.com/hjma29/proliant-mcp/blob/main/LICENSE). Not an official HPE product.
