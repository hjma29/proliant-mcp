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

## Hardware snapshot cache

A local SQLite cache is **on by default** (15 minutes). `get_inventory_snapshot` (an iLO's
processors, memory and firmware) and `get_com_inventory_snapshot` (COM hardware inventory) answer
repeat questions from it in milliseconds instead of re-reading the server. Each result says whether
it came from the cache or was read live, and when it was collected; ask for "fresh" or "current"
data and the assistant passes `refresh=true`. In a test on a real iLO, reading processors, memory
and firmware took about 3 s live and about 0.1 s from the cache.

The cache is created on first use in an owner-only folder,
`~/.config/proliant-mcp/cache/inventory.sqlite3` (Windows:
`%USERPROFILE%\.config\proliant-mcp\cache\inventory.sqlite3`). It holds hardware details
such as server names, serial numbers, processors, DIMMs and firmware versions, and **never**
passwords or access tokens. If the folder cannot be secured or the database cannot be used, the
snapshot tools read live and say so; they never fail because of the cache.

- The iLO is still logged in to on every call, so a changed password or an unreachable iLO is
  reported even when data is cached. COM's server list is always fetched live. COM's own inventory
  may have been collected earlier; check its `inventory_collected_at` value.
- Snapshots can be stale, including component health. Health, power, alerts, network and other
  status questions use the live tools, which never use the cache. No background polling is done.
- Ask your assistant to **turn the cache off** (`set_inventory_cache`), change its lifetime
  (1 second to 24 hours), check it (`get_inventory_cache_status`), or **clear** it
  (`clear_inventory_cache`). Turning it off stops reads and writes but keeps existing data; clearing
  removes the rows but cannot guarantee erasure from filesystem backups or SQLite free pages. Clear
  it after replacing hardware.

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
