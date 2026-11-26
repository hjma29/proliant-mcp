"""Offline tests: a fake iLO served through httpx.MockTransport, called via the MCP client."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import fake_mgmt as fake_mgmt_module
from fake_ilo import FAKE_ILO, fake_ilo
from fake_mgmt import fake_mgmt
from proliant_mcp import chat_firmware, com, ilo_firmware, inventory_cache, lookup
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


async def test_list_servers_by_generation_counts_every_source_once(mcp_client, monkeypatch):
    # the iLO and COM report the same physical server: the serial number is the same, whatever its case
    monkeypatch.setitem(FAKE_ILO["/redfish/v1/Systems/1/"], "SerialNumber", "sn0002")
    async with mcp_client as c:
        gen12 = (await c.call_tool("list_servers", {"generation": "Gen12"})).structured_content
        gen10 = (await c.call_tool("list_servers", {"generation": "10"})).structured_content
        dl380 = (await c.call_tool("list_servers", {"model": "dl380"})).structured_content
    assert gen12["total"] == 1 and gen12["counts"]["by_source"] == {"ilo": 1, "com": 1, "oneview": 0}  # one server, two sources
    assert gen12["counts"]["in_more_than_one_source"] == 1 and "| gen12 | DL345 Gen12 | On | ilo+com |" in gen12["table"]
    assert gen10["total"] == 2 and gen10["counts"]["by_source"] == {"ilo": 0, "com": 2, "oneview": 1}  # COM and OneView share SN0001
    assert gen10["counts"]["in_more_than_one_source"] == 1 and "esx2.example.com | Synergy 480 Gen10 | ON | com+oneview" in gen10["table"]
    assert set(gen10["source_errors"]) == {"ilo:badpass", "oneview:ov-badpass"}  # failed sources are named, not silently counted as empty
    assert dl380["total"] == 1 and "gpu1.example.com" in dl380["table"] and dl380["filter"] == {"generation": None, "model": "dl380"}
    assert gen12["counts"]["servers_checked"] == 4 and gen12["counts"]["unknown_generation"] == 0


async def test_list_servers_filter_that_matches_nothing_says_what_exists(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("list_servers", {"generation": "13"})).structured_content
        plain = (await c.call_tool("list_servers", {})).structured_content
    assert data["total"] == 0 and data["table"] == "No server matches."
    assert data["available"]["servers"] == 5 and data["available"]["generations"] == {"Gen10": 2, "Gen11": 1, "Gen12": 2}
    assert "servers" in plain and "available" not in plain  # without a filter nothing changes


def test_generation_comes_from_the_source_value_or_the_model_name():
    assert lookup.generation_of("GEN_11", None) == "11" and lookup.generation_of("Gen10", "x") == "10"
    assert lookup.generation_of(None, "HPE ProLiant Compute DL325 Gen12") == "12" and lookup.generation_of("UNKNOWN", "ProLiant DL380 Gen11") == "11"
    assert lookup.generation_of(None, "Synergy Composer2") is None


def test_merge_joins_rows_by_serial_and_never_joins_servers_without_one():
    rows = [
        {"source": "ilo", "label": "ilo:a", "name": "a", "model": "ProLiant DL380 Gen11", "serial_number": "ABC1", "power_state": "On"},
        {"source": "com", "label": "com:c", "name": "A-in-com", "model": None, "serial_number": "abc1", "generation": "GEN_11"},
        {"source": "com", "label": "com:c", "name": "no-serial-1", "model": None, "serial_number": None},
        {"source": "com", "label": "com:c", "name": "no-serial-2", "model": None, "serial_number": ""},
    ]
    merged = lookup.merge(rows)
    assert [m["name"] for m in merged] == ["a", "no-serial-1", "no-serial-2"]  # the iLO name wins; no serial, no merge
    assert merged[0]["sources"] == ["ilo:a", "com:c"] and merged[0]["generation"] == "11" and merged[1]["generation"] is None


def test_lookup_text_shows_a_few_names_per_model_when_the_list_is_long():
    def server(i: int, model: str) -> dict:
        return {"name": f"srv{i:02d}", "model": model, "power_state": "On", "source_types": ["com"]}

    matches = [server(i, "ProLiant DL380a Gen11") for i in range(12)] + [server(20 + i, "ProLiant DL360 Gen11") for i in range(5)]
    table, remaining, more = lookup._text(matches)
    lines = table.splitlines()
    assert lines[0] == "DL380a Gen11 (12): srv00 (com), srv01 (com), srv02 (com), +9 more"
    assert lines[1] == "DL360 Gen11 (5): srv20 (com), srv21 (com), srv22 (com), +2 more"
    assert sum(len(v.split(", ")) for v in remaining.values()) == 11 and "11 more servers match" in more
    short, kept, none = lookup._text(matches[:3])
    assert short.splitlines()[0] == "| Name | Model | Power | Source |" and len(short.splitlines()) == 5 and not kept and none == ""


async def test_server_summary(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("get_server_summary", {"server": "gen12"})).structured_content
    assert data["ilo_version"] == "iLO 7 1.21.00 Apr 07 2026"
    assert data["processors"]["model"] == "AMD EPYC 9375F 32-Core Processor"
    assert data["uid_light"] == "Off"
    assert data["post_state"] == "InPostDiscoveryComplete"


async def test_inventory_cache_is_on_by_default_and_respects_refresh_and_live_state(mcp_client, monkeypatch):
    requests = []

    def transport(request):
        requests.append((request.method, request.url.path))
        return _fake(request)

    monkeypatch.setattr(srv, "_transport", httpx.MockTransport(transport))
    args = {"servers": ["gen12"], "categories": ["processors"]}
    async with mcp_client as client:
        status = (await client.call_tool("get_inventory_cache_status", {})).structured_content
        assert status["enabled"] and status["ttl_seconds"] == 900 and not inventory_cache._db_path().exists()
        first = (await client.call_tool("get_inventory_snapshot", args)).structured_content
        assert inventory_cache._db_path().is_file()  # created privately on first use, no setup step
        first_requests = len(requests)
        second = (await client.call_tool("get_inventory_snapshot", args)).structured_content
        second_requests = requests[first_requests:]
        a = first["servers"][0]["inventory"]["processors"]
        b = second["servers"][0]["inventory"]["processors"]
        assert a["source"] == "live" and b["source"] == "cache"
        assert a["data"] == b["data"] and a["collected_at"] == b["collected_at"]
        assert any(method == "POST" and "Sessions" in path for method, path in second_requests)
        assert not any(method == "GET" and "Processors" in path for method, path in second_requests)
        fresh = (await client.call_tool("get_inventory_snapshot", {**args, "refresh": True})).structured_content
        assert fresh["servers"][0]["inventory"]["processors"]["source"] == "live"
        await client.call_tool("get_health", {"server": "gen12"})
        assert any(method == "GET" and path.endswith("/Systems/1/") for method, path in requests[first_requests:])
        cleared = (await client.call_tool("clear_inventory_cache", {})).structured_content
        assert cleared["removed"] == 1
        after_clear = (await client.call_tool("get_inventory_snapshot", args)).structured_content
        assert after_clear["servers"][0]["inventory"]["processors"]["source"] == "live"
        await client.call_tool("set_inventory_cache", {"enabled": False})
        disabled = (await client.call_tool("get_inventory_snapshot", args)).structured_content
        assert disabled["servers"][0]["inventory"]["processors"]["source"] == "live" and not disabled["cache_enabled"]
        again = (await client.call_tool("get_inventory_snapshot", args)).structured_content
        assert again["servers"][0]["inventory"]["processors"]["source"] == "live"  # off means off
    assert b"good" not in inventory_cache._db_path().read_bytes()


async def test_unusable_cache_falls_back_to_a_live_read(mcp_client):
    import sqlite3

    path = inventory_cache._db_path()
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
        db.execute("INSERT INTO unrelated VALUES ('keep')")
    async with mcp_client as client:
        result = (await client.call_tool("get_inventory_snapshot", {"servers": ["gen12"], "categories": ["processors"]})).structured_content
    assert not result["cache_enabled"] and "unavailable" in result["cache_note"]
    assert result["servers"][0]["inventory"]["processors"]["source"] == "live"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM unrelated").fetchone() == ("keep",)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_cache_directory_and_database_have_private_windows_acls(mcp_client):
    try:
        inventory_cache.configure(True)
    except inventory_cache.CacheError as exc:
        if isinstance(exc.__cause__, subprocess.CalledProcessError):
            pytest.fail(f"{exc}: {exc.__cause__.stderr.decode(errors='replace')}")
        raise

    script = """
$ErrorActionPreference = 'Stop'
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
foreach ($acl in @([System.IO.Directory]::GetAccessControl($env:CACHE_DIR),
                   [System.IO.File]::GetAccessControl($env:CACHE_DB))) {
    $rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
    if ($rules.Count -ne 1 -or $rules[0].IdentityReference -ne $sid -or
        $rules[0].AccessControlType -ne 'Allow' -or
        ($rules[0].FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -ne
          [System.Security.AccessControl.FileSystemRights]::FullControl) {
        throw "Cache ACL must grant only the current user full control"
    }
}
if (-not [System.IO.Directory]::GetAccessControl($env:CACHE_DIR).AreAccessRulesProtected) {
    throw "Cache directory must not inherit parent permissions"
}
if (-not (@([System.IO.File]::GetAccessControl($env:CACHE_DB).Access)[0].IsInherited)) {
    throw "Cache database must inherit the private directory permissions"
}
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "CACHE_DIR": str(inventory_cache._db_path().parent), "CACHE_DB": str(inventory_cache._db_path())},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


async def test_cached_snapshot_still_requires_login_and_scopes_identity(mcp_client):
    async with mcp_client as client:
        await client.call_tool("set_inventory_cache", {"enabled": True})
        result = (await client.call_tool("get_inventory_snapshot", {"servers": ["gen12"], "categories": ["processors"]})).structured_content
        assert result["servers"][0]["inventory"]["processors"]["source"] == "live"
        ini = srv.config_path()
        ini.write_text(ini.read_text().replace("[gen12]\nhost = 192.0.2.10", "[gen12]\nhost = 192.0.2.10\npassword = wrong"))
        failed = (await client.call_tool("get_inventory_snapshot", {"servers": ["gen12"], "categories": ["processors"]})).structured_content
        assert "error" in failed["servers"][0] and "inventory" not in failed["servers"][0]


async def test_com_snapshot_caches_inventory_but_reads_live_server_list(mcp_client, monkeypatch):
    requests = []

    def transport(request):
        requests.append((request.method, request.url.path))
        return _fake(request)

    monkeypatch.setattr(srv, "_transport", httpx.MockTransport(transport))
    args = {"name_contains": "esx2"}
    async with mcp_client as client:
        await client.call_tool("set_inventory_cache", {"enabled": True})
        first = (await client.call_tool("get_com_inventory_snapshot", args)).structured_content
        assert first["servers"] and all(row["inventory"]["source"] == "live" for row in first["servers"])
        requests.clear()
        second = (await client.call_tool("get_com_inventory_snapshot", args)).structured_content
        assert all(row["inventory"]["source"] == "cache" for row in second["servers"])
        assert any(path.endswith("/servers") for _, path in requests)
        assert not any(path.endswith("/inventory") for _, path in requests)
        refreshed = (await client.call_tool("get_com_inventory_snapshot", {**args, "refresh": True})).structured_content
        assert all(row["inventory"]["source"] == "live" for row in refreshed["servers"])
    assert b"csecret" not in inventory_cache._db_path().read_bytes()


async def test_cache_expiry_clear_race_and_invalid_database(mcp_client):
    import sqlite3
    import time

    async with mcp_client as client:
        await client.call_tool("set_inventory_cache", {"enabled": True, "ttl_seconds": 1})
        args = {"servers": ["gen12"], "categories": ["processors"]}
        await client.call_tool("get_inventory_snapshot", args)
        with sqlite3.connect(inventory_cache._db_path()) as db:
            db.execute("UPDATE snapshots SET collected_at = ?", (time.time() - 10,))
        refreshed = (await client.call_tool("get_inventory_snapshot", args)).structured_content
        assert refreshed["servers"][0]["inventory"]["processors"]["source"] == "live"
        generation = await inventory_cache.generation()
        await client.call_tool("clear_inventory_cache", {})
        assert not await inventory_cache.put("in-flight", {"safe": True}, time.time(), 1, generation)
        assert await inventory_cache.get("in-flight", 1) is None


async def test_cache_keeps_newer_concurrent_results(mcp_client):
    import time

    async with mcp_client as client:
        await client.call_tool("set_inventory_cache", {"enabled": True})
        generation = await inventory_cache.generation()
        now = time.time()
        assert await inventory_cache.put("newer", {"value": 2}, now - 1, 60, generation)
        assert await inventory_cache.put("other", {"value": 1}, now - 2, 60, generation)
        assert not await inventory_cache.put("newer", {"value": 0}, now - 3, 60, generation)
        assert (await inventory_cache.get("newer", 60))[0] == {"value": 2}
        assert (await inventory_cache.get("other", 60))[0] == {"value": 1}


async def test_cache_does_not_overwrite_unrelated_database(mcp_client):
    import sqlite3

    path = inventory_cache._db_path()
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
        db.execute("INSERT INTO unrelated VALUES ('keep')")
    async with mcp_client as client:
        with pytest.raises(ToolError, match="unexpected format"):
            await client.call_tool("set_inventory_cache", {"enabled": True})
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM unrelated").fetchone() == ("keep",)


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
        mine = (await c.call_tool("get_ilo_logins", {"servers": ["gen12"], "include_this_computer": True})).structured_content
    # proliant-mcp's own session (192.0.2.50, this computer) is left out unless asked for
    assert data["totals"] == {"logins": 3, "this_computer_logins": 1, "failed_logins": 1, "security_alerts": 1, "servers": 1, "unreachable": 1}
    gen12 = next(s for s in data["servers"] if s["server"] == "gen12")
    assert gen12["by_type"] == {"Browser": 2, "Host REST": 1}
    assert gen12["users"] == {"admin": 2, "System Administrator": 1}
    assert gen12["sources"] == [{"ip": "192.0.2.60", "host": None, "logins": 2, "last_login": gen12["sources"][0]["last_login"]}]
    assert gen12["failures"][0]["ip"] == "192.0.2.66" and "unauthorized" in gen12["security_alerts"][0]["message"]
    assert gen12["entries_with_invalid_time"] == 2 and gen12["log_covers_window"] is True
    assert len(data["by_day"]) == 7 and sum(d["total"] for d in data["by_day"]) == 3 and "gen12" in data["by_day"][0]
    assert "UnauthorizedLoginAttempt" in next(s for s in data["servers"] if s["server"] == "badpass")["error"]
    assert any("1 logins from this computer" in n for n in data["notes"]) and any("invalid clock" in n for n in data["notes"])
    assert mine["totals"]["logins"] == 4 and mine["window"]["this_computer_included"] is True
    src = mine["servers"][0]["sources"][1]
    assert src["this_computer"] is True and src["host"] == "admin-pc.example.com"


async def test_event_log_severity_floor_summary_and_fleet_summary(mcp_client):
    async with mcp_client as c:
        floor = (await c.call_tool("get_event_log", {"server": "gen12", "min_severity": "Warning"})).structured_content["result"]
        grouped = (await c.call_tool("get_event_log", {"server": "gen12", "summary": True})).structured_content["result"]
        fleet = (await c.call_tool("get_fleet_event_summary", {})).structured_content
    assert {e["severity"] for e in floor} == {"Critical", "Warning"} and len(floor) == 4
    # identical messages fold into one row: most severe first, counts summed
    assert [(g["severity"], g["message"], g["count"]) for g in grouped] == [
        ("Critical", "Server Critical Fault (Fan Board e-fuse failure)", 3),
        ("Warning", "Unsupported DIMM Configuration Detected", 1),
        ("OK", "Server power restored.", 1),
    ]
    assert grouped[0]["first"] < grouped[0]["last"] and grouped[0]["repaired"] is False
    gen12 = next(s for s in fleet["servers"] if s["server"] == "gen12")
    assert gen12["entries_by_severity"] == {"Critical": 3, "Warning": 1} and len(gen12["groups"]) == 2
    assert "error" in next(s for s in fleet["servers"] if s["server"] == "badpass")


async def test_server_summary_says_whether_an_os_runs(mcp_client, monkeypatch):
    import fake_ilo

    async with mcp_client as c:
        stuck = (await c.call_tool("get_server_summary", {"server": "gen12"})).structured_content["os"]
        system = {**fake_ilo.FAKE_ILO["/redfish/v1/Systems/1/"], "BootProgress": {"LastState": "OSRunning"},
                  "Oem": {"Hpe": {"PostState": "FinishedPost", "HostOS": {"OsName": "VMware ESXi", "OsVersion": "8.0.3"},
                                  "AggregateHealthStatus": {"AgentlessManagementService": "Ready"}}}}
        monkeypatch.setitem(fake_ilo.FAKE_ILO, "/redfish/v1/Systems/1/", system)
        running = (await c.call_tool("get_server_summary", {"server": "gen12"})).structured_content["os"]
    assert stuck["state"] == "stopped in BIOS setup (RBSU); no OS booted"
    assert stuck["boot_order"] == ["Generic USB Boot", "UEFI HTTPv4 (Embedded LOM 1)"] and stuck["name"] is None
    assert stuck["os_boot_entries"] == []  # devices and network boot are not OS loaders
    assert (running["state"], running["name"], running["version"], running["hpe_agent"]) == (
        "running VMware ESXi (reported by the HPE agent)", "VMware ESXi", "8.0.3", "Ready")


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
    assert f"{len(await srv.mcp.list_tools())} tools" in out  # never hard-code the count: every new tool changes it
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
    assert info["active_alerts"] == {"count": 2, "by_severity": {"Warning": 1, "Critical": 1}}


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

    async with mcp_client as c:
        l40s = (await c.call_tool("com_list_gpus", {"gpu_model": "l40S"})).structured_content  # any case
        nothing = (await c.call_tool("com_list_gpus", {"gpu_model": "H100", "include_servers_without_gpus": True})).structured_content
    assert [s["name"] for s in l40s["servers"]] == ["gpu1.example.com"]
    assert (l40s["servers_checked"], l40s["servers_with_gpus"], l40s["total_gpus"], l40s["gpus_by_model"]) == (4, 1, 2, {"NVIDIA L40S": 2})
    assert [g["name"] for g in l40s["servers"][0]["gpus"]] == ["NVIDIA L40S"] and l40s["servers"][0]["gpu_count"] == 2  # only the matching GPUs
    assert nothing["servers"] == [] and nothing["total_gpus"] == 0 and nothing["inventory_unavailable"] == ["dl345.example.com"]


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


async def test_com_list_servers_table_has_exact_counts_and_a_short_default_table(mcp_client):
    from collections import Counter

    async with mcp_client as c:
        rows = (await c.call_tool("com_list_servers", {})).structured_content["result"]
        out = (await c.call_tool("com_list_servers_table", {})).structured_content
        wide = (await c.call_tool("com_list_servers_table", {"columns": ["name", "serial", "ilo_host", "os"]})).structured_content
        status = (await c.call_tool("com_list_servers_table", {"columns": ["name", "power", "connected"]})).structured_content
        one = (await c.call_tool("com_list_servers_table", {"name_contains": "gpu1"})).structured_content
        none = (await c.call_tool("com_list_servers_table", {"name_contains": "no-such-server"})).structured_content
    lines = out["table"].splitlines()
    assert out["total"] == len(rows) and len(lines) == len(rows) + 2  # header, separator, one line per server
    assert out["columns"] == ["Name", "Model", "Health"]
    assert out["counts"] == {"by_health": dict(Counter(r["health"] or "UNKNOWN" for r in rows))}  # only counts for shown columns
    assert status["counts"]["connected_to_com"] == {"yes": sum(1 for r in rows if r["connected"]), "no": sum(1 for r in rows if not r["connected"])}
    assert sum(status["counts"]["by_power"].values()) == len(rows) and "do not recount" in out["note"]
    assert all(r["name"] in out["table"] for r in rows) and "firmware" not in out["table"].lower()
    assert wide["columns"] == ["Name", "Serial", "iLO host", "OS"] and wide["table"].splitlines()[0] == "| Name | Serial | iLO host | OS |"
    assert one["total"] == 1 and "gpu1.example.com" in one["table"]
    assert none["total"] == 0 and none["table"].startswith("No servers match.")
    assert none["available"]["servers"] == 4 and none["available"]["generations"] == {"Gen10": 2, "Gen11": 1, "Gen12": 1}  # what does exist
    assert "available" not in one  # only an empty answer carries it


async def test_com_list_servers_table_hardware_columns(mcp_client):
    async with mcp_client as c:
        out = (await c.call_tool("com_list_servers_table", {"columns": ["name", "gpu", "storage", "nics", "cpu_model"]})).structured_content
    rows = {line.split(" | ")[0].strip("| "): line for line in out["table"].splitlines()[2:]}
    assert "2 x NVIDIA L40S" in rows["gpu1.example.com"] and "| 10/25Gb 2-port SFP28 BCM57414 OCP3 Adapter 2p Ethernet," in rows["gpu1.example.com"]
    assert "| none | 2 x 960GB SSD, 1 x 1920GB SSD, 2 x 3200GB NVMe (10.24 TB) |" in rows["esx2.example.com"]
    assert "| unknown | unknown | unknown | unknown |" in rows["dl345.example.com"]
    assert out["counts"]["gpus_by_model"]["NVIDIA L40S"] >= 2 and out["counts"]["servers_with_unknown_gpus"] >= 1


def test_fleet_table_compact_layout_for_a_big_fleet_lists_every_server_once():
    from proliant_mcp import fleet_table

    rows = [{"name": f"ok{i}", "model": "ProLiant DL360 Gen10", "health": "OK"} for i in range(14)]
    rows += [{"name": "bad1", "model": "HPE ProLiant Compute DL380a Gen12", "health": "CRITICAL"}, {"name": "ok14", "model": "ProLiant DL360 Gen10", "health": "OK"}]
    out = fleet_table.build(rows)
    lines = out["table"].splitlines()
    assert out["layout"] == "compact" and lines[0] == "16 servers: 15 OK, 1 CRITICAL"
    assert "| bad1 | DL380a Gen12 | CRITICAL |" in lines and lines[-1].startswith("DL360 Gen10 (15): ok0, ok1")
    assert all(r["name"] in out["table"] for r in rows)
    assert fleet_table.build(rows, ["name"])["layout"] == "table" and fleet_table.build(rows[:3])["layout"] == "table"  # asked for columns, or small


async def test_com_list_servers_table_generation_filter_and_controller_firmware(mcp_client):
    async with mcp_client as c:
        all_rows = (await c.call_tool("com_list_servers", {})).structured_content["result"]
        gen10 = (await c.call_tool("com_list_servers_table", {"generation": "Gen10", "columns": ["name", "generation"]})).structured_content
        raw11 = (await c.call_tool("com_list_servers", {"generation": "11"})).structured_content["result"]
        both = (await c.call_tool("com_list_servers_table", {"columns": ["name", "storage_controllers", "nic_firmware"]})).structured_content
        nics = (await c.call_tool("com_list_nics", {"generation": "GEN_11"})).structured_content
    assert gen10["total"] == sum(1 for r in all_rows if r["generation"] == "GEN_10") and "GEN_11" not in gen10["table"]
    assert [r["generation"] for r in raw11] == ["GEN_11"] and nics["servers_checked"] == 1
    cells = {line.split(" | ")[0].strip("| "): line for line in both["table"].splitlines()[2:]}
    assert "Smart Array P408i-a SR Gen10 fw " in cells["esx2.example.com"] and "| unknown | unknown |" in cells["dl345.example.com"]
    assert "10/25Gb 2-port SFP28 BCM57414 OCP3 Adapter fw " in cells["gpu1.example.com"]


async def test_storage_controller_filter_keeps_matches_and_counts_unknown_separately(mcp_client):
    async with mcp_client as c:
        every = (await c.call_tool("com_list_servers", {})).structured_content["result"]
        table = (await c.call_tool("com_list_servers_table", {"storage_controller": "p-408 I"})).structured_content
        report = (await c.call_tool("com_hardware_report", {"storage_controller": "P408"})).structured_content
        nothing = (await c.call_tool("com_list_servers_table", {"storage_controller": "NS204"})).structured_content
    f = table["counts"]["storage_controller_filter"]
    assert table["total"] == f["servers_with"] >= 1
    assert "esx2.example.com" in table["table"] and "Smart Array P408i-a" in table["table"]  # default columns show the controller
    assert "dl345.example.com" in f["unknown_servers"] and "dl345.example.com" not in table["table"]  # no inventory: unknown, not "without"
    assert f["servers_with"] + f["servers_without"] + f["servers_unknown"] == len(every)
    assert report["storage_controller_filter"]["servers_with"] == report["servers_checked"] == f["servers_with"]
    assert nothing["total"] == 0 and nothing["counts"]["storage_controller_filter"]["servers_unknown"] == f["servers_unknown"]


def test_fleet_table_cells_cannot_break_the_table():
    from proliant_mcp import fleet_table

    rows = [
        {"name": "a|b", "model": "line\nbreak", "health": None, "power_state": "ON", "connected": None, "ilo": {"ip": None}},
        {"name": "ok", "model": "M", "health": "CRITICAL", "power_state": None, "connected": True, "ilo": {"ip": "192.0.2.1"}},
    ]
    out = fleet_table.build(rows, ["bogus", "name", "model", "health", "connected", "ilo_ip"])
    assert out["table"].splitlines() == [
        "| Name | Model | Health | Connected | iLO IP |",
        "|---|---|---|---|---|",
        "| a\\|b | line break | UNKNOWN | - | - |",
        "| ok | M | CRITICAL | Yes | 192.0.2.1 |",
    ]
    assert out["counts"]["by_health"] == {"UNKNOWN": 1, "CRITICAL": 1}
    assert fleet_table.build([], ["bogus"])["columns"] == ["Name", "Model", "Health"]


async def test_server_list_tools_steer_the_model_in_their_first_80_characters(mcp_client):
    """Hermes (and clients like it) list deferred tools as `name: first sentence`, clipped to 80 characters, so the
    model picks a tool from that alone. Without this, "list my servers in com" went to the 35 KB raw JSON tool."""
    async with mcp_client as c:
        tools = {t.name: " ".join((t.description or "").split()) for t in await c.list_tools()}
    assert "com_list_servers_table" in tools["com_list_servers"][:80]
    assert tools["com_list_servers_table"].lower().startswith("list or show") and "." in tools["com_list_servers_table"][:80]


async def test_tool_definitions_stay_small(mcp_client):
    """Every tool's name, description and schema is sent to the model on every turn (about 200 tokens a tool),
    so they must stay short. If this fails, shorten the new text (the tool's runtime next_step can carry details)
    before raising the limits."""
    import json

    async with mcp_client as c:
        tools = await c.list_tools()
    sizes = {t.name: len(json.dumps({"name": t.name, "description": t.description or "", "parameters": t.input_schema})) for t in tools}
    assert sum(sizes.values()) <= 43_800, f"all tool definitions: {sum(sizes.values())} chars"  # +1000 for the 3 iLO firmware tools, +300 for list_servers generation/model
    assert len(srv.INSTRUCTIONS) <= 3_800
    too_long = {t.name: len(t.description or "") for t in tools if len(t.description or "") > 600}
    assert not too_long, f"descriptions over 600 chars: {too_long}"


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
        "total_gb": 10240,  # 960 + 960 + 1920 behind the controller, 2 x 3200 direct (empty bay ignored)
        "unsized_drives": 0,
    }
    assert storage["gpu1.example.com"] == {"controllers": [], "direct_drives": [], "drive_count": 0, "total_gb": 0, "unsized_drives": 0, "known": False}
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


async def test_com_list_nics_reports_modes_ports_and_speeds_for_the_fleet(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("com_list_nics", {})).structured_content
        report = (await c.call_tool("com_hardware_report", {})).structured_content
        detail = (await c.call_tool("com_get_server", {"server": "gpu1.example.com"})).structured_content
    assert data["nic_modes"] == {
        "FibreChannel": {"adapters": 1, "ports": 2, "ports_up": 2},
        "Ethernet": {"adapters": 2, "ports": 3, "ports_up": 2},  # the VPI card's Ethernet port counts here
        "InfiniBand": {"adapters": 1, "ports": 1, "ports_up": 0},
    }
    assert data["most_used_nic_mode"] == "Ethernet"  # ties on linked ports go to the mode with more adapters
    assert data["linked_ports_by_speed"] == {"16Gb FibreChannel": 2, "25Gb Ethernet": 1, "100Gb Ethernet": 1}
    assert data["adapters_by_model"]["HPE SN1620E 32Gb 2p FC HBA (FibreChannel)"] == {"adapters": 1, "ports": 2, "ports_up": 2, "speeds_up": ["2 x 16Gb"]}
    assert data["most_used_model"]["by_linked_ports"] == "HPE SN1620E 32Gb 2p FC HBA (FibreChannel)"
    assert data["nic_unknown"] == ["old-t4.example.com", "dl345.example.com"]  # no network inventory: unknown, not none
    gpu1 = next(s for s in data["servers"] if s["name"] == "gpu1.example.com")["adapters"]
    assert gpu1[0]["name"] == "10/25Gb 2-port SFP28 BCM57414 OCP3 Adapter"  # SKU, not the bare 'BCM57414'
    assert (gpu1[1]["mode"], gpu1[1]["speeds_up"]) == ("Ethernet+InfiniBand", ["1 x 100Gb Ethernet"])
    gpu_config = next(g for g in report["configurations"] if g["servers"] == ["gpu1.example.com"])
    assert gpu_config["nics"] == [
        "1 x 10/25Gb 2-port SFP28 BCM57414 OCP3 Adapter (Ethernet, 2p)",
        "1 x HPE InfiniBand HDR/Ethernet 200Gb 2-port QSFP56 MCX653436A-HDAI Adapter (Ethernet+InfiniBand, 2p)",
    ]
    assert report["nic_unknown"] == ["old-t4.example.com", "dl345.example.com"] and report["most_used_nic_mode"] == "Ethernet"
    ports = detail["hardware"]["network"]["adapters"][1]["port_detail"]
    assert [(p["mode"], p["link"], p["mac"]) for p in ports] == [("Ethernet", "LinkUp", "02:00:00:00:00:01"), ("InfiniBand", "LinkDown", "02:00:00:00:00:02")]


async def test_com_list_nics_ports_filter_answers_with_a_short_grouped_list(mcp_client):
    async with mcp_client as c:
        full = (await c.call_tool("com_list_nics", {})).structured_content
        short = (await c.call_tool("com_list_nics", {"ports": 2})).structured_content
        none = (await c.call_tool("com_list_nics", {"ports": 4})).structured_content
        linked = (await c.call_tool("com_list_nics", {"ports": 2, "linked_only": True})).structured_content
    counts = short["counts"]
    assert counts["servers_checked"] == full["servers_checked"] and counts["servers_with_nic_data"] == full["servers_with_nic_data"]
    assert counts["servers_without_nic_data"] == len(full["nic_unknown"]) == 2  # unknown, not none
    assert counts["adapters_matching"] == 3 and short["layout"] == "all" and "remaining" not in short
    assert counts["by_adapter"] == {
        "10/25Gb 2-port SFP28 BCM57414 OCP3 Adapter": 1,
        "HPE InfiniBand HDR/Ethernet 200Gb 2-port QSFP56 MCX653436A-HDAI Adapter": 1,
        "HPE SN1620E 32Gb 2p FC HBA": 1,
    }
    assert short["total"] == counts["servers_with_match"]
    # every matching server is named once per matching adapter, with ports up out of ports
    assert "gpu1.example.com (1/2)" in short["table"].split("MCX653436A-HDAI Adapter (1):")[1]
    first = short["table"].splitlines()[0]
    assert first == f"{short['total']} of {counts['servers_with_nic_data']} servers with NIC data have a 2-port adapter (2 more servers have no NIC data):"
    assert none["total"] == 0 and none["counts"]["adapters_matching"] == 0 and none["table"].startswith("No server with NIC data has a 4-port adapter")
    assert linked["counts"]["adapters_matching"] == 3 and "with a link up" in linked["table"]  # each 2-port adapter has a port up


def test_ports_summary_of_a_long_list_shows_samples_and_keeps_the_rest_for_a_follow_up():
    def adapter(name: str, up: int) -> dict:
        return {"name": name, "ports": 4, "ports_up": up, "mode": "Ethernet", "speeds_up": []}

    rows = [
        {"id": f"s{i}", "name": f"srv{i:02d}", "hardware": {"network": {"known": True, "adapters": [adapter("Card A 4p" if i < 15 else "Card B 4p", i % 2)]}}}
        for i in range(20)
    ] + [{"id": "u1", "name": "unknown1", "hardware": {}}]
    data = com._ports_summary(rows, 4, False)
    assert data["layout"] == "sample" and data["total"] == 20
    assert data["counts"]["by_adapter"] == {"Card A 4p": 15, "Card B 4p": 5}
    assert data["counts"]["servers_without_nic_data"] == 1 and data["counts"]["servers_checked"] == 21
    lines = data["table"].splitlines()
    assert lines[0] == "20 of 20 servers with NIC data have a 4-port adapter (1 more servers have no NIC data):"
    assert lines[1] == "Card A 4p (15): srv00 (0/4), srv01 (1/4), srv02 (0/4), +12 more"
    assert lines[2] == "Card B 4p (5): srv15 (1/4), srv16 (0/4), srv17 (1/4), +2 more"
    shown = sum(name in data["table"] for name in (f"srv{i:02d}" for i in range(20)))
    kept = sum(name in data["remaining"]["Card A 4p"] + data["remaining"]["Card B 4p"] for name in (f"srv{i:02d}" for i in range(20)))
    assert shown == 6 and kept == 14 and shown + kept == 20  # every server is in exactly one of the two
    assert "14 more servers match" in data["note"] and "Do NOT show `remaining`" in data["note"]


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


async def test_oneview_uplink_sets_report_ports_roles_neighbors_and_issues(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("oneview_list_uplink_sets", {})).structured_content
    assert (data["uplink_sets"], data["by_status"], data["ports"]) == (3, {"Critical": 1, "OK": 2}, {"total": 5, "linked": 4})
    assert data["with_issues"] == ["ACI-MAP", "span"]
    aci, prod, span = data["sets"]  # Critical first
    assert aci["name"] == "ACI-MAP" and aci["alerts"] == ["Uplink set ACI-MAP does not contain any networks."]
    assert aci["issues"] == [
        "no networks: the uplink set carries no traffic",
        "not linked: Enclosure-01, interconnect 6 Q5:2",
        "no interconnect redundancy: all linked ports are on one module",
    ]
    assert prod["networks"] == ["prod (VLAN 160)"] and prod["issues"] == [] and (prod["ports_linked"], prod["ports_active"]) == (2, 1)
    assert [(p["interconnect"], p["port"], p["link"], p["role"], p["speed"], p["neighbor"]) for p in prod["ports"]] == [
        ("Enclosure-01, interconnect 3", "Q1:1", "Linked", "Active", "10G", "tor-01 Eth1/11"),
        ("Enclosure-01, interconnect 6", "Q1:1", "Linked", "StandBy", "10G", "tor-02 Eth1/11"),
    ]
    assert span["issues"] == ["no interconnect redundancy: all linked ports are on one module"]
    # A switch still named "switch" (the default) is identified by its chassis ID, and listed once however many ports it has
    assert [p["neighbor"] for p in aci["ports"] if p["link"] == "Linked"] == ["switch (78:0c:f0:7d:8a:c3) Eth1/6"]
    assert span["ports"][0]["neighbor"] == "switch (78:0c:f0:7d:8a:c8) Eth1/10" and span["ports"][0]["neighbor_chassis_id"] == "78:0c:f0:7d:8a:c8"
    # the two "switch" ports have chassis MACs that differ only in the last byte: one switch, not two
    by_name = {s["name"]: s for s in data["neighbor_switches"]}
    assert sorted(by_name) == ["switch", "tor-01", "tor-02"] and by_name["switch"]["software"].startswith("Cisco Nexus")
    assert by_name["switch"]["chassis_ids"] == ["78:0c:f0:7d:8a:c3", "78:0c:f0:7d:8a:c8"] and len(by_name["switch"]["connections"]) == 2
    assert "Do not guess how many switches" in data["note"]


async def test_find_server_searches_every_source_and_probes_unknown_ilos(mcp_client, monkeypatch):
    def world(request: httpx.Request) -> httpx.Response:
        if request.url.host == "192.0.2.99":
            raise httpx.ConnectError("no route to host")
        if request.url.host == "192.0.2.77":
            if request.url.path == "/xmldata":
                return httpx.Response(200, text="<RIMP><HSI><SBSN>VG13525009</SBSN><SPN>ProLiant DL525 Gen13</SPN></HSI>"
                                                 "<MP><PN>Integrated Lights-Out 8 (iLO 8)</PN><FWRI>1.10</FWRI></MP></RIMP>")
            return httpx.Response(200, json={"Product": "ProLiant DL525 Gen13", "Vendor": "HPE",
                                             "Oem": {"Hpe": {"Manager": [{"ManagerType": "iLO 8", "ManagerFirmwareVersion": "1.10", "HostName": "ILOVG13525009"}]}}})
        return _fake(request)

    monkeypatch.setattr(srv, "_transport", httpx.MockTransport(world))
    async with mcp_client as c:
        by_ip = (await c.call_tool("find_server", {"query": "192.0.2.50"})).structured_content
        module = (await c.call_tool("find_server", {"query": "vc100-bay6"})).structured_content
        inventory = (await c.call_tool("find_server", {"query": "192.0.2.10"})).structured_content
        unknown = (await c.call_tool("find_server", {"query": "192.0.2.77"})).structured_content
        gone = (await c.call_tool("find_server", {"query": "192.0.2.99"})).structured_content
    assert by_ip["found"] and by_ip["compute_ops_management"][0]["matches"][0]["name"] == "esx2.example.com"
    assert by_ip["oneview"][0]["server_hardware"][0]["name"] == "Enclosure-01, bay 2" and "probe" not in by_ip
    assert module["oneview"][0]["interconnects"][0]["hostname"] == "VC100-bay6"
    assert inventory["ilo_inventory"] == [{"name": "gen12", "ilo_host": "192.0.2.10"}]
    assert not unknown["found"] and unknown["probe"] == {
        "host": "192.0.2.77", "reachable": True, "redfish_product": "ProLiant DL525 Gen13", "redfish_vendor": "HPE",
        "ilo_type": "iLO 8", "ilo_firmware": "1.10", "ilo_hostname": "ILOVG13525009",
        "serial_number": "VG13525009", "server_model": "ProLiant DL525 Gen13", "ilo_model": "Integrated Lights-Out 8 (iLO 8)",
    }
    assert gone["probe"]["reachable"] is False and "cannot reach" in gone["probe"]["error"]


async def test_com_hardware_report_counts_servers_over_a_storage_size(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("com_hardware_report", {"storage_over_tb": 1})).structured_content
        everything = (await c.call_tool("com_hardware_report", {})).structured_content
        huge = (await c.call_tool("com_hardware_report", {"storage_over_tb": 10.24})).structured_content  # not strictly over
    # esx2 holds 10240 GB; gpu1/old-t4 have no storage inventory and dl345 has none: unknown, never counted as small
    assert data["storage_filter"] == {"over_tb": 1, "servers_over": 1, "servers_not_over": 0, "servers_unknown": 3}
    assert [(g["servers"], g["total_storage_gb"]) for g in data["configurations"]] == [(["esx2.example.com"], 10240)]
    assert "storage_filter" not in everything and len(everything["configurations"]) == 4
    assert (huge["storage_filter"]["servers_over"], huge["storage_filter"]["servers_not_over"], huge["configurations"]) == (0, 1, [])


async def test_greenlake_list_services_joins_catalog_and_provisions(mcp_client):
    async with mcp_client as c:
        data = (await c.call_tool("greenlake_list_services", {})).structured_content
        only = (await c.call_tool("greenlake_list_services", {"provisioned_only": True})).structured_content
    assert (data["services_available"], data["services_provisioned"]) == (3, 1)
    assert [(s["name"], s["provisioned"]) for s in data["services"]] == [("Compute Ops Management", True), ("Data Services", False), ("OpsRamp", False)]
    assert data["services"][0]["regions"] == [{"region": "eu-central", "status": "PROVISIONED"}, {"region": "us-west", "status": "PROVISIONED"}]
    assert data["services"][1]["regions"] == [{"region": "us-west", "status": "FAILED", "reason": "quota"}]  # not ready, not "provisioned"
    assert data["services"][0]["description"] == "Securely manage your compute."
    assert [s["name"] for s in only["services"]] == ["Compute Ops Management"]


def test_generic_intel_gpu_name_resolved_from_pci_id():
    intel = {"Name": "Intel Video Controller", "Manufacturer": "Intel", "ProcessorType": "GPU", "Status": {"Health": "OK"}}
    pci = [
        {"Name": "Embedded Video Controller", "PCIeFunctions": [{"VendorId": "0X102B", "DeviceId": "0X0538", "DeviceClass": "DisplayController"}]},
        {"Name": "Intel Video Controller", "PCIeFunctions": [{"VendorId": "0X8086", "DeviceId": "0X0BDA", "DeviceClass": "DisplayController"}]},
    ]
    hw = com.hardware_summary({"processor": {"data": [intel, intel]}, "pciDevice": {"data": pci}})
    assert [(g["name"], g["count"]) for g in hw["gpus"]] == [("Intel Data Center GPU Max 1100", 2)]


async def test_usage_stats_are_on_by_default_and_count_names_and_timings_only(mcp_client):
    import sqlite3

    from proliant_mcp import usage_stats

    usage_stats._state["checked"] = None
    async with mcp_client as c:
        await c.call_tool("list_servers", {})
        await c.call_tool("com_list_servers_table", {"columns": ["name"]})
        with pytest.raises(Exception):
            await c.call_tool("get_health", {"server": "no-such-server"})
        on = (await c.call_tool("get_usage_stats", {})).structured_content
    by_tool = {t["tool"]: t for t in on["by_tool"]}
    assert on["enabled"] and on["total_calls"] == 3 and on["errors"] == 1
    assert by_tool["list_servers"]["calls"] == 1 and by_tool["get_health"]["errors"] == 1
    assert "get_usage_stats" not in by_tool and on["busiest_bursts"][0]["calls"] == 3
    assert list(on["by_client"]) and "unknown" not in on["by_client"]
    db = sqlite3.connect(usage_stats._db_path())
    columns = [r[1] for r in db.execute("PRAGMA table_info(calls)")]
    stored = " ".join(str(v) for row in db.execute("SELECT * FROM calls") for v in row)
    db.close()
    assert columns == ["ts", "tool", "client", "ms", "ok", "bytes"] and "no-such-server" not in stored and "example.com" not in stored
    async with mcp_client as c:
        assert (await c.call_tool("set_usage_stats", {"enabled": False})).structured_content["enabled"] is False
        await c.call_tool("list_servers", {})
        assert (await c.call_tool("get_usage_stats", {})).structured_content["total_calls"] == 3  # off: nothing new counted
        assert (await c.call_tool("set_usage_stats", {"enabled": False, "clear": True})).structured_content["cleared"] == 3
        assert (await c.call_tool("get_usage_stats", {})).structured_content["total_calls"] == 0


async def test_guide_resource_routes_questions_to_real_tools(mcp_client):
    """Some assistants discover a server through its resources (OpenCode's list_mcp_resources); every tool the guide names must exist."""
    import re

    async with mcp_client as c:
        resources = await c.list_resources()
        text = (await c.read_resource("proliant://guide"))[0].text
        names = {t.name for t in await c.list_tools()}
    assert [str(r.uri) for r in resources] == ["proliant://guide"]
    named = set(re.findall(r"\b((?:com|oneview|get|greenlake|find|list)_[a-z_]+)\b", text))
    assert named and named <= names, named - names
    assert "COM" in text and "NS204" in text


async def test_hardware_report_lists_servers_without_any_inventory(mcp_client):
    async with mcp_client as c:
        report = (await c.call_tool("com_hardware_report", {})).structured_content
    unknown = set(report["storage_unknown"]) & set(report["gpu_unknown"]) & set(report["nic_unknown"])
    assert set(report["no_inventory"]) == unknown and "dl345.example.com" in unknown


_SDR = "https://downloads.linux.hpe.com/SDR/repo/fwpp-gen12/"
_ADD_FROM_URI = "/redfish/v1/UpdateService/Actions/Oem/Hpe/HpeiLOUpdateServiceExt.AddFromUri/"


class FlashingIlo:
    """The fake iLO plus HPE's repository; AddFromUri plays the real iLO 7 states, an iLO reset and the new version."""

    def __init__(self) -> None:
        self.version = "1.21.00 Apr 07 2026"
        self.script: list = []
        self.posts: list[dict] = []
        self.idle = "Idle"  # State reported outside a scripted update
        self.after_post: list | None = None  # replaces the default script after AddFromUri

    def _sdr(self, request: httpx.Request) -> httpx.Response:
        listings = {
            "/SDR/repo/fwpp-gen12/": '<a href="2026.07.00.00/">x</a><a href="2026.09.00.00/">x</a>',
            "/SDR/repo/fwpp-gen12/2026.07.00.00/": '<a href="ilo7_1.24.00.fwpkg">x</a>',
            "/SDR/repo/fwpp-gen12/2026.09.00.00/": '<a href="ilo7_1.24.00.fwpkg">x</a><a href="ilo7_1.25.02.fwpkg">x</a>',
            "/SDR/repo/fwpp-gen12/2026.09.00.00/ilo7_1.25.02.json": json.dumps({"MinimumActiveVersion": "1.24.00"}),
            "/SDR/repo/fwpp-gen12/2026.09.00.00/ilo7_1.24.00.json": json.dumps({"Package": {"ReleaseDate": "2026-07-25T00:00:00"}}),
        }
        path = request.url.path
        return httpx.Response(200, text=listings[path]) if path in listings else httpx.Response(404)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "downloads.linux.hpe.com":
            return self._sdr(request)
        if self.script and self.script[0] == "offline":
            self.script.pop(0)
            raise httpx.ConnectError("iLO resetting", request=request)
        path = request.url.path
        if request.method == "POST" and path == _ADD_FROM_URI:
            body = json.loads(request.read())
            self.posts.append(body)
            new = body["ImageURI"].rsplit("ilo7_", 1)[1].removesuffix(".fwpkg")
            self.script = [("Uploading", None), ("Updating", 40), ("Updating", 100), ("Complete", None), *["offline"] * 4, ("new", new)]
            if self.after_post is not None:
                self.script = [*self.after_post, ("new", new)]
            return httpx.Response(200, json={})
        if request.method == "GET" and path == "/redfish/v1/UpdateService/" and request.headers.get("X-Auth-Token") == "tok":
            state, percent = self.idle, None
            if self.script and isinstance(self.script[0], tuple):
                state, percent = self.script.pop(0)
                if state == "new":
                    self.version, state, percent = f"{percent} Jan 01 2027", "Idle", None
            return httpx.Response(200, json={"Oem": {"Hpe": {"State": state, "FlashProgressPercent": percent, "Actions": {
                "#HpeiLOUpdateServiceExt.AddFromUri": {"target": _ADD_FROM_URI}}}}})
        if request.method == "GET" and path == "/redfish/v1/Managers/1/" and request.headers.get("X-Auth-Token") == "tok":
            return httpx.Response(200, json={**FAKE_ILO[path], "FirmwareVersion": self.version})
        return _fake(request)


@pytest.fixture
def flashing_ilo(mcp_client, monkeypatch):
    ilo = FlashingIlo()
    monkeypatch.setattr(srv, "_transport", httpx.MockTransport(ilo))
    monkeypatch.setattr(ilo_firmware, "POLL_SECONDS", 0)
    monkeypatch.setattr(chat_firmware, "_run", None)
    ilo_firmware._catalog_cache.clear()
    return ilo


async def test_firmware_list_updates_plans_steps_from_minimum_active_version(mcp_client, flashing_ilo):
    async with mcp_client as c:
        data = (await c.call_tool("ilo_firmware_list_updates", {})).structured_content
    gen12, badpass = data["servers"]
    assert gen12["latest"] == "1.25.02" and not gen12["up_to_date"]
    newest, older = gen12["available"]
    assert newest["steps"] == ["1.24.00", "1.25.02"] and newest["requires_at_least"] == "1.24.00"
    assert newest["url"] == _SDR + "2026.09.00.00/ilo7_1.25.02.fwpkg"
    assert older["steps"] == ["1.24.00"] and older["release_date"] == "2026-07-25" and newest["about_minutes"] == 2 * older["about_minutes"]
    assert "login failed" in badpass["error"]
    assert [p["phase"] for p in data["one_step"]["phases"]] == list(ilo_firmware.PHASES)
    assert not flashing_ilo.posts  # read-only


async def test_firmware_update_flashes_only_after_the_user_confirms_on_the_page(mcp_client, flashing_ilo, tmp_path):
    async with mcp_client as c, httpx.AsyncClient() as browser:
        step = (await c.call_tool("ilo_firmware_update_start", {"servers": ["gen12"], "version": "1.25.02"})).structured_content
        while step["status"] == "checking":
            step = (await c.call_tool("ilo_firmware_update_status", {})).structured_content
        assert step["status"] == "select_servers" and "Do not choose for them" in step["next_step"]
        page = (await browser.get(step["url"])).text
        assert "name='sel_0' value='1' checked" in page and "value='1.25.02' selected" in page and "via 1.24.00" in page
        assert "login failed" in page  # badpass is listed with its error, without a check box

        await browser.post(step["url"], data={"step": "select", "action": "review", "sel_0": "1", "ver_0": "1.25.02", "mode": "parallel"})
        step = (await c.call_tool("ilo_firmware_update_status", {})).structured_content
        assert step["status"] == "confirm_update" and step["plan"]["mode"] == "parallel"
        assert step["plan"]["servers"][0]["steps"] == ["1.24.00", "1.25.02"] and step["plan"]["about_minutes_total"] >= 20
        assert "Start update" in (await browser.get(step["url"])).text
        assert not flashing_ilo.posts  # nothing is flashed before the user clicks Start

        await browser.post(step["url"], data={"step": "confirm", "action": "start"})
        for _ in range(20):
            step = (await c.call_tool("ilo_firmware_update_status", {})).structured_content
            if step["status"] != "updating":
                break
    assert step["status"] == "done", step
    server = step["servers"][0]
    assert (server["server"], server["status"], server["to"], server["path"]) == ("gen12", "done", "1.25.02", ["1.24.00", "1.25.02"])
    assert [p["ImageURI"].rsplit("/", 1)[1] for p in flashing_ilo.posts] == ["ilo7_1.24.00.fwpkg", "ilo7_1.25.02.fwpkg"]
    assert all(p["UpdateTarget"] for p in flashing_ilo.posts) and flashing_ilo.version.startswith("1.25.02")
    phases = " ".join(server["recent"])
    assert all(p in phases for p in ("downloading", "flashing", "activating", "resetting", "verifying"))
    history = json.loads((tmp_path / ".config/proliant-mcp/firmware-history.json").read_text())
    assert [h["version"] for h in history] == ["1.24.00", "1.25.02"] and "flashing" in history[0]["phases"]
    saved = json.loads((tmp_path / ".config/proliant-mcp/firmware-update.json").read_text())
    assert saved["status"] == "done"
    monkeypatch_run = chat_firmware._run
    chat_firmware._run = None  # another proliant-mcp process (e.g. a sub-agent's) reads the saved progress
    try:
        other = await chat_firmware.status()
    finally:
        chat_firmware._run = monkeypatch_run
    assert other["from_file"] and other["status"] == "done"


async def test_firmware_update_sequential_stops_after_a_failure_parallel_does_not(mcp_client, flashing_ilo):
    servers = srv._servers()
    pkg = ilo_firmware.Package("ilo7", "1.24.00", "ilo7_1.24.00.fwpkg", _SDR + "2026.09.00.00/ilo7_1.24.00.fwpkg", "2026.09.00.00")
    results = {}
    for mode in ("sequential", "parallel"):
        flashing_ilo.version = "1.21.00 Apr 07 2026"
        run = chat_firmware.Run()
        run.mode = mode
        run.progress = [ilo_firmware.Progress(name, "ilo7", "1.21.00", "1.24.00", [pkg]) for name in ("badpass", "gen12")]
        await chat_firmware._execute(run, servers, srv._transport)
        results[mode] = [(p.status, p.error or "") for p in run.progress]
    assert results["sequential"][0][0] == "failed" and "login failed" in results["sequential"][0][1]
    assert results["sequential"][1][0] == "skipped"
    assert [s for s, _ in results["parallel"]] == ["failed", "done"]


async def test_firmware_flash_survives_a_network_blip_and_a_stale_error_state(mcp_client, flashing_ilo, monkeypatch):
    """A leftover Error from an earlier attempt, and two failed polls mid-flash, are not the result or the iLO restart."""
    monkeypatch.setattr(ilo_firmware, "RETURN_TIMEOUT", 0)
    flashing_ilo.idle = "Error"
    flashing_ilo.after_post = [("Error", None), ("Uploading", None), *["offline"] * 3, ("Updating", 50), ("Updating", 50),
                               ("Updating", 100), ("Complete", None), *["offline"] * 4]
    pkg = ilo_firmware.Package("ilo7", "1.24.00", "ilo7_1.24.00.fwpkg", _SDR + "2026.09.00.00/ilo7_1.24.00.fwpkg", "2026.09.00.00")
    progress = ilo_firmware.Progress("gen12", "ilo7", "1.21.00", "1.24.00", [pkg], step=1)
    await ilo_firmware.flash(srv._servers()["gen12"], pkg, progress, lambda: None, srv._transport)
    assert progress.installed.startswith("1.24.00") and progress.phase == "verifying"
