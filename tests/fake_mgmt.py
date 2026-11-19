"""Fake OneView appliance and Compute Ops Management API for the offline tests."""

from __future__ import annotations

import json

import httpx

OV_HOST = "192.0.2.100"
COM_API = "us-west.api.greenlake.hpe.com"
COM_SSO = "sso.common.cloud.hpe.com"
GL_GLOBAL = "global.api.greenlake.hpe.com"

_SH_URI = "/rest/server-hardware/HW1"


def _port(ic: str, name: str, status: str, role: str, uplink_set: str, switch: str | None, switch_port: str | None) -> dict:
    return {"uri": f"/rest/interconnects/{ic}/ports/{ic}:{name}", "portName": name, "portType": "Uplink", "portStatus": status,
            "portStatusReason": role, "portHealthStatus": "Normal", "operationalSpeed": "Speed10G" if status == "Linked" else "Speed0M",
            "associatedUplinkSetUri": uplink_set,
            "neighbor": {"remoteSystemName": switch, "remotePortId": switch_port} if switch else None}


def _uplink_set(uid: str, name: str, status: str, networks: list[str], ports: list[tuple[str, str]]) -> dict:
    return {"uri": f"/rest/uplink-sets/{uid}", "name": name, "status": status, "state": "Active", "networkType": "Ethernet",
            "ethernetNetworkType": "Tagged", "connectionMode": "Auto", "reachability": "Reachable", "networkUris": networks,
            "portConfigInfos": [{"portUri": f"/rest/interconnects/{ic}/ports/{ic}:{p}",
                                 "location": {"locationEntries": [{"type": "Port", "value": p}]}} for ic, p in ports]}

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
    "/rest/interconnects": {"members": [
        {"uri": "/rest/interconnects/IC3", "name": "Enclosure-01, interconnect 3", "model": "Virtual Connect SE 100Gb F32 Module for Synergy",
         "status": "OK", "hostName": "VC100-bay3", "serialNumber": "7C9M0003", "ipAddressList": [{"ipAddress": "192.0.2.103", "ipAddressType": "Ipv4Static"}]},
        {"uri": "/rest/interconnects/IC6", "name": "Enclosure-01, interconnect 6", "model": "Virtual Connect SE 100Gb F32 Module for Synergy",
         "status": "OK", "hostName": "VC100-bay6", "serialNumber": "7C9M0006", "ipAddressList": [{"ipAddress": "192.0.2.106", "ipAddressType": "Ipv4Static"}]},
    ]},
    "/rest/interconnects/IC3/ports": {"members": [
        _port("IC3", "Q1:1", "Linked", "Active", "/rest/uplink-sets/US1", "tor-01", "Eth1/11"),
        _port("IC3", "Q5:2", "Linked", "None", "/rest/uplink-sets/US2", "switch", "Eth1/6"),
        _port("IC3", "X1", "Linked", "Active", "/rest/uplink-sets/US3", "switch", "Eth1/10"),
    ]},
    "/rest/interconnects/IC6/ports": {"members": [
        _port("IC6", "Q1:1", "Linked", "StandBy", "/rest/uplink-sets/US1", "tor-02", "Eth1/11"),
        _port("IC6", "Q5:2", "Unlinked", "None", "/rest/uplink-sets/US2", None, None),
    ]},
    "/rest/uplink-sets": {"members": [
        _uplink_set("US1", "prod-uplinks", "OK", ["/rest/ethernet-networks/N1"], [("IC3", "Q1:1"), ("IC6", "Q1:1")]),
        _uplink_set("US2", "ACI-MAP", "Critical", [], [("IC3", "Q5:2"), ("IC6", "Q5:2")]),
        _uplink_set("US3", "span", "OK", ["/rest/ethernet-networks/N2"], [("IC3", "X1")]),
    ]},
    "/rest/ethernet-networks": {"members": [{"uri": "/rest/ethernet-networks/N1", "name": "prod", "vlanId": 160},
                                            {"uri": "/rest/ethernet-networks/N2", "name": "mirror", "vlanId": 0}]},
    "/rest/fc-networks": {"members": []},
    "/rest/network-sets": {"members": []},
    "/rest/alerts": {"members": [
        {"uri": "/rest/alerts/7761", "severity": "Warning", "alertState": "Active", "description": "POST errors occurred.", "resourceUri": _SH_URI, "associatedResource": {"resourceName": "Enclosure-01, bay 2"}},
        {"uri": "/rest/alerts/8909", "severity": "Critical", "alertState": "Active", "description": "Uplink set ACI-MAP does not contain any networks.",
         "resourceUri": "/rest/logical-interconnects/LI1", "associatedResource": {"resourceName": "LE01-LIG"}},
    ]},
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
    {"id": "P3+SN0003", "name": "gpu1.example.com", "hardware": {"serialNumber": "SN0003", "model": "ProLiant DL380 Gen11", "powerState": "ON", "health": {"summary": "OK"}, "bmc": {"version": "iLO 6 v1.74"}},
     "state": {"connected": True}, "serverGeneration": "GEN_11", "connectionType": "DIRECT"},
    {"id": "P4+SN0004", "name": "old-t4.example.com", "hardware": {"serialNumber": "SN0004", "model": "ProLiant DL360 Gen10", "powerState": "ON", "health": {"summary": "OK"}, "bmc": {"version": "iLO 5 v3.17"}},
     "state": {"connected": True}, "serverGeneration": "GEN_10", "connectionType": "DIRECT"},
    {"id": "P2+SN0002", "name": "dl345.example.com", "hardware": {"serialNumber": "SN0002", "model": "ProLiant Compute DL345 Gen12", "powerState": "ON", "health": {"summary": "OK"}},
     "state": {"connected": False}, "serverGeneration": "GEN_12", "connectionType": "DIRECT"},
]
def _gpu(i: int, name: str, vendor: str, part: str) -> dict:
    return {"Id": str(i), "Name": name, "Model": part, "PartNumber": part, "Manufacturer": vendor, "SerialNumber": f"GSN{i}",
            "ProcessorType": "GPU", "Status": {"State": "Enabled", "Health": "OK"}}


_CPU = {"Id": "1", "Name": "Processors", "Model": "AMD EPYC 9374F 32-Core Processor      ", "TotalCores": 32, "TotalThreads": 64, "ProcessorType": "CPU"}
def _drive(i: int, gb: int, media: str = "SSD", protocol: str = "SATA", state: str = "Enabled") -> dict:
    return {"Id": str(i), "Model": f"MK{gb}", "SerialNumber": f"DSN{i}", "CapacityBytes": gb * 1000**3, "MediaType": media,
            "Protocol": protocol, "Status": {"State": state, "Health": "OK"}}


_RAID = {"Id": "DE07C000", "Name": "HPE Smart Array P408i-a SR Gen10",
         "StorageControllers": [{"Model": "HPE Smart Array P408i-a SR Gen10", "FirmwareVersion": "7.00", "Status": {"Health": "OK"}}],
         "Drives": [_drive(1, 960), _drive(2, 960), _drive(3, 1920), _drive(9, 0, state="Absent")], "Status": {"Health": "OK"}}
_NVME = [{"Id": f"DA00010{i}", "Name": "NVMe Storage System", "StorageControllers": [{"Model": "MO003200KXPTT"}],
          "Drives": [_drive(10 + i, 3200, "SSD", "NVMe")]} for i in range(2)]
_EMBEDDED_VIDEO = {"Id": "273", "Name": "Embedded Video Controller", "PCIeFunctions": [{"DeviceClass": "DisplayController", "ClassCode": "0X000003"}]}


def _port(i: int, tech: str, up: bool, speed: float | None) -> dict:
    return {"Id": str(i), "PortId": str(i), "LinkNetworkTechnology": tech, "LinkStatus": "LinkUp" if up else "LinkDown",
            "CurrentSpeedGbps": speed, "Ethernet": {"AssociatedMACAddresses": [f"02:00:00:00:00:0{i}"]}}


# Field shapes seen in real COM inventory: the marketing name sits in Model, SKU or Name depending on the card.
_BCM = {"Id": "DE07A000", "Name": "Network Adapter", "Model": "BCM57414", "SKU": "10/25Gb 2-port SFP28 BCM57414 OCP3 Adapter",
        "Manufacturer": "Broadcom", "PartNumber": "P10113-001", "Controllers": [{"FirmwarePackageVersion": "235.1.164.2"}],
        "Ports": [_port(1, "Ethernet", True, 25.0), _port(2, "Ethernet", False, 0.0)]}
_VPI = {"Id": "DE081000", "Name": "NetworkAdapter", "Model": "HPE InfiniBand HDR/Ethernet 200Gb 2-port QSFP56 MCX653436A-HDAI Adapter",
        "SKU": "P36054-001", "Manufacturer": "MLNX", "Ports": [_port(1, "Ethernet", True, 100.0), _port(2, "InfiniBand", False, 0.0)]}
_FC = {"Id": "DE085000", "Name": "HPE SN1620E 32Gb 2p FC HBA", "Model": "HPE SN1620E 32Gb 2p FC HBA", "SKU": "SN1620E2P",
       "Ports": [_port(1, "FibreChannel", True, 16), _port(2, "FibreChannel", True, 16)]}
FAKE_COM_INVENTORY: dict[str, dict] = {
    "/compute-ops-mgmt/v1/servers/P1+SN0001/inventory": {
        "lastFullInventoryCollectionAt": "2026-11-10T00:00:00Z",
        "processor": {"data": [_CPU, {**_CPU, "Id": "2"}]},
        "pciDevice": {"data": [_EMBEDDED_VIDEO]},
        "storage": {"data": [_RAID, *_NVME]},
        "networkAdapter": {"data": [_FC]},
    },
    "/compute-ops-mgmt/v1/servers/P3+SN0003/inventory": {
        "lastFullInventoryCollectionAt": "2026-11-11T00:00:00Z",
        "processor": {"data": [_CPU, _gpu(3, "NVIDIA L40S", "NVIDIA", "900-2G133-0380-030"), _gpu(4, "NVIDIA L40S", "NVIDIA", "900-2G133-0380-030"),
                                _gpu(5, "Intel Video Controller", "Intel", "")]},
        "pciDevice": {"data": [_EMBEDDED_VIDEO]},
        "networkAdapter": {"data": [_BCM, _VPI]},
    },
    # an older iLO that lists the GPU only as a PCIe display controller
    "/compute-ops-mgmt/v1/servers/P4+SN0004/inventory": {
        "processor": {"data": [_CPU]},
        "pciDevice": {"data": [_EMBEDDED_VIDEO, {"Id": "5", "Name": "NVIDIA T4", "Manufacturer": "NVIDIA", "SerialNumber": "T4SN",
                                                 "PCIeFunctions": [{"DeviceClass": "DisplayController", "ClassCode": "0X000003"}]}]},
    },
}
_COM_ALERTS = [
    {"id": "a1", "serverId": "P1+SN0001", "severity": "WARNING", "message": "Bios subsystem health is Warning", "cleared": False, "createdAt": "2026-11-02"},
    {"id": "a2", "serverId": "P2+SN0002", "severity": "CRITICAL", "message": "Fan failed", "cleared": True, "createdAt": "2026-11-01"},
]
FAKE_COM: dict[str, list[dict]] = {
    "/service-catalog/v1/service-managers": [
        {"id": "sm-com", "name": "Compute Ops Management", "description": "Securely manage\n your compute."},
        {"id": "sm-ds", "name": "Data Services", "description": "Data."},
        {"id": "sm-oc", "name": "OpsRamp", "description": None},
    ],
    "/service-catalog/v1/service-manager-provisions": [
        {"id": "p1", "serviceManager": {"id": "sm-com"}, "region": "us-west", "provisionStatus": "PROVISIONED", "reason": None},
        {"id": "p2", "serviceManager": {"id": "sm-com"}, "region": "eu-central", "provisionStatus": "PROVISIONED", "reason": None},
        {"id": "p3", "serviceManager": {"id": "sm-ds"}, "region": "us-west", "provisionStatus": "FAILED", "reason": "quota"},
    ],
    "/compute-ops-mgmt/v1/servers": _COM_SERVERS,
    "/compute-ops-mgmt/v1/alerts": _COM_ALERTS,
    "/compute-ops-mgmt/v1/servers/P1+SN0001/alerts": _COM_ALERTS[:1],
    "/compute-ops-mgmt/v1/servers/P3+SN0003/alerts": [],
    "/compute-ops-mgmt/v1beta1/appliances": [{"id": "ov1", "state": "activated", "activationKey": "eyJ-secret"}],
}


def _com(request: httpx.Request) -> httpx.Response:
    if request.url.host == COM_SSO:
        if request.headers.get("Authorization", "").startswith("Basic ") and b"client_credentials" in request.read():
            return httpx.Response(200, json={"access_token": "comtok", "expires_in": 7200})
        return httpx.Response(401, json={})
    if request.headers.get("Authorization") != "Bearer comtok":
        return httpx.Response(401, json={"message": "unauthorized"})
    inventory = FAKE_COM_INVENTORY.get(request.url.path)
    if inventory is not None:
        return httpx.Response(200, json=inventory)
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
    if request.url.host in (COM_API, COM_SSO, GL_GLOBAL):
        return _com(request)
    return None
