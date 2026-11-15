"""Offline tests: a fake iLO served through httpx.MockTransport, called via the MCP client."""

from __future__ import annotations

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from fake_ilo import fake_ilo
from proliant_mcp import server as srv

@pytest.fixture
def mcp_client(tmp_path, monkeypatch):
    ini = tmp_path / "inventory.ini"
    ini.write_text(
        "[defaults]\npassword = good\n\n"
        "[gen12]\nhost = 192.0.2.10\n\n"
        "[badpass]\nhost = 192.0.2.11\npassword = wrong\n\n"
        "[oneview]\nhost = 192.0.2.100\ntype = oneview\n"
    )
    monkeypatch.setenv("PROLIANT_MCP_CONFIG", str(ini))
    monkeypatch.delenv("ILO_HOST", raising=False)
    monkeypatch.setattr(srv, "_transport", httpx.MockTransport(fake_ilo))
    return Client(srv.mcp)


async def test_list_servers_skips_non_ilo_and_hides_credentials(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("list_servers", {})).data
    assert [s["name"] for s in data["servers"]] == ["gen12", "badpass"]
    assert "good" not in str(data)


async def test_server_summary(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("get_server_summary", {"server": "gen12"})).structured_content
    assert data["ilo_version"] == "iLO 7 1.21.00 Apr 07 2026"
    assert data["processors"]["model"] == "AMD EPYC 9375F 32-Core Processor"
    assert data["uid_light"] == "Off"
    assert data["post_state"] == "FinishedPost"


async def test_processors_split_gpus(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("get_processors", {"server": "192.0.2.10"})).structured_content
    assert [p["model"] for p in data["cpus"]] == ["AMD EPYC 9375F"]
    assert data["gpus"][0]["name"] == "NVIDIA RTXPRO6000BlackwellDC"


async def test_network_lldp_port_hex_decoded(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("get_network_adapters", {"server": "gen12"})).structured_content["result"]
    port = data[0]["ports"][0]
    assert port["mac"] == "bc:97:e1:b1:60:24"
    assert port["lldp_neighbor"]["port"] == "swp4"


async def test_fleet_summary_isolates_failures(mcp_client):
    async with mcp_client as c:
        rows = (await c.call_tool("get_fleet_summary", {})).structured_content["result"]
    by_name = {r["server"]: r for r in rows}
    assert by_name["gen12"]["power_state"] == "On"
    assert "UnauthorizedLoginAttempt" in by_name["badpass"]["error"]


@pytest.mark.parametrize(
    "args",
    [
        {"server": "nope"},
        {"server": "gen12", "path": "/etc/passwd"},
        {"server": "gen12", "path": "/redfish/v1/AccountService/Accounts/"},
    ],
)
async def test_errors(mcp_client, args):
    tool = "redfish_get" if "path" in args else "get_ilo_info"
    async with mcp_client as c:
        with pytest.raises(ToolError):
            await c.call_tool(tool, args)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_inventory_file_saved_by_windows_editors(tmp_path, encoding):
    from proliant_mcp.config import load_servers

    ini = tmp_path / "inventory.ini"
    ini.write_text("[defaults]\r\npassword = changeme\r\n\r\n[lab]\r\nhost = 192.0.2.10\r\n", encoding=encoding)
    servers = load_servers(ini)
    assert servers["lab"].host == "192.0.2.10"
    assert servers["lab"].password == "changeme"


async def test_check_reports_servers_and_login_failures(mcp_client, capsys):
    assert await srv._check(connect=False) == 0
    assert "12 tools" in capsys.readouterr().out

    assert await srv._check(connect=True) == 1
    out = capsys.readouterr().out
    assert "gen12  192.0.2.10  OK  HPE ProLiant Compute DL345 Gen12" in out
    assert "badpass  192.0.2.11  FAILED" in out
    assert "good" not in out
