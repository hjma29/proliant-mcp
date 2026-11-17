"""Google ADK agent that uses hpe-compute-mcp over stdio.

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

hpe_compute_tools = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "--directory", str(REPO), "hpe-compute-mcp"],
        ),
        timeout=120,
    ),
)

root_agent = Agent(
    name="hpe_compute_agent",
    model=os.environ.get("ADK_MODEL", "gemini-2.5-flash"),
    instruction=(
        "You help operate HPE ProLiant servers through their iLO, HPE Compute Ops Management "
        "and HPE OneView. Call list_servers first to learn server, appliance and account names. "
        "Use the tools for every fact; never guess versions or state."
    ),
    tools=[hpe_compute_tools],
)
