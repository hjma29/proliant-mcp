"""Offline tests: a fake iLO served through httpx.MockTransport, called via the MCP client."""

from __future__ import annotations

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from proliant_mcp import server as srv

FAKE_ILO: dict[str, dict] = {
    "/redfish/v1/": {
        "Systems": {"@odata.id": "/redfish/v1/Systems/"},
        "Managers": {"@odata.id": "/redfish/v1/Managers/"},
        "Chassis": {"@odata.id": "/redfish/v1/Chassis/"},
        "UpdateService": {"@odata.id": "/redfish/v1/UpdateService/"},
    },
    "/redfish/v1/Systems/": {"Members": [{"@odata.id": "/redfish/v1/Systems/1/"}]},
    "/redfish/v1/Managers/": {"Members": [{"@odata.id": "/redfish/v1/Managers/1/"}]},
    "/redfish/v1/Chassis/": {"Members": [{"@odata.id": "/redfish/v1/Chassis/1/"}]},
    "/redfish/v1/Systems/1/": {
        "Model": "HPE ProLiant Compute DL345 Gen12",
        "SerialNumber": "TEST123",
        "PowerState": "On",
        "Status": {"Health": "OK", "HealthRollup": "OK"},
        "BiosVersion": "A66 v1.40",
        "LocationIndicatorActive": False,
        "ProcessorSummary": {"Count": 1, "Model": "AMD EPYC 9375F 32-Core Processor     "},
        "MemorySummary": {"TotalSystemMemoryGiB": 768},
        "Oem": {"Hpe": {"PostState": "FinishedPost"}},
        "Processors": {"@odata.id": "/redfish/v1/Systems/1/Processors/"},
    },
    "/redfish/v1/Systems/1/Processors/": {
        "Members": [{"@odata.id": "/redfish/v1/Systems/1/Processors/1/"}, {"@odata.id": "/redfish/v1/Systems/1/Processors/2/"}]
    },
    "/redfish/v1/Systems/1/Processors/1/": {"ProcessorType": "CPU", "Socket": "Proc 1", "Model": "AMD EPYC 9375F", "TotalCores": 32},
    "/redfish/v1/Systems/1/Processors/2/": {"ProcessorType": "GPU", "Name": "NVIDIA RTXPRO6000BlackwellDC", "Manufacturer": "NVIDIA"},
    "/redfish/v1/Managers/1/": {"Model": "iLO 7", "FirmwareVersion": "1.21.00 Apr 07 2026"},
    "/redfish/v1/Chassis/1/": {"NetworkAdapters": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/"}},
    "/redfish/v1/Chassis/1/NetworkAdapters/": {"Members": [{"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/1/"}]},
    "/redfish/v1/Chassis/1/NetworkAdapters/1/": {
        "Model": "BCM57414",
        "Controllers": [{"FirmwarePackageVersion": "235.1.164.14"}],
        "Ports": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/1/Ports/"},
    },
    "/redfish/v1/Chassis/1/NetworkAdapters/1/Ports/": {"Members": [{"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/1/Ports/1/"}]},
    "/redfish/v1/Chassis/1/NetworkAdapters/1/Ports/1/": {
        "Id": "1",
        "LinkStatus": "LinkUp",
        "Ethernet": {"AssociatedMACAddresses": ["BC:97:E1:B1:60:24"], "LLDPReceive": {"PortId": "73:77:70:34"}},
    },
}


def fake_ilo(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.method == "POST" and path == "/redfish/v1/SessionService/Sessions":
        body = request.read()
        if b'"Password":"good"' not in body.replace(b" ", b""):
            return httpx.Response(401, json={"error": {"@Message.ExtendedInfo": [{"MessageId": "iLO.2.41.UnauthorizedLoginAttempt"}]}})
        return httpx.Response(201, headers={"X-Auth-Token": "tok", "Location": "/redfish/v1/SessionService/Sessions/1/"}, json={})
    if request.method == "DELETE":
        return httpx.Response(200, json={})
    if request.headers.get("X-Auth-Token") != "tok":
        return httpx.Response(401)
    if path in FAKE_ILO:
        return httpx.Response(200, json=FAKE_ILO[path])
    return httpx.Response(404, json={})


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
