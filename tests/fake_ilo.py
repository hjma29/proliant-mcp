"""A tiny fake iLO Redfish tree shared by the tests."""

from __future__ import annotations

import httpx

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
