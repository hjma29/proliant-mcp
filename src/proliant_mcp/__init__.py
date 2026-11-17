"""MCP server for HPE compute: ProLiant iLO (Redfish), Compute Ops Management and OneView."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("proliant-mcp")
except PackageNotFoundError:
    __version__ = "0+unknown"
