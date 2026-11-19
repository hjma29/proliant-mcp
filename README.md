# proliant-mcp

**proliant-mcp** turns AI agents into your assistant for managing HPE compute: **HPE ProLiant** servers (iLO), **HPE Compute Ops Management** (GreenLake) and **HPE Synergy / OneView**, all from one conversation. It's an [MCP](https://modelcontextprotocol.io) server for Claude, GitHub Copilot, Codex and other AI assistants. Describe what you want in plain English instead of remembering which GUI screen or CLI command does it.

Ask things like *"which servers in Compute Ops Management are critical and why"*, *"show a table of each server's storage controller and GPU"* or *"any active OneView alerts on the Synergy frame"*.

## Install

### Option 1: ask your AI agent

Paste this into your AI agent (Claude, GitHub Copilot, Codex, ...):

> Install the proliant-mcp MCP server for me. Follow the instructions at https://raw.githubusercontent.com/hjma29/proliant-mcp/main/AGENTS.md (install uv first if it is missing), then tell me when to restart you.

### Option 2: manual

**Step 1.** Install [uv](https://docs.astral.sh/uv/) (no admin rights, no git needed).

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

macOS:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Step 2.** Add proliant to your AI client, then restart it.

- **VS Code (GitHub Copilot):** open *View > Command Palette*, run **MCP: Open User Configuration**, pick **Copilot Global**, then paste this and save:

  ```json
  {"mcpServers": {"proliant": {"type": "stdio", "command": "uvx", "args": ["proliant-mcp@latest"], "tools": ["*"]}}}
  ```

- **Claude Desktop:** *Settings > Developer > Edit Config*: `{"mcpServers": {"proliant": {"command": "uvx", "args": ["proliant-mcp@latest"]}}}`
- **Codex, opencode and others:** see [AGENTS.md](https://github.com/hjma29/proliant-mcp/blob/main/AGENTS.md).

If the client cannot find `uvx`, put its full path in `command`: `C:\Users\<you>\.local\bin\uvx.exe` on Windows, `/Users/<you>/.local/bin/uvx` on macOS.

## Connect your servers

Just ask your assistant, and it guides you in the chat:

- *"Sign in to HPE GreenLake"* for Compute Ops Management. If your chat cannot show pop-ups, it gives you a link to a page on your own computer to type your email.
- *"Add my iLO"* or *"add my OneView"*, or *"import my servers from a file"* (`.ini` or `.csv`).

Passwords are typed on a page on your own computer, never in the chat.

## Update

`proliant-mcp@latest` picks up new releases whenever your AI client restarts the server. To stay on one version, use `proliant-mcp@X.Y.Z`. To check what you run: `uvx proliant-mcp@latest --check --connect`.

## More

[Tools](https://github.com/hjma29/proliant-mcp/blob/main/docs/tools.md) | [Configuration](https://github.com/hjma29/proliant-mcp/blob/main/docs/configuration.md) (inventory file, environment variables, API client) | [Advanced](https://github.com/hjma29/proliant-mcp/blob/main/docs/advanced.md) (pinning, testing, skills, security) | [Changelog](https://github.com/hjma29/proliant-mcp/blob/main/CHANGELOG.md)

Credentials stay on your computer and are never sent to the AI model. MIT licensed; not an official HPE product.

<!-- mcp-name: io.github.hjma29/proliant-mcp -->
