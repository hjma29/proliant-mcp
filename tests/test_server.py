"""Offline tests: a fake iLO served through httpx.MockTransport, called via the MCP client."""

from __future__ import annotations

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import fake_mgmt as fake_mgmt_module
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


async def test_event_log_filters_and_puts_wrong_clock_entries_last(mcp_client):
    async with mcp_client as c:
        newest = (await c.call_tool("get_event_log", {"server": "gen12", "log": "iel", "limit": 20})).structured_content["result"]
        recent = (await c.call_tool("get_event_log", {"server": "gen12", "log": "iel", "since": "2d", "contains": "LOGIN"})).structured_content["result"]
        bad = await c.call_tool("get_event_log", {"server": "gen12", "log": "iel", "since": "last week"}, raise_on_error=False)
    # the 2039 entry would sort first by date; it goes last, flagged, with the 0000 one
    assert newest[0]["message"].startswith("Browser logout") and "time_invalid" not in newest[0]
    assert [e["created"][:4] for e in newest if e.get("time_invalid")] == ["0000", "2039"]
    assert all(e.get("time_invalid") for e in newest[-2:])
    assert len(recent) == 5 and not any("old.example.com" in e["message"] or e.get("time_invalid") for e in recent)
    assert bad.is_error and "7d" in bad.content[0].text


async def test_ilo_logins_summarises_the_fleet_in_one_call(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("get_ilo_logins", {"days": 7, "utc_offset_hours": 0})).structured_content
    assert data["totals"] == {"logins": 4, "failed_logins": 1, "security_alerts": 1, "servers": 1, "unreachable": 1}
    gen12 = next(s for s in data["servers"] if s["server"] == "gen12")
    assert gen12["by_type"] == {"Browser": 3, "Host REST": 1}
    assert gen12["users"] == {"admin": 2, "Administrator": 1, "System Administrator": 1}
    assert gen12["sources"][0] == {"ip": "192.0.2.60", "host": None, "logins": 2, "last_login": gen12["sources"][0]["last_login"]}
    assert gen12["sources"][1]["this_computer"] is True and gen12["sources"][1]["host"] == "admin-pc.example.com"
    assert gen12["failures"][0]["ip"] == "192.0.2.66" and "unauthorized" in gen12["security_alerts"][0]["message"]
    assert gen12["entries_with_invalid_time"] == 2 and gen12["log_covers_window"] is True
    assert len(data["by_day"]) == 7 and sum(d["total"] for d in data["by_day"]) == 4 and "gen12" in data["by_day"][0]
    assert "UnauthorizedLoginAttempt" in next(s for s in data["servers"] if s["server"] == "badpass")["error"]
    assert any("this_computer" in n for n in data["notes"]) and any("invalid clock" in n for n in data["notes"])


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
    assert "39 tools" in out
    assert "latest release:" in out

    assert await srv._check(connect=True) == 1
    out = capsys.readouterr().out
    assert "gen12  192.0.2.10  OK  HPE ProLiant Compute DL345 Gen12" in out
    assert "badpass  192.0.2.11  FAILED" in out
    assert "oneview  192.0.2.100  OK  Synergy Composer2 11.10.00-1" in out
    assert "ov-badpass  192.0.2.100  FAILED" in out
    assert "greenlake  us-west  OK  4 servers" in out
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
    assert data["server_count"] == 4
    assert data["by_health"] == {"WARNING": 1, "OK": 3}
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


async def test_com_list_gpus_reports_the_fleet_in_one_call(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("com_list_gpus", {})).structured_content
    assert (data["servers_checked"], data["servers_with_gpus"], data["total_gpus"]) == (4, 2, 4)
    assert data["gpus_by_model"] == {"NVIDIA L40S": 2, "Intel Video Controller": 1, "NVIDIA T4": 1}
    assert data["inventory_unavailable"] == ["dl345.example.com"]  # no inventory is "unknown", not "no GPU"
    gpu1 = next(s for s in data["servers"] if s["name"] == "gpu1.example.com")
    assert (gpu1["model"], gpu1["ilo_version"], gpu1["gpu_count"]) == ("ProLiant DL380 Gen11", "iLO 6 v1.74", 3)
    assert gpu1["cpu"] == [{"model": "AMD EPYC 9374F 32-Core Processor", "count": 1, "cores_each": 32, "threads_each": 64}]
    assert gpu1["gpus"][0] == {"name": "NVIDIA L40S", "manufacturer": "NVIDIA", "part_number": "900-2G133-0380-030", "count": 2, "health": "OK"}
    old = next(s for s in data["servers"] if s["name"] == "old-t4.example.com")
    assert old["gpus"][0]["name"] == "NVIDIA T4"  # PCIe display controller fallback; embedded video is ignored
    assert all(s["name"] != "esx2.example.com" for s in data["servers"])  # embedded video only

    async with mcp_client as c:
        everyone = (await c.call_tool("com_list_gpus", {"include_servers_without_gpus": True, "name_contains": "esx"})).structured_content
    assert [s["name"] for s in everyone["servers"]] == ["esx2.example.com"] and everyone["servers"][0]["gpu_count"] == 0


async def test_com_list_servers_with_hardware_and_server_detail(mcp_client):
    async with mcp_client as c:
        rows = (await c.call_tool("com_list_servers", {"include_hardware": True})).structured_content["result"]
        plain = (await c.call_tool("com_list_servers", {})).structured_content["result"]
        detail = (await c.call_tool("com_get_server", {"server": "gpu1.example.com"})).structured_content
    assert "hardware" not in plain[0]
    by_name = {r["name"]: r for r in rows}
    assert by_name["gpu1.example.com"]["hardware"]["gpu_count"] == 3
    assert by_name["dl345.example.com"]["hardware"]["inventory_available"] is False
    assert detail["hardware"]["gpus"][0]["serial_numbers"] == ["GSN3", "GSN4"]


def _greenlake_by_region(totals):
    """Fake GreenLake where COM answers only in the regions listed in *totals* (others: routing 404)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "sso.common.cloud.hpe.com":
            return httpx.Response(200, json={"access_token": "comtok", "expires_in": 7200})
        region = request.url.host.split(".")[0]
        if region not in totals:
            return httpx.Response(404, json={"httpStatusCode": 404, "errorCode": "HPE_GL_ERROR_NOT_FOUND",
                                             "message": "Routing details for the customer not found."})
        servers = [{"id": f"{region}-{i}", "name": f"{region}-srv{i}"} for i in range(totals[region])]
        return httpx.Response(200, json={"items": servers, "total": len(servers), "count": len(servers)})

    return handler


@pytest.fixture
def com_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("PROLIANT_MCP_CONFIG", str(tmp_path / "none.ini"))
    (tmp_path / "none.ini").write_text("")
    monkeypatch.setenv("HPECOM_CLIENT_ID", "cid-region-test")
    monkeypatch.setenv("HPECOM_CLIENT_SECRET", "s")
    monkeypatch.setenv("HPECOM_REGION", "us-west")
    com._tokens.clear()
    com._regions.clear()
    yield monkeypatch
    com._regions.clear()


async def test_com_uses_the_region_where_the_workspace_has_com(com_only, capsys):
    com_only.setattr(srv, "_transport", httpx.MockTransport(_greenlake_by_region({"eu-central": 3, "ap-northeast": 1})))
    async with Client(srv.mcp) as c:
        rows = (await c.call_tool("com_list_servers", {})).structured_content["result"]
        accounts = (await c.call_tool("list_servers", {})).data["com_accounts"]
    assert [r["name"] for r in rows] == ["eu-central-srv0", "eu-central-srv1", "eu-central-srv2"]  # most servers wins
    assert accounts == [{"name": "com", "region": "eu-central", "configured_region": "us-west", "workspace": None, "source": "environment"}]

    com._regions.clear()
    assert await srv._check(connect=True) == 0
    assert "com  us-west  OK  3 servers in eu-central (this workspace has no COM in us-west); login: environment" in capsys.readouterr().out


async def test_com_in_no_region_says_to_sign_in_again(com_only):
    com_only.setattr(srv, "_transport", httpx.MockTransport(_greenlake_by_region({})))
    async with Client(srv.mcp) as c:
        result = await c.call_tool("com_get_overview", {}, raise_on_error=False)
    text = result.content[0].text
    assert result.is_error and "not set up for this GreenLake workspace in any region" in text
    assert "comes from environment" in text and "com_login" in text


async def test_com_keeps_a_working_region(com_only):
    com_only.setattr(srv, "_transport", httpx.MockTransport(_greenlake_by_region({"us-west": 1, "eu-central": 5})))
    async with Client(srv.mcp) as c:
        rows = (await c.call_tool("com_list_servers", {})).structured_content["result"]
    assert [r["name"] for r in rows] == ["us-west-srv0"] and com._regions == {}


async def test_com_hardware_includes_storage_controllers_and_direct_drives(mcp_client):
    async with mcp_client as c:
        rows = (await c.call_tool("com_list_servers", {"include_hardware": True})).structured_content["result"]
        detail = (await c.call_tool("com_get_server", {"server": "esx2.example.com"})).structured_content
    storage = {r["name"]: r["hardware"]["storage"] for r in rows if "storage" in r["hardware"]}
    assert storage["esx2.example.com"] == {
        "known": True,
        "controllers": [{"model": "Smart Array P408i-a SR Gen10", "firmware": "7.00", "health": "OK",
                         "drives": ["2 x 960GB SSD", "1 x 1920GB SSD"], "drive_count": 3}],  # empty bay ignored
        "direct_drives": ["2 x 3200GB NVMe"],  # DA* drives' own NVMe chip is not a controller
        "drive_count": 5,
    }
    assert storage["gpu1.example.com"] == {"controllers": [], "direct_drives": [], "drive_count": 0, "known": False}
    assert [d["serial"] for d in detail["hardware"]["storage"]["controllers"][0]["drive_detail"]] == ["DSN1", "DSN2", "DSN3"]


async def test_com_hardware_report_groups_identical_servers_in_a_compact_answer(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("com_hardware_report", {})).structured_content
    assert data["servers_checked"] == 4 and data["storage_unknown"] == ["gpu1.example.com", "old-t4.example.com", "dl345.example.com"]
    assert data["gpu_unknown"] == ["dl345.example.com"]
    esx = next(g for g in data["configurations"] if g["servers"] == ["esx2.example.com"])
    assert esx["storage_controllers"] == [{"model": "Smart Array P408i-a SR Gen10", "drives": ["2 x 960GB SSD", "1 x 1920GB SSD"]}]
    assert esx["direct_attached_drives"] == ["2 x 3200GB NVMe"] and esx["gpus"] == []
    gpu = next(g for g in data["configurations"] if g["servers"] == ["gpu1.example.com"])
    assert gpu["gpus"] == ["2 x NVIDIA L40S", "1 x Intel Video Controller"] and gpu["storage_controllers"] is None  # unknown, not none
    assert next(g for g in data["configurations"] if g["servers"] == ["dl345.example.com"])["gpus"] is None


def _fake_com_server(server_id: str, name: str) -> dict:
    base = fake_mgmt_module.FAKE_COM["/compute-ops-mgmt/v1/servers"][0]
    return {**base, "id": server_id, "name": name}


async def test_com_hardware_report_merges_identical_hardware(mcp_client, monkeypatch):
    twin = {**_fake_com_server("P5+SN0005", "esx3.example.com")}
    monkeypatch.setitem(fake_mgmt_module.FAKE_COM, "/compute-ops-mgmt/v1/servers", [*fake_mgmt_module.FAKE_COM["/compute-ops-mgmt/v1/servers"], twin])
    monkeypatch.setitem(fake_mgmt_module.FAKE_COM_INVENTORY, "/compute-ops-mgmt/v1/servers/P5+SN0005/inventory",
                        fake_mgmt_module.FAKE_COM_INVENTORY["/compute-ops-mgmt/v1/servers/P1+SN0001/inventory"])
    async with mcp_client as c:
        data = (await c.call_tool("com_hardware_report", {"name_contains": "esx"})).structured_content
    assert [(g["count"], g["servers"]) for g in data["configurations"]] == [(2, ["esx2.example.com", "esx3.example.com"])]
