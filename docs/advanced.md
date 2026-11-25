# Advanced

## Update and pin versions

`proliant-mcp@latest` makes `uvx` check [PyPI](https://pypi.org/project/proliant-mcp/) for a new
release each time the AI client starts the server (a fraction of a second when nothing changed).
To update, **restart the AI client** (in VS Code: **MCP: List Servers > proliant > Restart**).
Plain `proliant-mcp` without `@latest` keeps using the copy `uvx` cached first.

- **What's new:** [CHANGELOG.md](https://github.com/hjma29/proliant-mcp/blob/main/CHANGELOG.md) and the
  [releases page](https://github.com/hjma29/proliant-mcp/releases).
- **Which version am I on:** `--check` prints it, plus the latest release and
  `UPDATE AVAILABLE` if you are behind.
- **Pin a version** (no surprise changes; you upgrade by editing the version):
  `proliant-mcp@X.Y.Z` (a version from the [changelog](https://github.com/hjma29/proliant-mcp/blob/main/CHANGELOG.md)) in place of `proliant-mcp@latest`.
- **No access to PyPI** (some corporate networks): every release is also attached to GitHub.
  Use `"args": ["--refresh-package", "proliant-mcp", "--from", "https://github.com/hjma29/proliant-mcp/releases/latest/download/proliant-mcp.tar.gz", "proliant-mcp"]`.
- **Unreleased changes** from `main`:
  `"args": ["--from", "https://github.com/hjma29/proliant-mcp/archive/refs/heads/main.zip", "proliant-mcp"]`
  (picks up every push on restart; may include unfinished work).

To hack on it: `git clone https://github.com/hjma29/proliant-mcp && cd proliant-mcp && uv sync && uv run pytest`.

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

Install the server the same way users do (see the README): the `proliant` entry in the Copilot Global
config `~/.copilot/mcp-config.json` runs `uvx --python 3.13 proliant-mcp@latest`, the released package.
Start it from the MCP view, switch Copilot Chat to **Agent** mode, and ask:

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
2. **Actions,** opt-in and annotated destructive. Done: iLO firmware update (the user picks and
   confirms on a page). Next: power and UID (iLO, COM, OneView), one-time boot, COM firmware update jobs.
3. **More reports:** OneView networks, COM sustainability / energy reports.
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
- The password and sign-in pages are served on `127.0.0.1` with a random port and path, accept
  one answer, and never log what you type. `PROLIANT_MCP_PAGE_HOST` (off by default) serves them on
  another address for a server on a remote computer; that makes them plain HTTP on the network, so
  use it only over Tailscale or a VPN (see [Configuration](configuration.md)).
