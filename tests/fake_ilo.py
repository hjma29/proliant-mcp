"""A tiny fake iLO Redfish tree shared by the tests."""

from __future__ import annotations

import datetime as dt

import httpx

_NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)


def _ago(**delta: float) -> str:
    return (_NOW - dt.timedelta(**delta)).isoformat().replace("+00:00", "Z")


def _iel(created: str, message: str, count: int = 1) -> dict:
    return {"Created": created, "Message": message, "Severity": "OK", "Oem": {"Hpe": {"Count": count, "Updated": created}}}


# Shapes seen on real iLO 6/7 Event Logs, including entries stamped while the clock was wrong.
FAKE_IEL = [
    _iel(_ago(hours=1), "Browser login: Administrator - 192.0.2.50(admin-pc.example.com)."),
    _iel(_ago(minutes=50), "Browser logout: Administrator - 192.0.2.50(admin-pc.example.com)."),
    _iel(_ago(hours=3), "Host REST login: System Administrator"),
    _iel(_ago(hours=40), "Browser login: admin - 192.0.2.60(DNS name not found).", count=2),
    _iel(_ago(days=1), "Browser login failure from: 192.0.2.66(DNS name not found)."),
    _iel(_ago(days=1), "iLO detected more than 3 unauthorized login attempts."),
    _iel(_ago(days=30), "Browser login: Administrator - 192.0.2.80(old.example.com)."),
    _iel("0000-00-00T00:00:00Z", "iLO RTC reset has happened."),
    _iel("2039-12-09T02:25:35Z", "Browser login: Administrator - 192.0.2.70(wrong-clock.example.com)."),
]

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
    "/redfish/v1/Managers/1/": {
        "Model": "iLO 7",
        "FirmwareVersion": "1.21.00 Apr 07 2026",
        "LogServices": {"@odata.id": "/redfish/v1/Managers/1/LogServices/"},
    },
    "/redfish/v1/Managers/1/LogServices/": {"Members": [{"@odata.id": "/redfish/v1/Managers/1/LogServices/IEL/"}]},
    "/redfish/v1/Managers/1/LogServices/IEL/": {"Entries": {"@odata.id": "/redfish/v1/Managers/1/LogServices/IEL/Entries/"}},
    "/redfish/v1/Managers/1/LogServices/IEL/Entries/": {"Members": FAKE_IEL},
    "/redfish/v1/SessionService/Sessions/1/": {"Oem": {"Hpe": {"MySession": True, "UserIP": "192.0.2.50"}}},
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
