# Configure

Credentials are read from the machine running the server, never from this
repository, and never sent to the AI model. That machine must reach each iLO
and OneView appliance over HTTPS (443), and `*.api.greenlake.hpe.com` /
`sso.common.cloud.hpe.com` for COM. Configure any mix of the three.

## Inventory file

**Add servers from the chat** (easiest): ask your assistant to *"add an iLO"* or *"add a OneView"*. It
calls the `add_server` tool, which shows a pop-up for the type, address, name and user name, then
opens a secure page on your own computer (`127.0.0.1`) for the password. The password goes straight
to proliant-mcp, never through the AI client or model. It is **checked against the server before
anything is saved**, so a wrong password or address is caught at once. The tools use the new server
immediately, no restart. In AI clients without pop-ups (Claude Desktop, some Codex versions) the
assistant asks for the address in the chat and uses `add_server_start` / `add_server_status`
instead: it gives you the same secure page link, and tells you when the server is saved.

**Add multiple iLOs:** ask your assistant to *"add my iLO servers"*. It opens a local browser
page where you can enter an IPv4 range or CIDR, paste one address per line (optionally preceded
by a name), or choose a text, CSV, INI, or `.xlsx` file. For Excel, the first sheet must have a
header row with an address column such as `host` or `iLO IP`; formulas are not supported. The
limit is 500 entries and 1 MB per file. Remove passwords from files and enter one shared
username and password on the page; use separate batches for different credentials. File-provided
usernames are not used in this flow.

Review and edit the list before confirming any network checks. The page checks the selected
iLOs, shows individual results, and lets you retry failures or choose what to save. Successful
checks are selected by default; saving failed checks is explicit. Existing names are kept unless
you choose replacement, and an address already used by another name is skipped. Names configured
through environment variables are also skipped, because the inventory cannot replace them. The page is
served on the computer running the MCP server and is not reachable through `127.0.0.1` from a
different computer. It does not send the file or password to the AI. Saved passwords are
plaintext in the local inventory file.

**Import a list** (older pop-up flow): ask your assistant to *"import my servers from a file"*. The
`import_inventory` tool pops up for the **path** of an `.ini` or `.csv` file (the AI cannot choose the
file; you type it), shows what it found, and optionally asks once for a password shared by entries
that have none. It saves before optional login checks, which cover at most the first 30 entries.
The file stays where it is. For a spreadsheet, use **File >
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

## Pages for a server on another computer

Passwords are typed on a short-lived page that proliant-mcp serves on its own computer
(`http://127.0.0.1:<port>/<random path>`), never in the chat. If proliant-mcp runs on another
computer than the one you sit at (a remote gateway or server), `127.0.0.1` is that computer and
your browser cannot open the page. Pick one:

- **No page for an `@hpe.com` GreenLake sign-in:** give the assistant your email in the chat. You then
  only tap the Okta Verify number it shows you.
- **Forward the port** over SSH: `ssh -L <port>:127.0.0.1:<port> <user>@<server>`, then open the same
  link on your computer.
- **Serve the pages on a reachable address:** set `PROLIANT_MCP_PAGE_HOST` to one address of the server's
  computer, for example its Tailscale or VPN address, in the MCP server's environment
  (`PROLIANT_MCP_PAGE_HOST=100.64.0.10`). The links then use that address. The pages are **plain HTTP**
  and anyone who can reach the address and knows the random link can use it, so use this only on a private,
  encrypted network (Tailscale, a VPN), never on the open internet. It must be an address of that computer
  (not `0.0.0.0`). It is off by default.

## Local cache

A local SQLite cache is **on by default**, and it is **cache first**: almost every read tool answers from it
whatever the age of the data (kept up to 7 days) and reads live only what is missing. Every cached answer ends
with one short line, `Note: data collected 46 min ago, say 'refresh' for the latest.`, and saying "refresh" reads
live. Two kinds of data have
their own lifetime, both 7 days by default (shorten them with `set_inventory_cache`):

| Kind | Default lifetime | Tools |
|---|---|---|
| Inventory (rarely changes) | 7 days | `get_ilo_info`, `get_firmware_inventory`, `get_processors`, `get_memory`, `get_storage`; `com_list_gpus`, `com_list_nics`, `com_hardware_report`, `com_list_groups`, `com_list_firmware_bundles`, `greenlake_list_services`; `oneview_list_firmware_baselines`; `list_servers` with `generation` or `model`; `find_server` |
| State (can change any time) | 7 days | `get_server_summary`, `get_fleet_summary`, `get_health`, `get_network_adapters` (links), `get_event_log`, `get_fleet_event_summary`, `get_ilo_logins`; `com_get_overview`, `com_list_servers`, `com_list_servers_table`, `com_get_server`, `com_list_alerts`, `com_list_jobs`; `oneview_get_appliance`, `oneview_list_server_hardware`, `oneview_get_server_hardware`, `oneview_list_server_profiles`, `oneview_list_alerts`, `oneview_list_enclosures`, `oneview_list_interconnects`, `oneview_list_uplink_sets` |
| Always live | | anything that changes a server or signs in (firmware update, `com_login`, `add_server`, ...), `ilo_firmware_update_status`, `ilo_firmware_list_updates`, and the raw `redfish_get`, `com_api_get`, `oneview_rest_get` |

Measured on a lab of 42 COM servers, 3 iLOs and one OneView appliance: those 35 tools took 31.9 s
live together and 0.7 s from the cache (12 to 66 ms each), with no request to any iLO, OneView or COM.

Each answer has `data_age`: `source` (`live`, `local cache` or both), `as_of` and `minutes_old`. When data
comes from the cache, the assistant ends the reply with that one line (the age is in minutes, hours or days) and
"refresh" makes it call again with `refresh=true`, which reads live and updates the cache.
Health and power from the cache can be days old, so the age line matters; the automatic refresh
keeps everything about an hour old.

- **A source that is down:** inventory falls back to the last stored answer (up to 7 days old), marked
  stale with the error. State never does: an iLO that cannot be read is reported, not hidden behind
  an old "OK". A refused login (iLO 401/403, GreenLake sign-in) is always reported, so you can fix it.
- **Firmware updates:** when `ilo_firmware_update_start` updates a server, its cached reads are dropped
  before and after the update, so the next answer shows the new versions.
- **New servers:** `find_server` looks in the cached lists first and again live if nothing matches.
- COM's own inventory may have been collected long before; check `inventory_collected_at`.

**Automatic refresh.** While proliant-mcp runs, it refreshes everything the tools read (each iLO with one
login, each COM account, each OneView appliance) about every 60 minutes, so cached answers stay recent and
nothing needs scheduling. The refresh only replaces what it read successfully: when a source is down its last
answer stays in the cache (up to 7 days) with its age. With several chats open, the running servers share one
refresh (a claim row in the cache), and each wakes at random times, so the sources see one refresh per hour,
spread over time. A source that refuses its sign-in is left alone for a day, to avoid account lock-outs; asking
for `refresh=true` still tries it. Ask your assistant to change the interval or turn it off
(`set_inventory_cache`, `poll_minutes`, 0 to 1440; 0 = off).

**Poll from cron instead.** `proliant-mcp poll` does the same refresh on a timer without any assistant running
(`--interval MINUTES`, default 30), and `proliant-mcp poll --once` refreshes once and exits, for cron or a
systemd timer. On the lab one refresh stores 83 entries in about 11 s, at the cost of one login per iLO:

```
# every 30 minutes, with uv installed (Linux / WSL crontab -e)
*/30 * * * * uvx --python 3.13 proliant-mcp@latest poll --once >> ~/.config/proliant-mcp/poll.log 2>&1
```

The cache is created on first use in an owner-only folder,
`~/.config/proliant-mcp/cache/inventory.sqlite3` (Windows:
`%USERPROFILE%\.config\proliant-mcp\cache\inventory.sqlite3`). It holds what the tools return (server
names, serial numbers, hardware, firmware, health, alerts, event log and login entries with user names
and source addresses) and **never** passwords or access tokens. If the folder cannot be secured or the
database cannot be used, the tools read live and say so; they never fail because of the cache.

- A cached answer does not log in to the iLO, OneView or COM: it is what that source said at `as_of`.
  `get_inventory_snapshot` still logs in to the iLO on every call.
- Ask your assistant to **turn the cache off** (`set_inventory_cache`), change its lifetimes
  (`ttl_seconds` for inventory, `state_ttl_seconds` for state, 1 second to 7 days), check it
  (`get_inventory_cache_status`), or **clear** it (`clear_inventory_cache`). Turning it off stops reads
  and writes but keeps existing data; clearing removes the rows but cannot guarantee erasure from
  filesystem backups or SQLite free pages. Clear it after replacing hardware.

## One COM list tool

proliant-mcp exposes one tool for COM server lists, `com_list_servers_table`. It filters by health, generation, GPU, CPU, NIC ports,
NIC mode (InfiniBand, Fibre Channel, Ethernet) and link, DIMM maker, direct-attached drives, storage controller and capacity,
groups with `group_by` (adapter, GPU, DIMM, CPU, storage controller, iLO version, model) and shows any column, with exact counts.
It replaces `com_list_servers`, `com_list_gpus`, `com_list_nics`, `com_list_memory` and `com_hardware_report`, which are hidden:
in tests with Claude and Qwen the assistants answered as many fleet questions right with one list tool, with fewer tools to
choose from and one answer shape for the same question. To bring the five tools back, set the environment variable
`PROLIANT_MCP_COM_LIST=several` for the MCP server. Example for an MCP client config:

```json
{"command": "uvx", "args": ["--python", "3.13", "proliant-mcp@latest"], "env": {"PROLIANT_MCP_COM_LIST": "several"}}
```

## Tool-call counters

proliant-mcp counts which tools your assistant calls, how often, how long they take and how many fail, so
tool descriptions can be improved. Ask your assistant to **show them** (`get_usage_stats`), **turn them off**
(`set_usage_stats`) or **delete them** (`clear`). The counts are kept only on your computer, in
`~/.config/proliant-mcp/usage.sqlite3` (Windows: `%USERPROFILE%\.config\proliant-mcp\usage.sqlite3`): per
call the tool name, the client's name, the time, the duration, ok or error and the size of the result.
**Never** the arguments, the results, server names, addresses or credentials, and nothing is sent anywhere.
Rows older than 90 days are removed. The summary lists calls, errors and times per tool and client, calls per
day and the longest bursts of calls (calls less than a minute apart, roughly one question each): dozens in
one burst usually means the assistant got lost and the tool descriptions need work. Turning it off keeps
the data; `clear` deletes it. Counting never makes a tool call fail.

## iLO

A read-only iLO account (Login privilege only) is enough for every tool except the iLO firmware
update, which needs the "Configure iLO Settings" privilege (an Administrator account has it). iLO uses a
self-signed certificate by default, so TLS verification is off unless you set
`verify_ssl = true`. One iLO can also come from environment variables:
`ILO_HOST`, `ILO_USERNAME`, `ILO_PASSWORD` (`ILO_NAME` sets its name, default `default`).

```bash
export ILO_HOST=192.0.2.10 ILO_USERNAME=Administrator ILO_PASSWORD='...'
```

## HPE OneView

A OneView user with the read-only role is enough. One appliance can also come from
environment variables: `ONEVIEW_HOST`, `ONEVIEW_USERNAME`, `ONEVIEW_PASSWORD`
(`ONEVIEW_NAME`). The REST API version is negotiated automatically.

## HPE Compute Ops Management

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
