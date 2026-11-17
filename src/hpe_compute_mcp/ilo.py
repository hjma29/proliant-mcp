"""Read-only iLO inventory helpers. Each returns plain JSON-serialisable data."""

from __future__ import annotations

from typing import Any

from hpe_compute_mcp.redfish import RedfishClient, RedfishError


def _hpe(resource: dict[str, Any]) -> dict[str, Any]:
    oem = resource.get("Oem") or {}
    return oem.get("Hpe") or oem.get("Hp") or {}


def _link(resource: dict[str, Any], key: str) -> str | None:
    return (resource.get(key) or {}).get("@odata.id")


def _health(resource: dict[str, Any]) -> str | None:
    return (resource.get("Status") or {}).get("Health")


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return None if value in {"", "Unknown", "N/A"} else value
    return value


def _lldp_text(value: str | None) -> str | None:
    """iLO reports some LLDP IDs as hex bytes ("73:77:70:33"); decode when printable."""
    if value and ":" in value:
        try:
            decoded = bytes.fromhex(value.replace(":", "")).decode("ascii")
            if decoded.isprintable():
                return decoded
        except ValueError:
            pass
    return value or None


def _ilo_version(manager: dict[str, Any]) -> str:
    # iLO 6 puts the model inside FirmwareVersion ("iLO 6 v1.74"); iLO 7 doesn't ("1.21.00").
    model = manager.get("Model") or ""
    fw = manager.get("FirmwareVersion") or ""
    return fw if not model or fw.startswith(model) else f"{model} {fw}".strip()


async def _ilo_interface(c: RedfishClient, manager: dict[str, Any]) -> dict[str, Any] | None:
    fallback = None
    for iface in await c.members(_link(manager, "EthernetInterfaces")):
        if _hpe(iface).get("InterfaceType") == "Dedicated":
            return iface
        if fallback is None and (iface.get("InterfaceEnabled") is not False):
            fallback = iface
    return fallback


def _ipv4(addresses: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    for a in addresses or []:
        addr = (a.get("Address") or "").strip()
        if addr and addr != "0.0.0.0":
            return {
                "address": addr,
                "subnet_mask": a.get("SubnetMask"),
                "gateway": a.get("Gateway"),
                "origin": a.get("AddressOrigin"),
            }
    return None


async def ilo_info(c: RedfishClient) -> dict[str, Any]:
    manager = await c.manager()
    hpe = _hpe(manager)
    current_fw = (hpe.get("Firmware") or {}).get("Current") or {}
    lic = hpe.get("License") or {}
    cloud = hpe.get("CloudConnect") or {}
    iface = await _ilo_interface(c, manager)

    network = None
    if iface:
        network = {
            "hostname": iface.get("HostName"),
            "fqdn": iface.get("FQDN"),
            "mac": (iface.get("MACAddress") or "").lower() or None,
            "dhcp_enabled": (iface.get("DHCPv4") or {}).get("DHCPEnabled"),
            "ipv4": _ipv4(iface.get("IPv4Addresses")),
            "link_status": iface.get("LinkStatus"),
            "speed_mbps": iface.get("SpeedMbps"),
            "dns_servers": [s for s in (iface.get("NameServers") or []) if s and s != "0.0.0.0"],
        }

    return {
        "ilo_model": manager.get("Model"),
        "ilo_version": _ilo_version(manager),
        "firmware_version": manager.get("FirmwareVersion"),
        "firmware_date": current_fw.get("Date"),
        "firmware_version_string": current_fw.get("VersionString"),
        "health": _health(manager),
        "uuid": manager.get("UUID"),
        "date_time": manager.get("DateTime"),
        "license": {"name": lic.get("LicenseString"), "type": lic.get("LicenseType")} if lic else None,
        "compute_ops_management": {
            "status": cloud.get("CloudConnectStatus"),
            "connection_type": cloud.get("ConnectionType"),
        } if cloud else None,
        "network": network,
    }


async def server_summary(c: RedfishClient) -> dict[str, Any]:
    system = await c.system()
    manager = await c.manager()
    hpe = _hpe(system)
    proc = system.get("ProcessorSummary") or {}
    mem = system.get("MemorySummary") or {}
    uid = system.get("LocationIndicatorActive")
    return {
        "model": system.get("Model"),
        "product_id": system.get("SKU"),
        "serial_number": system.get("SerialNumber"),
        "uuid": system.get("UUID"),
        "os_hostname": system.get("HostName") or None,
        "power_state": system.get("PowerState"),
        "post_state": (hpe.get("PostState")),
        "health": _health(system),
        "health_rollup": (system.get("Status") or {}).get("HealthRollup"),
        "uid_light": ("Lit" if uid else "Off") if isinstance(uid, bool) else system.get("IndicatorLED"),
        "bios_version": system.get("BiosVersion"),
        "ilo_version": _ilo_version(manager),
        "processors": {"count": proc.get("Count"), "model": _clean(proc.get("Model"))},
        "memory_gib": mem.get("TotalSystemMemoryGiB"),
    }


async def health(c: RedfishClient) -> dict[str, Any]:
    system = await c.system()
    agg = _hpe(system).get("AggregateHealthStatus") or {}
    subsystems: dict[str, Any] = {}
    for key, value in agg.items():
        if isinstance(value, dict) and "Status" in value:
            subsystems[key] = (value.get("Status") or {}).get("Health")
        elif isinstance(value, dict) and "PowerSuppliesMismatch" in value:
            subsystems[key] = (value.get("Status") or {}).get("Health")
    return {
        "power_state": system.get("PowerState"),
        "overall": _health(system),
        "rollup": (system.get("Status") or {}).get("HealthRollup"),
        "subsystems": subsystems,
        "fan_redundancy": agg.get("FanRedundancy"),
        "power_supply_redundancy": agg.get("PowerSupplyRedundancy"),
    }


async def firmware_inventory(c: RedfishClient) -> list[dict[str, Any]]:
    update_service = await c.get(_link(await c.root(), "UpdateService") or "/redfish/v1/UpdateService")
    items = await c.members(_link(update_service, "FirmwareInventory"))
    return [
        {
            "name": i.get("Name"),
            "version": i.get("Version"),
            "updateable": i.get("Updateable"),
            "location": _hpe(i).get("DeviceContext") or None,
        }
        for i in items
    ]


async def processors(c: RedfishClient) -> dict[str, Any]:
    system = await c.system()
    cpus, gpus = [], []
    for p in await c.members(_link(system, "Processors")):
        if (p.get("ProcessorType") or "").upper() == "GPU":
            gpus.append({
                "name": (p.get("Name") or "").strip(),
                "model": (p.get("Model") or "").strip() or None,
                "manufacturer": p.get("Manufacturer"),
                "health": _health(p),
            })
            continue
        if (p.get("Status") or {}).get("State") == "Absent":
            continue
        cpus.append({
            "socket": p.get("Socket"),
            "model": (p.get("Model") or "").strip(),
            "cores": p.get("TotalCores"),
            "threads": p.get("TotalThreads"),
            "max_speed_mhz": p.get("MaxSpeedMHz"),
            "health": _health(p),
        })
    return {"cpus": cpus, "gpus": gpus}


async def memory(c: RedfishClient) -> dict[str, Any]:
    system = await c.system()
    dimms, empty = [], 0
    for d in await c.members(_link(system, "Memory")):
        hpe = _hpe(d)
        status = hpe.get("DIMMStatus") or (d.get("Status") or {}).get("State")
        if not d.get("CapacityMiB") or status in {"NotPresent", "Absent", "Unknown"}:
            empty += 1
            continue
        dimms.append({
            "slot": d.get("DeviceLocator") or d.get("Name"),
            "capacity_gib": d["CapacityMiB"] // 1024,
            "type": d.get("MemoryDeviceType"),
            "module_type": d.get("BaseModuleType"),
            "speed_mts": hpe.get("MaxOperatingSpeedMTs") or d.get("OperatingSpeedMhz"),
            "manufacturer": hpe.get("VendorName") or d.get("Manufacturer"),
            "part_number": _clean(hpe.get("PartNumber")) or _clean(d.get("PartNumber")),
            "health": _health(d),
        })
    return {
        "total_gib": sum(d["capacity_gib"] for d in dimms),
        "populated_slots": len(dimms),
        "empty_slots": empty,
        "dimms": dimms,
    }


async def storage(c: RedfishClient) -> list[dict[str, Any]]:
    system = await c.system()
    result = []
    for s in await c.members(_link(system, "Storage")):
        controllers = list(s.get("StorageControllers") or [])
        # Direct-attached (DA*) subsystems only describe the drive's own chip, not a real controller.
        direct_attached = (s.get("Id") or "").upper().startswith("DA")
        if direct_attached:
            controllers = []
        elif not controllers and _link(s, "Controllers"):
            try:
                controllers = await c.members(_link(s, "Controllers"))
            except RedfishError:
                controllers = []
        drives = []
        for ref in s.get("Drives") or []:
            try:
                d = await c.get(ref["@odata.id"])
            except RedfishError:
                continue
            loc = ((d.get("PhysicalLocation") or {}).get("PartLocation") or {})
            drives.append({
                "name": d.get("Name"),
                "model": d.get("Model"),
                "media_type": d.get("MediaType"),
                "protocol": d.get("Protocol"),
                "capacity_gb": round(d["CapacityBytes"] / 1e9) if d.get("CapacityBytes") else None,
                "serial_number": d.get("SerialNumber"),
                "firmware": d.get("Revision") or d.get("FirmwareVersion"),
                "location": loc.get("ServiceLabel"),
                "health": _health(d),
            })
        volumes = []
        if _link(s, "Volumes"):
            try:
                for v in await c.members(_link(s, "Volumes")):
                    volumes.append({
                        "name": v.get("Name"),
                        "raid_type": v.get("RAIDType") or v.get("VolumeType"),
                        "capacity_gb": round(v["CapacityBytes"] / 1e9) if v.get("CapacityBytes") else None,
                        "health": _health(v),
                    })
            except RedfishError:
                pass
        if not drives and not controllers and not volumes:
            continue
        result.append({
            "id": s.get("Id"),
            "name": s.get("Name"),
            "direct_attached": direct_attached,
            "controllers": [
                {
                    "model": ctl.get("Model") or ctl.get("Name"),
                    "firmware": ctl.get("FirmwareVersion"),
                    "health": _health(ctl),
                }
                for ctl in controllers
            ],
            "drives": drives,
            "volumes": volumes,
        })
    return result


async def network_adapters(c: RedfishClient) -> list[dict[str, Any]]:
    chassis = await c.chassis()
    result = []
    try:
        adapters = await c.members(_link(chassis, "NetworkAdapters"))
    except RedfishError:
        return []
    for a in adapters:
        ctl = (a.get("Controllers") or [{}])[0]
        ports = []
        try:
            raw_ports = await c.members(_link(a, "Ports") or _link(a, "NetworkPorts"))
        except RedfishError:
            raw_ports = []
        for p in raw_ports:
            eth = p.get("Ethernet") or {}
            macs = eth.get("AssociatedMACAddresses") or p.get("AssociatedNetworkAddresses") or []
            lldp = (eth.get("LLDPReceive") or {})
            ports.append({
                "id": p.get("Id"),
                "mac": (macs[0].lower() if macs else None),
                "link_status": p.get("LinkStatus"),
                "speed_gbps": p.get("CurrentSpeedGbps"),
                "lldp_neighbor": {
                    "system": _clean(lldp.get("SystemName")),
                    "chassis_id": _lldp_text(lldp.get("ChassisId")),
                    "port": _lldp_text(lldp.get("PortId")),
                    "management_ipv4": _clean(lldp.get("ManagementAddressIPv4")),
                } if lldp else None,
            })
        loc = ((a.get("Location") or ctl.get("Location") or {}).get("PartLocation") or {})
        result.append({
            "name": a.get("Name"),
            "model": a.get("Model"),
            "manufacturer": a.get("Manufacturer"),
            "part_number": a.get("PartNumber") or a.get("SKU"),
            "serial_number": a.get("SerialNumber"),
            "location": loc.get("ServiceLabel"),
            "firmware": ctl.get("FirmwarePackageVersion"),
            "health": _health(a),
            "ports": ports,
        })
    return result


async def event_log(c: RedfishClient, log: str, limit: int, severity: str | None) -> list[dict[str, Any]]:
    """Return newest-first entries of the IML (server) or IEL (iLO event log)."""
    owner = await c.system() if log == "iml" else await c.manager()
    services = await c.get(_link(owner, "LogServices") or "")
    log_uri = next(
        (m["@odata.id"] for m in services.get("Members", []) if m.get("@odata.id", "").rstrip("/").upper().endswith("/" + log.upper())),
        None,
    )
    if not log_uri:
        raise RedfishError(f"{c.server.name}: {log.upper()} log service not found")
    entries_uri = _link(await c.get(log_uri), "Entries")
    # iLO embeds full LogEntry records in the Entries collection.
    entries = (await c.get(entries_uri or "")).get("Members", [])
    out = []
    for e in entries:
        hpe = _hpe(e)
        sev = e.get("Severity") or hpe.get("Severity")
        if severity and (sev or "").lower() != severity.lower():
            continue
        out.append({
            "created": e.get("Created"),
            "updated": hpe.get("Updated"),
            "severity": sev,
            "message": e.get("Message"),
            "class": hpe.get("ClassDescription"),
            "count": hpe.get("Count"),
            "repaired": hpe.get("Repaired"),
        })
    out.sort(key=lambda x: x.get("updated") or x.get("created") or "", reverse=True)
    return out[:limit]
