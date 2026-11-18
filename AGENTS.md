# Agent instructions for proliant-mcp

Public repository. Read this before changing anything.

1. **No credentials, ever.** No real passwords, API keys, tokens, client
   secrets or internal IPs/hostnames in code, tests, docs or examples. Use
   RFC 5737 addresses (`192.0.2.x`) and `changeme`-style placeholders. Real
   credentials live only in `~/.config/proliant-mcp/inventory.ini` (or
   proliant-cli's config) or env vars. Raw-GET tools must pass results
   through `util.redact`.
2. **Read-only by default.** New tools that change a server, a COM group or a
   OneView resource (power, UID, BIOS, network, firmware, profiles) must be clearly separated, annotated
   `destructiveHint`, and opt-in. Follow HPE's iLO Redfish rules (dual
   `Oem.Hpe` PATCH for NIC changes, `@Redfish.Settings` pending pattern).
3. **Workflow:** commit directly to `main` and push. Run `uv run pytest`
   first; it uses a fake iLO, OneView and COM (`tests/fake_*.py`) and needs
   no hardware or GreenLake account.
4. Layout: `config.py` (inventory: iLOs, OneView appliances, COM accounts),
   `redfish.py` + `ilo.py` (iLO), `oneview.py` (OneView client + views),
   `com.py` (COM client + views), `login.py` (GreenLake sign-in engine),
   `chat.py` (pop-up and secure password page machinery shared by the setup tools),
   `chat_login.py` (`com_login`), `chat_inventory.py` (`add_server`, `add_server_start`, `import_inventory`),
   `inventory.py` (edit/parse the inventory file), `probe.py` (login checks),
   `server.py` (MCP tools only), `skills/`.
   COM/OneView logic is ported from proliant-cli (`proliant/com`,
   `proliant/oneview`); this repo returns JSON for AI agents, not tables.
   Tool names: iLO tools unprefixed, `com_*`, `oneview_*`.
   GreenLake sign-in (`login.py`) ports proliant-cli's `proliant com login`:
   `@hpe.com` -> Okta Verify push (password fallback), other emails -> password
   (+ push if Okta asks). It uses undocumented web endpoints (keep the README
   caveat) and stores only the API client it creates, in
   `~/.config/proliant-mcp/com/login.json`. Two front ends share it through
   `LoginUI`: `com_login` (MCP elicitation; must work on both 2025-11-25 live
   pop-ups and 2026-07-28 `InputRequiredResult` rounds, test both),
   `com_login_start` + `com_login_status` (no pop-ups: each call returns the
   next step for the assistant to relay, within Codex's 60 s tool timeout) and
   the `proliant-mcp login` terminal command. Passwords never go through a form
   elicitation or the AI: URL-mode elicitation to a one-time 127.0.0.1 page.
5. **Releases** (proliant-cli style, no binaries): the version lives only in
   `pyproject.toml`. For each customer-visible change add a line under a
   `## vX.Y.Z — YYYY-MM-DD` section of `CHANGELOG.md` (New Features /
   Enhancements / Bug Fixes). To release: bump `pyproject.toml`, run
   `uv lock`, commit and push, then `git tag vX.Y.Z && git push origin vX.Y.Z`.
   `.github/workflows/release.yml` checks the tag matches `pyproject.toml`,
   runs the tests, publishes the release with that CHANGELOG section and
   `proliant-mcp.tar.gz`, uploads the same build to PyPI (trusted publishing,
   GitHub environment `pypi`; a version can never be re-uploaded, so a bad
   release is fixed by the next version), and installs it on Windows/Linux/macOS.
   Customers on `uvx proliant-mcp@latest` get it on their next AI client restart.
6. **Keep improving the MCP server from real use.** Always aim to add or extend
   an MCP tool whenever that makes answers more consistent, more efficient or
   faster than an agent writing a one-time script. A tool gives every customer's
   assistant the same correct answer in one call, with tested parsing, while an
   ad-hoc script is slow, differs on every run and is lost after the chat. This
   server should get better every time it is used. The goal is also that a customer
   never leaves the AI chat: when something is missing, the server guides them
   (sign-in, adding servers) in the chat, and errors tell the assistant the
   exact tool to call next, never "open a browser" or "run a terminal command".
   Whenever you (or a customer's assistant, in a screenshot or log) had to do
   any of these, treat it as a missing feature and fix it in this repo, with a
   test, before or alongside the answer:
   - ran a raw script, `curl`, `uv run python -c ...` or `com_api_get` /
     `redfish_get` / `oneview_rest_get` to get data a user asked for: add or
     extend a tool so it is one call next time (e.g. `com_list_gpus`);
   - looped a per-server tool over many servers: add a fleet tool or option;
   - read a credential file or worked around a login by hand, or asked the user
     to do something outside the chat: make a tool (or tool error) guide it;
   - saw an unclear error (raw HTTP code, stack trace, a message that sends the
     assistant the wrong way): make it say what happened and what to call next;
   - a client (Codex, Claude Desktop, opencode, VS Code) behaved differently:
     make the tools work there too and note it in the README.
   Always prefer the server's own tools over scripts when answering a user. If
   you must script once to answer now, turn it into a tool (or a tool option)
   in the same session, verify it against live data, and release it.
