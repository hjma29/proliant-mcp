"""Read-only iLO inventory helpers. Each returns plain JSON-serialisable data."""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from typing import Any

from proliant_mcp.redfish import RedfishClient, RedfishError


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


async def _boot_order(c: RedfishClient, system: dict[str, Any]) -> list[str] | None:
    """UEFI boot entries by display name, in boot order (entries not in BootOrder last)."""
    uri = _link(system, "BootOptions") or _link(system.get("Boot") or {}, "BootOptions")
    if not uri:
        return None
    try:
        options = await c.members(uri)
    except RedfishError:
        return None
    by_ref = {o.get("BootOptionReference"): o.get("DisplayName") or o.get("Alias") or o.get("Id") for o in options}
    order = [by_ref.pop(ref) for ref in (system.get("Boot") or {}).get("BootOrder") or [] if ref in by_ref]
    return [n for n in [*order, *by_ref.values()] if n]


_NOT_OS_ENTRY = re.compile(r" : |usb|pxe|http|shell|network|iscsi|embedded|cd/dvd|cdrom|virtual|diagnos|setup|\bmac:", re.I)


def _os_entries(boot_order: list[str] | None) -> list[str]:
    """Boot entries an OS installer created ('Ubuntu', 'VMware ESXi', 'Windows Boot Manager'), not devices or network boot."""
    return [n for n in boot_order or [] if not _NOT_OS_ENTRY.search(n)]


def _os_state(system: dict[str, Any], hpe: dict[str, Any], boot_order: list[str] | None) -> str:
    """One plain line on whether an OS is running, for answers like 'which OS do they run'."""
    host_os = hpe.get("HostOS") or {}
    progress = (system.get("BootProgress") or {}).get("LastState") or ""
    post = str(hpe.get("PostState") or "")
    loaders = _os_entries(boot_order)
    hint = f"; boot entries suggest {', '.join(loaders)}" if loaders else ""
    if str(system.get("PowerState") or "").lower() != "on":
        return f"server is powered off{hint}"
    if host_os.get("OsName"):
        return f"running {host_os['OsName']} (reported by the HPE agent)"
    if progress == "SetupEntered":
        return f"stopped in BIOS setup (RBSU); no OS booted{hint}"
    if post.startswith("InPost") or progress in ("SystemHardwareInitializationComplete", "PrimaryProcessorInitializationStarted"):
        note = "; no boot entries at all" if boot_order == [] else hint
        return f"still in POST ({post or progress}); no OS booted{note}"
    if progress in ("OSRunning", "OSBootStarted") or post == "FinishedPost":
        return f"OS booted, but no HPE agent (AMS) reports its name{hint}"
    return f"unknown (POST state {post or '?'}, boot progress {progress or '?'}){hint}"


async def server_summary(c: RedfishClient) -> dict[str, Any]:
    system = await c.system()
    manager = await c.manager()
    hpe = _hpe(system)
    proc = system.get("ProcessorSummary") or {}
    mem = system.get("MemorySummary") or {}
    uid = system.get("LocationIndicatorActive")
    host_os = hpe.get("HostOS") or {}
    progress = system.get("BootProgress") or {}
    boot_order = await _boot_order(c, system)
    ams = ((hpe.get("AggregateHealthStatus") or {}).get("AgentlessManagementService")
           or (hpe.get("DeviceDiscoveryComplete") or {}).get("AMSDeviceDiscovery"))
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
        "os": {
            "state": _os_state(system, hpe, boot_order),
            "name": _clean(host_os.get("OsName")),
            "version": _clean(host_os.get("OsVersion")),
            "fqdn": hpe.get("ServerFQDN") or None,
            "hpe_agent": ams,
            "boot_progress": progress.get("LastState"),
            "boot_progress_time": progress.get("LastStateTime"),
            "os_boot_entries": _os_entries(boot_order),
            "boot_order": None if boot_order is None else boot_order[:8] + ([f"...and {len(boot_order) - 8} more"] if len(boot_order) > 8 else []),
        },
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


def parse_time(value: str | None) -> dt.datetime | None:
    """An iLO timestamp as an aware UTC datetime, or None if missing or unparseable."""
    if not value or value.startswith("0000"):
        return None
    try:
        t = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


def _valid(t: dt.datetime | None, now: dt.datetime) -> bool:
    # iLO stamps 0000-00-00, an early default (2001) or a far-future date (2039) after an RTC reset
    # or before it got NTP time. No iLO with Redfish predates 2012.
    return t is not None and t.year >= 2012 and t <= now + dt.timedelta(days=1)


def parse_when(value: str, now: dt.datetime, end_of_day: bool = False) -> dt.datetime:
    """'7d', '24h', '30m', a date (2026-11-12) or an ISO 8601 time (UTC unless it has a zone).
    With *end_of_day* a bare date means the end of that day (for 'until')."""
    v = value.strip()
    rel = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([dhm])", v, re.I)
    if rel:
        unit = {"d": "days", "h": "hours", "m": "minutes"}[rel.group(2).lower()]
        return now - dt.timedelta(**{unit: float(rel.group(1))})
    t = parse_time(v)
    if t is None:
        raise ValueError(f"'{value}' is not a time; use e.g. 7d, 24h, 2026-11-12 or 2026-11-12T08:00:00Z")
    if end_of_day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        t += dt.timedelta(days=1) - dt.timedelta(microseconds=1)
    return t


async def _log_entries(c: RedfishClient, log: str) -> list[dict[str, Any]]:
    owner = await c.manager() if log == "iel" else await c.system()
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
        out.append({
            "created": e.get("Created"),
            "updated": hpe.get("Updated"),
            "severity": e.get("Severity") or hpe.get("Severity"),
            "message": e.get("Message"),
            "class": hpe.get("ClassDescription"),
            "count": hpe.get("Count"),
            "repaired": hpe.get("Repaired"),
        })
    return out


def _span(e: dict[str, Any], now: dt.datetime) -> tuple[dt.datetime | None, dt.datetime | None]:
    """(first, last) valid time of an entry; iLO folds repeats into one entry with a Count."""
    first, last = parse_time(e.get("created")), parse_time(e.get("updated"))
    first = first if _valid(first, now) else None
    last = last if _valid(last, now) else first
    return first or last, last


_SEVERITY_RANK = {"ok": 0, "warning": 1, "critical": 2}


def _summarize(entries: list[tuple[dt.datetime | None, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Identical messages folded into one row with the total count and first/last valid time."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for last, e in entries:
        key = (e["severity"] or "", " ".join((e["message"] or "").split()))
        g = groups.setdefault(key, {"severity": key[0] or None, "message": key[1], "count": 0, "first": None, "last": None,
                                    "class": e.get("class"), "repaired": e.get("repaired")})
        g["count"] += int(e.get("count") or 1)
        if last is not None:
            stamp = last.isoformat().replace("+00:00", "Z")
            first = parse_time(e.get("created"))
            first_stamp = first.isoformat().replace("+00:00", "Z") if first and first <= last else stamp
            g["last"] = max(g["last"] or stamp, stamp)
            g["first"] = min(g["first"] or first_stamp, first_stamp)
        else:
            g["time_invalid"] = True
        if e.get("repaired") is False:
            g["repaired"] = False
    return sorted(groups.values(), key=lambda g: (-_SEVERITY_RANK.get((g["severity"] or "").lower(), 0), -g["count"], g["message"]))


async def event_log(
    c: RedfishClient,
    log: str,
    limit: int,
    severity: str | None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    contains: str | None = None,
    now: dt.datetime | None = None,
    min_severity: str | None = None,
    summary: bool = False,
) -> list[dict[str, Any]]:
    """Newest-first entries of the IML (server), IEL (iLO event log) or SL (security log). Entries whose
    time the iLO got wrong (clock reset) are marked ``time_invalid``, sorted last and left out of time
    filters. With *summary*, identical messages are grouped (most severe and most frequent first)."""
    now = now or dt.datetime.now(dt.UTC)
    needle = (contains or "").lower()
    floor = _SEVERITY_RANK.get((min_severity or "").lower(), 0)
    out = []
    for e in await _log_entries(c, log):
        if severity and (e["severity"] or "").lower() != severity.lower():
            continue
        if floor and _SEVERITY_RANK.get((e["severity"] or "").lower(), 0) < floor:
            continue
        if needle and needle not in (e["message"] or "").lower():
            continue
        first, last = _span(e, now)
        if first is None:
            if since or until:
                continue
            e["time_invalid"] = True
        elif (since and last < since) or (until and first > until):
            continue
        out.append((last, e))
    if summary:
        return _summarize(out)[:limit]
    out.sort(key=lambda x: (x[0] is not None, x[0] or now), reverse=True)
    return [e for _, e in out[:limit]]


# "Browser login: Administrator - 192.0.2.5(host.example.com)." / "Host REST login: System Administrator"
_LOGIN = re.compile(r"^(?P<kind>.+?) login: (?P<user>.+?)(?: - (?P<ip>[0-9A-Fa-f:.]+)(?:\((?P<host>[^)]*)\))?)?\.?\s*$")
_FAILED = re.compile(r"login fail|failed login|invalid (user|login|password)", re.I)
_ALERT = re.compile(r"unauthori[sz]ed|locked out|lockout", re.I)
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
LIST_CAP = 20


async def own_ip(c: RedfishClient) -> str | None:
    """The address this computer reaches the iLO from, as the iLO sees it (our own session)."""
    if not c.session_uri:
        return None
    try:
        return _hpe(await c.get(c.session_uri)).get("UserIP") or None
    except RedfishError:
        return None


async def logins(
    c: RedfishClient,
    since: dt.datetime,
    until: dt.datetime,
    tz: dt.tzinfo,
    now: dt.datetime | None = None,
    include_this_computer: bool = True,
) -> dict[str, Any]:
    """Who logged in to this iLO between *since* and *until*, from the iLO Event Log. Logins from
    the computer running proliant-mcp (its own Redfish sessions) are counted in ``this_computer_logins``
    and left out of the other counts unless *include_this_computer*."""
    now = now or dt.datetime.now(dt.UTC)
    entries = await _log_entries(c, "iel")
    me = await own_ip(c)
    total = failed = invalid = mine = 0
    by_day: Counter[str] = Counter()
    by_type: Counter[str] = Counter()
    users: Counter[str] = Counter()
    sources: dict[tuple[str, str | None], dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    oldest: dt.datetime | None = None
    for e in entries:
        msg = (e.get("message") or "").strip()
        first, last = _span(e, now)
        if first is None:
            invalid += 1
            continue
        oldest = first if oldest is None or first < oldest else oldest
        if last < since or first > until:
            continue
        n = int(e.get("count") or 1)
        stamp = last.isoformat().replace("+00:00", "Z")
        if _ALERT.search(msg) and "login" in msg.lower():
            alerts.append({"time": stamp, "message": msg, "count": n})
            continue
        if _FAILED.search(msg):
            failed += n
            ip = _IP.search(msg)
            failures.append({"time": stamp, "message": msg, "count": n, **({"ip": ip.group(0)} if ip else {})})
            continue
        m = _LOGIN.match(msg)
        if not m:
            continue
        if me and m["ip"] == me:
            mine += n
            if not include_this_computer:
                continue
        total += n
        by_day[first.astimezone(tz).date().isoformat()] += n
        by_type[m["kind"]] += n
        users[m["user"]] += n
        if m["ip"]:
            host = m["host"] if m["host"] and "not found" not in m["host"].lower() else None
            src = sources.setdefault((m["ip"], host), {"ip": m["ip"], "host": host, "logins": 0, "last_login": None})
            src["logins"] += n
            src["last_login"] = max(src["last_login"] or stamp, stamp)
            if me and m["ip"] == me:
                src["this_computer"] = True
    ranked = sorted(sources.values(), key=lambda s: -s["logins"])
    failures.sort(key=lambda f: f["time"], reverse=True)
    alerts.sort(key=lambda f: f["time"], reverse=True)
    result: dict[str, Any] = {
        "logins": total,
        "this_computer_logins": mine,
        "failed_logins": failed,
        "by_day": dict(sorted(by_day.items())),
        "by_type": dict(by_type.most_common()),
        "users": dict(users.most_common()),
        "sources": ranked[:LIST_CAP],
        "failures": failures[:LIST_CAP],
        "security_alerts": alerts[:LIST_CAP],
        "log_starts": oldest.isoformat().replace("+00:00", "Z") if oldest else None,
        "log_covers_window": oldest is not None and oldest <= since,
    }
    if len(ranked) > LIST_CAP:
        result["more_sources"] = len(ranked) - LIST_CAP
    if invalid:
        result["entries_with_invalid_time"] = invalid
    return result
