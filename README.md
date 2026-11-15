# proliant-mcp

An [MCP](https://modelcontextprotocol.io) server for **HPE ProLiant** servers,
built with [FastMCP](https://gofastmcp.com). It lets AI assistants and agents
(GitHub Copilot, Claude, Google ADK, ...) read server inventory and health
straight from each server's **iLO** through the Redfish API.

Tested against iLO 6 (ProLiant DL380 Gen11) and iLO 7 (ProLiant Compute DL325 /
DL345 Gen12). iLO 5 (Gen10) uses the same Redfish paths.

> Status: **v0.1, iLO, read-only.** HPE Compute Ops Management (COM) and
> OneView are next; see [Roadmap](#roadmap).

## Tools

| Tool | What it returns |
|---|---|
| `list_servers` | Configured server names and iLO hosts (never credentials) |
| `get_server_summary` | Model, product ID, serial, power and POST state, health, UID, BIOS and iLO version, CPU/memory totals |
| `get_fleet_summary` | `get_server_summary` for all servers in parallel; failures are per server |
| `get_ilo_info` | iLO model and firmware version/date, license, COM connection status, iLO IP/MAC/DHCP/DNS |
| `get_health` | Overall and per-subsystem health, fan and power-supply redundancy |
| `get_firmware_inventory` | Every firmware component and version |
| `get_processors` | CPUs and GPUs |
| `get_memory` | Total, populated/empty slots, each DIMM |
| `get_storage` | Controllers, drives, RAID volumes |
| `get_network_adapters` | NICs, ports, MACs, link, LLDP neighbor (switch and port) |
| `get_event_log` | Newest IML (hardware) or IEL (iLO) entries, filter by severity |
| `redfish_get` | Raw read-only GET of any `/redfish/v1/...` resource |

Every tool is annotated `readOnlyHint`. Nothing in this version changes a server.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/hjma29/proliant-mcp.git
cd proliant-mcp
uv sync
```

## Configure your iLOs

Credentials are read from your machine only, never from this repository.

**Option A: inventory file (several servers).** Copy
[examples/inventory.example.ini](examples/inventory.example.ini) to
`~/.config/proliant-mcp/inventory.ini`, fill in hosts and passwords, then
`chmod 600` it. Or point `PROLIANT_MCP_CONFIG` at any path. An existing
[proliant-cli](https://github.com/hjma29/proliant-cli) inventory at
`~/.config/proliant-cli/inventory.ini` is picked up automatically.

**Option B: one server from environment variables.**

```bash
export ILO_HOST=192.0.2.10 ILO_USERNAME=Administrator ILO_PASSWORD='...'
export ILO_NAME=my-server        # optional, default "default"
```

iLO uses a self-signed certificate by default, so TLS verification is off
unless you set `verify_ssl = true` (ini) or `ILO_VERIFY_SSL=1`. A read-only
iLO account (Login privilege only) is enough for every tool.

## Test it

### 1. Unit tests (no hardware)

```bash
uv run pytest
```

Runs the tools against a fake iLO (`httpx.MockTransport`) through a real MCP client.

### 2. From the terminal

```bash
uv run fastmcp list src/proliant_mcp/server.py                     # tools and schemas
uv run fastmcp call src/proliant_mcp/server.py list_servers
uv run fastmcp call src/proliant_mcp/server.py get_ilo_info server=dl380-gen11
```

### 3. MCP Inspector (web UI)

```bash
npx @modelcontextprotocol/inspector uv run --directory "$PWD" proliant-mcp
```

Open the printed URL, click **Connect**, then **Tools** to call each tool by hand.

### 4. VS Code / GitHub Copilot

Open this folder in VS Code. [.vscode/mcp.json](.vscode/mcp.json) registers
the server as `proliant`; start it from the MCP view (or the code lens in that
file), switch Copilot Chat to **Agent** mode, and ask:

- "List my ProLiant servers and their iLO versions"
- "Which servers have a GPU?"
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

[examples/adk_agent/agent.py](examples/adk_agent/agent.py) is a
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

[skills/proliant-ilo/SKILL.md](skills/proliant-ilo/SKILL.md) is an agent skill
that tells an assistant which tool to use for which question, plus workflows
(fleet firmware audit, health triage, finding a server's switch port). Copy
it into your agent's skills folder (for example `~/.copilot/skills/` or
`.github/skills/`).

## Roadmap

1. **iLO (now):** read-only inventory and health.
2. **iLO actions:** power, UID, one-time boot, opt-in and gated as destructive.
3. **HPE Compute Ops Management (GreenLake):** fleet inventory, firmware
   baselines and jobs.
4. **HPE OneView / Synergy.**
5. **Skills** for deploying and managing ProLiant servers (OS deployment,
   firmware updates, BIOS baselines).

## Security

- No credentials are stored in this repository; `.gitignore` blocks `*.ini`
  (except the example) and `.env` files.
- Tools never return passwords; `redfish_get` refuses session and account resources.
- Each tool call opens and closes its own Redfish session.

## License

[MIT](LICENSE). Not an official HPE product.
