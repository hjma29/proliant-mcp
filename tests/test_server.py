"""Offline tests: a fake iLO served through httpx.MockTransport, called via the MCP client."""

from __future__ import annotations

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from fake_ilo import fake_ilo
from fake_mgmt import fake_mgmt
from proliant_mcp import com
from proliant_mcp import server as srv


def _fake(request: httpx.Request) -> httpx.Response:
    return fake_mgmt(request) or fake_ilo(request)


@pytest.fixture
def mcp_client(tmp_path, monkeypatch):
    ini = tmp_path / "inventory.ini"
    ini.write_text(
        "[defaults]\npassword = good\n\n"
        "[gen12]\nhost = 192.0.2.10\n\n"
        "[badpass]\nhost = 192.0.2.11\npassword = wrong\n\n"
        "[oneview]\nhost = 192.0.2.100\n\n"
        "[ov-badpass]\nhost = 192.0.2.100\ntype = oneview\npassword = wrong\n\n"
        "[greenlake]\ntype = com\nclient_id = cid\nclient_secret = csecret\nregion = us-west\n"
    )
    monkeypatch.setenv("PROLIANT_MCP_CONFIG", str(ini))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for var in ("ILO_HOST", "ONEVIEW_HOST", "HPECOM_CLIENT_ID", "HPECOM_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(srv, "_transport", httpx.MockTransport(_fake))
    com._tokens.clear()
    return Client(srv.mcp)


async def test_list_servers_groups_targets_and_hides_credentials(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("list_servers", {})).data
    assert [s["name"] for s in data["servers"]] == ["gen12", "badpass"]
    assert [a["name"] for a in data["oneview_appliances"]] == ["oneview", "ov-badpass"]
    assert data["com_accounts"][0]["name"] == "greenlake"
    assert "good" not in str(data) and "csecret" not in str(data)


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
    out = capsys.readouterr().out
    assert "30 tools" in out
    assert "latest release:" in out

    assert await srv._check(connect=True) == 1
    out = capsys.readouterr().out
    assert "gen12  192.0.2.10  OK  HPE ProLiant Compute DL345 Gen12" in out
    assert "badpass  192.0.2.11  FAILED" in out
    assert "oneview  192.0.2.100  OK  Synergy Composer2 11.10.00-1" in out
    assert "ov-badpass  192.0.2.100  FAILED" in out
    assert "greenlake  us-west  OK  2 servers" in out
    assert "good" not in out


@pytest.mark.parametrize(
    ("installed", "status", "expected"),
    [
        ("0.2.0", 200, "UPDATE AVAILABLE"),
        ("0.10.0", 200, "(up to date)"),
        ("0.11.0", 200, "newer, unreleased"),
        ("0.2.0", 404, "none published yet"),
        ("0.2.0", 503, "unknown"),
    ],
)
async def test_check_compares_with_latest_github_release(monkeypatch, installed, status, expected):
    def github(request: httpx.Request) -> httpx.Response:
        assert request.url == srv.LATEST_RELEASE_API
        return httpx.Response(status, json={"tag_name": "v0.10.0"})

    monkeypatch.setattr(srv, "_transport", httpx.MockTransport(github))
    monkeypatch.setattr(srv, "__version__", installed)
    assert expected in await srv._release_status()


async def test_com_overview_counts_only_uncleared_alerts_across_pages(mcp_client, monkeypatch):
    monkeypatch.setattr(com, "_PAGE", 1)
    async with mcp_client as c:
        data = (await c.call_tool("com_get_overview", {})).structured_content
    assert data["server_count"] == 2
    assert data["by_health"] == {"WARNING": 1, "OK": 1}
    assert data["not_connected"] == ["dl345.example.com"]
    assert data["active_alerts"]["by_severity"] == {"WARNING": 1}
    assert data["active_alerts"]["newest"][0]["server"] == "esx2.example.com"


async def test_com_get_server_by_ilo_ip(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("com_get_server", {"server": "192.0.2.50"})).structured_content
    assert data["serial_number"] == "SN0001"
    assert data["health_detail"] == {"bios": "WARNING"}
    assert [a["id"] for a in data["active_alerts"]] == ["a1"]


def test_redact_masks_secret_keys_and_values():
    from proliant_mcp.util import redact

    out = redact({"base64SSLKeyData": "x", "iloSsoUrl": "https://x", "nested": [{"url": "hplocons://addr=1&sessionkey=abc"}], "messageKey": "Fan"})
    assert out == {"base64SSLKeyData": "***", "iloSsoUrl": "***", "nested": [{"url": "***"}], "messageKey": "Fan"}


async def test_com_api_get_redacts_secrets(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("com_api_get", {"path": "/compute-ops-mgmt/v1beta1/appliances"})).structured_content
    assert data["items"][0]["activationKey"] == "***"


async def test_oneview_server_hardware_resolves_names(mcp_client):
    async with mcp_client as c:
        rows = (await c.call_tool("oneview_list_server_hardware", {})).structured_content["result"]
        detail = (await c.call_tool("oneview_get_server_hardware", {"server": "esx2.example.com"})).structured_content
    assert rows[0]["profile"] == "esx2-profile"
    assert rows[0]["location"] == "Enclosure-01"
    assert rows[0]["ilo"]["ip"] == "192.0.2.50"
    assert detail["firmware"][0]["location"] is None
    assert detail["active_alerts"][0]["description"] == "POST errors occurred."


async def test_oneview_profiles_and_appliance(mcp_client):
    async with mcp_client as c:
        profiles = (await c.call_tool("oneview_list_server_profiles", {})).structured_content["result"]
        info = (await c.call_tool("oneview_get_appliance", {"appliance": "ONEVIEW"})).structured_content
    assert profiles[0]["server_hardware"] == "Enclosure-01, bay 2"
    assert profiles[0]["template"] == "esx-template"
    assert info["api_version"] == 8400
    assert info["active_alerts"] == {"count": 1, "by_severity": {"Warning": 1}}


@pytest.mark.parametrize(
    "tool, args, message",
    [
        ("oneview_get_appliance", {"appliance": "ov-badpass"}, "login failed"),
        ("oneview_get_appliance", {"appliance": "nope"}, "Unknown OneView appliance"),
        ("oneview_rest_get", {"path": "/rest/login-sessions"}, "not exposed"),
        ("oneview_rest_get", {"path": "/rest/server-hardware/HW1/remoteConsoleUrl"}, "not exposed"),
        ("oneview_rest_get", {"path": "/rest/certificates/client/rabbitmq/keypair/default"}, "not exposed"),
        ("com_api_get", {"path": "/rest/servers"}, "must start with /compute-ops-mgmt/"),
        ("com_get_server", {"server": "nope"}, "No COM server matches"),
    ],
)
async def test_mgmt_errors(mcp_client, tool, args, message):
    async with mcp_client as c:
        with pytest.raises(ToolError, match=message):
            await c.call_tool(tool, args)


def test_com_account_reused_from_proliant_cli_login(tmp_path, monkeypatch):
    from proliant_mcp.config import load_com

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for var in ("HPECOM_CLIENT_ID", "PROLIANT_MCP_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    token = tmp_path / ".config" / "proliant-cli" / "com" / "token.json"
    token.parent.mkdir(parents=True)
    token.write_text('{"glp_client_id": "cid", "glp_client_secret": "sec", "region": "eu-central", "workspace_name": "Lab  WS"}')
    account = load_com()["com"]
    assert (account.client_id, account.region, account.workspace) == ("cid", "eu-central", "Lab WS")
