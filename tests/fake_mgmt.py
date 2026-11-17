"""Fake OneView appliance and Compute Ops Management API for the offline tests."""

from __future__ import annotations

import json

import httpx

OV_HOST = "192.0.2.100"
COM_API = "us-west.api.greenlake.hpe.com"
COM_SSO = "sso.common.cloud.hpe.com"

_SH_URI = "/rest/server-hardware/HW1"

FAKE_OV: dict[str, dict] = {
    "/rest/appliance/nodeinfo/version": {"modelNumber": "Synergy Composer2", "softwareVersion": "11.10.00-1", "family": "Synergy Composer"},
    "/rest/appliance/health-status": {"members": [{"resourceType": "DISK", "severity": "INFO", "available": "17 GB", "capacity": "894 GB ", "statusMessage": "SUFFICIENT_DISK_SPACE"}]},
    "/rest/server-hardware": {"members": [{
        "uri": _SH_URI, "name": "Enclosure-01, bay 2", "serverName": "esx2.example.com", "model": "Synergy 480 Gen10",
        "serialNumber": "SN0001", "status": "Warning", "state": "ProfileApplied", "powerState": "On",
        "serverProfileUri": "/rest/server-profiles/P1", "locationUri": "/rest/enclosures/E1", "position": 2,
        "processorCount": 2, "processorType": "Intel(R) Xeon(R) Silver 4214R CPU @ 2.40GHz", "processorCoreCount": 12, "processorSpeedMhz": 2400,
        "memoryMb": 262144, "mpModel": "iLO5", "mpFirmwareVersion": "3.17",
        "mpHostInfo": {"mpHostName": "ilo-sn0001", "mpIpAddresses": [{"address": "fe80::1", "type": "LinkLocal"}, {"address": "192.0.2.50", "type": "DHCP"}]},
        "serverFirmwareInventoryUri": f"{_SH_URI}/firmware",
    }]},
    f"{_SH_URI}/firmware": {"components": [{"componentLocation": " ", "componentName": "Intel-i40en", "componentVersion": "1.13"}, {"componentLocation": "System Board", "componentName": "System ROM", "componentVersion": "I42 v3.60"}]},
    "/rest/server-profiles": {"members": [{"uri": "/rest/server-profiles/P1", "name": "esx2-profile", "status": "OK", "serverHardwareUri": _SH_URI, "serverProfileTemplateUri": "/rest/server-profile-templates/T1", "templateCompliance": "Compliant"}]},
    "/rest/server-profile-templates": {"members": [{"uri": "/rest/server-profile-templates/T1", "name": "esx-template"}]},
    "/rest/enclosures": {"members": [{"uri": "/rest/enclosures/E1", "name": "Enclosure-01", "status": "OK"}]},
    "/rest/interconnects": {"members": []},
    "/rest/alerts": {"members": [{"uri": "/rest/alerts/7761", "severity": "Warning", "alertState": "Active", "description": "POST errors occurred.", "resourceUri": _SH_URI, "associatedResource": {"resourceName": "Enclosure-01, bay 2"}}]},
}


def _ov(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/rest/version":
        return httpx.Response(200, json={"currentVersion": 8400})
    if path == "/rest/login-sessions":
        if request.method == "DELETE":
            return httpx.Response(204)
        if json.loads(request.read()).get("password") != "good":
            return httpx.Response(401, json={"message": "Invalid user name or password."})
        return httpx.Response(200, json={"sessionID": "ovtok"})
    if request.headers.get("auth") != "ovtok" or request.headers.get("X-API-Version") != "8400":
        return httpx.Response(401, json={"message": "no session"})
    body = FAKE_OV.get(path)
    if body is None:
        return httpx.Response(404, json={"message": "not found"})
    if "members" in body:
        filters = request.url.params.get_list("filter")
        members = [m for m in body["members"] if not any(f"'{m.get('resourceUri')}'" not in f for f in filters if "resourceUri" in f)]
        return httpx.Response(200, json={"members": members, "total": len(members)})
    return httpx.Response(200, json=body)


_COM_SERVERS = [
    {"id": "P1+SN0001", "name": "esx2.example.com", "hardware": {"serialNumber": "SN0001", "model": "Synergy 480 Gen10", "powerState": "ON", "health": {"summary": "WARNING", "bios": "WARNING"}, "bmc": {"ip": "192.0.2.50", "hostname": "ilo-sn0001"}},
     "state": {"connected": True, "subscriptionExpiresAt": "2027-01-01"}, "serverGeneration": "GEN_10", "connectionType": "ONEVIEW",
     "firmwareInventory": [{"name": "iLO 5", "version": "3.17", "deviceContext": "System Board"}]},
    {"id": "P2+SN0002", "name": "dl345.example.com", "hardware": {"serialNumber": "SN0002", "model": "ProLiant Compute DL345 Gen12", "powerState": "ON", "health": {"summary": "OK"}},
     "state": {"connected": False}, "serverGeneration": "GEN_12", "connectionType": "DIRECT"},
]
_COM_ALERTS = [
    {"id": "a1", "serverId": "P1+SN0001", "severity": "WARNING", "message": "Bios subsystem health is Warning", "cleared": False, "createdAt": "2026-11-02"},
    {"id": "a2", "serverId": "P2+SN0002", "severity": "CRITICAL", "message": "Fan failed", "cleared": True, "createdAt": "2026-11-01"},
]
FAKE_COM: dict[str, list[dict]] = {
    "/compute-ops-mgmt/v1/servers": _COM_SERVERS,
    "/compute-ops-mgmt/v1/alerts": _COM_ALERTS,
    "/compute-ops-mgmt/v1/servers/P1+SN0001/alerts": _COM_ALERTS[:1],
    "/compute-ops-mgmt/v1beta1/appliances": [{"id": "ov1", "state": "activated", "activationKey": "eyJ-secret"}],
}


def _com(request: httpx.Request) -> httpx.Response:
    if request.url.host == COM_SSO:
        if request.headers.get("Authorization", "").startswith("Basic ") and b"client_credentials" in request.read():
            return httpx.Response(200, json={"access_token": "comtok", "expires_in": 7200})
        return httpx.Response(401, json={})
    if request.headers.get("Authorization") != "Bearer comtok":
        return httpx.Response(401, json={"message": "unauthorized"})
    items = FAKE_COM.get(request.url.path)
    if items is None:
        return httpx.Response(404, json={"message": "not found"})
    offset = int(request.url.params.get("offset", 0))
    limit = int(request.url.params.get("limit", 100))
    return httpx.Response(200, json={"items": items[offset : offset + limit], "total": len(items), "count": len(items[offset : offset + limit])})


def fake_mgmt(request: httpx.Request) -> httpx.Response | None:
    """Route OneView and COM requests; return None for anything else (the iLOs)."""
    if request.url.host == OV_HOST:
        return _ov(request)
    if request.url.host in (COM_API, COM_SSO):
        return _com(request)
    return None
