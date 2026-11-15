"""Google ADK agent that uses proliant-mcp over stdio.

Run from the repo root:
    uv run --with "google-adk[mcp]" adk web examples
and pick "adk_agent". Needs GOOGLE_API_KEY (or Vertex AI env vars) for Gemini.
"""

import os
from pathlib import Path

from google.adk.agents import Agent
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from mcp import StdioServerParameters

REPO = Path(__file__).resolve().parents[2]

proliant_tools = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "--directory", str(REPO), "proliant-mcp"],
        ),
        timeout=120,
    ),
)

root_agent = Agent(
    name="proliant_agent",
    model=os.environ.get("ADK_MODEL", "gemini-2.5-flash"),
    instruction=(
        "You help operate HPE ProLiant servers through their iLO. Call list_servers first "
        "to learn server names. Use the tools for every fact; never guess versions or state."
    ),
    tools=[proliant_tools],
)
