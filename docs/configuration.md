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

## Optional hardware snapshot cache

The local SQLite cache is **off by default**. Ask your assistant to enable the inventory cache
and optionally specify a lifetime; the default is 15 minutes, with a maximum of 24 hours.
`set_inventory_cache` saves this preference locally; `get_inventory_cache_status` shows it.
Cached hardware snapshots are stored in `~/.config/proliant-mcp/cache/inventory.sqlite3`
(Windows: `%USERPROFILE%\.config\proliant-mcp\cache\inventory.sqlite3`). They can contain
server names, serial numbers and hardware details, so enable the cache only on a trusted
computer. The cache never stores passwords or access tokens. Enabling the cache requires an
owner-only cache directory; failure to secure it is reported instead of enabling caching.

`get_inventory_snapshot` uses the cache for iLO processors, memory and firmware;
`get_com_inventory_snapshot` uses it for COM hardware inventory. Each snapshot states whether
it is local cache or a live fetch and when it was collected. The iLO is authenticated even on a
cache hit; COM's server list is fetched live. COM's inventory itself may have been collected
earlier, so also check its `inventory_collected_at` value. Use `refresh=true` to bypass the
local cache. Expired entries and failed fetches are never returned as fresh cache hits.
Snapshot component-health values may be old; use the existing live tools for current health,
power, alerts and network status. No background polling is performed.

Ask to disable the cache to stop further reads and writes; this does not remove existing data.
Ask to clear it to remove cached rows. Clearing does not guarantee erasure from filesystem
backups or SQLite free pages. Clear the cache after changing permissions or replacing hardware.
These snapshot tools are separate from existing live tools, which keep their existing behavior.

## iLO

A read-only iLO account (Login privilege only) is enough for every tool. iLO uses a
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
