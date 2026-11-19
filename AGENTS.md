# proliant-mcp: instructions for AI agents

MCP server for HPE ProLiant iLO, Compute Ops Management (GreenLake) and OneView. Read-only. Docs: [README.md](README.md), [docs/](docs).

## Install it for the user

Add it to the user's MCP client config. It needs only [uv](https://docs.astral.sh/uv/) (no git, no clone) and installs from PyPI:

```json
{"servers": {"proliant": {"type": "stdio", "command": "uvx", "args": ["proliant-mcp@latest"]}}}
```

- Key names differ by client: Claude Desktop `mcpServers`; Codex `config.toml` `[mcp_servers.proliant]` with `command = 'uvx'` and `args = ['proliant-mcp@latest']`; opencode `"mcp": {"servers": {"proliant": {"type": "local", "command": ["uvx", "proliant-mcp@latest"]}}}`.
- Do not install with `uvx --from git+https://github.com/hjma29/proliant-mcp`: it needs git and installs unreleased code. `@latest` picks up new releases when the client restarts the server; pin with `proliant-mcp@X.Y.Z`.
- If `uv` is missing, install it: Windows `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`, macOS/Linux `curl -LsSf https://astral.sh/uv/install.sh | sh`. After editing the config, tell the user to restart the client.
- If the client cannot find `uvx`, use its full path (Windows `%USERPROFILE%\.local\bin\uvx.exe`).
- Check the install: `uvx proliant-mcp@latest --check --connect`.

## Use it

- Start with `list_servers`, then use the tool descriptions. For anything across many servers use a fleet tool (`com_hardware_report`, `com_list_servers`, `get_fleet_summary`, ...) rather than looping per-server tools or scripting raw API calls.
- If nothing is configured, set it up in the chat; never ask the user to type a password in the chat, or to leave it for a browser or terminal:
  - Compute Ops Management: `com_login`; if no pop-up appears, `com_login_start` then `com_login_status`, and relay the page link or Okta Verify number. Pass `email` only if the user gave it; do not ask for it with a question tool (some modes auto-answer "user not available"), because without it the user types it on a local page.
  - iLO or OneView: `add_server` for one (if no pop-up appears it continues with a local page link to relay), `import_inventory` for a `.ini` or `.csv` file (the user types its path); without pop-ups `add_server_start` (pass `host` only if the user gave it) then `add_server_status`.
- Credentials are stored on the user's computer only (`~/.config/proliant-mcp/`), never in this repo.

## Contributing

- Public repo: never commit real credentials, tokens or internal IPs or hostnames; use `192.0.2.x` and `changeme`.
- New tools are read-only unless clearly separated, annotated `destructiveHint` and opt-in.
- `uv run pytest` needs no hardware (fake iLO, OneView and COM in `tests/fake_*.py`).
