"""HPE OneView / Synergy Composer client and read-only views.

Login follows proliant-cli's ``proliant.oneview.client``: negotiate
``X-API-Version`` from ``/rest/version``, POST ``/rest/login-sessions``, send
the session ID in the ``auth`` header, and DELETE the session on exit.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

import httpx

from proliant_mcp.config import OneViewAppliance
from proliant_mcp.util import matches

_TIMEOUT = httpx.Timeout(timeout=60.0, connect=10.0)
_LOGIN_TIMEOUT = httpx.Timeout(timeout=15.0, connect=8.0)
_PAGE = 500


class OneViewError(RuntimeError):
    pass


class OneViewLoginError(OneViewError):
    """The appliance answered but refused the user name or password."""


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return body.get("message") or body.get("errorCode") or resp.reason_phrase
    except ValueError:
        return resp.reason_phrase or ""


class OneViewClient:
    """One authenticated OneView session. Use as ``async with OneViewClient(appliance)``."""

    def __init__(self, appliance: OneViewAppliance, transport: httpx.AsyncBaseTransport | None = None):
        self.appliance = appliance
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._logged_in = False

    async def __aenter__(self) -> "OneViewClient":
        a = self.appliance
        self._http = httpx.AsyncClient(base_url=a.base_url, verify=a.verify_ssl, timeout=_TIMEOUT, transport=self._transport)
        try:
            version = (await self._http.get("/rest/version", timeout=_LOGIN_TIMEOUT)).json().get("currentVersion", 800)
            self._http.headers["X-API-Version"] = str(version)
            resp = await self._http.post(
                "/rest/login-sessions",
                json={"userName": a.username, "password": a.password, "loginMsgAck": True},
                timeout=_LOGIN_TIMEOUT,
            )
        except (httpx.TransportError, ValueError) as exc:
            await self._http.aclose()
            raise OneViewError(f"{a.name}: cannot reach OneView at {a.host} ({exc.__class__.__name__})") from exc
        if resp.status_code >= 400:
            await self._http.aclose()
            raise OneViewLoginError(f"{a.name}: OneView login failed (HTTP {resp.status_code} {_detail(resp)})")
        self._http.headers["auth"] = resp.json().get("sessionID", "")
        self._logged_in = True
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._http is None:
            return
        try:
            if self._logged_in:
                await self._http.delete("/rest/login-sessions")
        except Exception:  # noqa: BLE001 - logout is best effort
            pass
        finally:
            await self._http.aclose()
            self._http = None

    async def get(self, uri: str, params: Any = None) -> dict[str, Any]:
        assert self._http is not None
        try:
            resp = await self._http.get(uri, params=params)
        except httpx.TransportError as exc:
            raise OneViewError(f"{self.appliance.name}: GET {uri} failed ({exc.__class__.__name__})") from exc
        if resp.status_code >= 400:
            raise OneViewError(f"{self.appliance.name}: GET {uri} -> HTTP {resp.status_code} {_detail(resp)}")
        return resp.json() if resp.content else {}

    async def get_all(self, uri: str, params: list[tuple[str, str]] | None = None, max_items: int = 10000) -> list[dict[str, Any]]:
        members: list[dict[str, Any]] = []
        start = 0
        while len(members) < max_items:
            page = await self.get(uri, [*(params or []), ("start", str(start)), ("count", str(_PAGE))])
            batch = page.get("members") or []
            members.extend(batch)
            start += len(batch)
            if not batch or start >= int(page.get("total") or 0):
                break
        return members[:max_items]


# ── normalizers ───────────────────────────────────────────────────────────────


def _ips(mp_host_info: dict[str, Any] | None) -> list[str]:
    addrs = (mp_host_info or {}).get("mpIpAddresses") or []
    routed = [a["address"] for a in addrs if a.get("address") and (a.get("type") or "").lower() != "linklocal"]
    return routed + [a["address"] for a in addrs if a.get("address") and (a.get("type") or "").lower() == "linklocal"]


def _cpu(h: dict[str, Any]) -> str | None:
    if not h.get("processorType"):
        return None
    model = h["processorType"].split(" @ ")[0].strip()
    return f"{h.get('processorCount') or 0} x {model} ({h.get('processorCoreCount') or '?'} cores, {(h.get('processorSpeedMhz') or 0) / 1000:.2f} GHz)"


def hardware_row(h: dict[str, Any], profile_names: dict[str, str], enclosure_names: dict[str, str]) -> dict[str, Any]:
    mp = h.get("mpHostInfo") or {}
    ips = _ips(mp)
    return {
        "name": h.get("name"),
        "server_name": h.get("serverName") or None,
        "model": h.get("model"),
        "serial_number": h.get("serialNumber"),
        "part_number": h.get("partNumber"),
        "generation": h.get("generation"),
        "status": h.get("status"),
        "state": h.get("state"),
        "power_state": h.get("powerState"),
        "maintenance_mode": h.get("maintenanceMode"),
        "profile": profile_names.get(h.get("serverProfileUri") or ""),
        "location": enclosure_names.get(h.get("locationUri") or "") or h.get("locationUri"),
        "bay": h.get("position"),
        "cpu": _cpu(h),
        "memory_gb": round(h["memoryMb"] / 1024) if h.get("memoryMb") else None,
        "rom_version": h.get("romVersion"),
        "os": h.get("operatingSystem") or None,
        "ilo": {
            "model": h.get("mpModel"),
            "firmware": h.get("mpFirmwareVersion"),
            "hostname": mp.get("mpHostName"),
            "ip": ips[0] if ips else None,
            "license": h.get("mpLicenseType"),
            "state": h.get("mpState"),
        },
        "uuid": h.get("uuid"),
        "uri": h.get("uri"),
    }


def profile_row(p: dict[str, Any], hardware_names: dict[str, str], template_names: dict[str, str]) -> dict[str, Any]:
    fw = p.get("firmware") or {}
    return {
        "name": p.get("name"),
        "status": p.get("status"),
        "state": p.get("state"),
        "server_hardware": hardware_names.get(p.get("serverHardwareUri") or ""),
        "template": template_names.get(p.get("serverProfileTemplateUri") or ""),
        "template_compliance": p.get("templateCompliance"),
        "firmware_baseline": (fw.get("firmwareBaselineUri") or "").rsplit("/", 1)[-1] or None,
        "manage_firmware": fw.get("manageFirmware"),
        "boot_mode": (p.get("bootMode") or {}).get("mode"),
        "connections": len((p.get("connectionSettings") or {}).get("connections") or []),
        "uri": p.get("uri"),
    }


def alert_row(a: dict[str, Any]) -> dict[str, Any]:
    res = a.get("associatedResource") or {}
    return {
        "id": (a.get("uri") or "").rsplit("/", 1)[-1],
        "severity": a.get("severity"),
        "state": a.get("alertState"),
        "resource": res.get("resourceName"),
        "resource_type": res.get("resourceCategory") or a.get("physicalResourceType"),
        "description": a.get("description"),
        "corrective_action": a.get("correctiveAction"),
        "created": a.get("created"),
        "urgency": a.get("urgency"),
        "health_category": a.get("healthCategory"),
    }


def enclosure_row(e: dict[str, Any]) -> dict[str, Any]:
    bays = e.get("deviceBays") or []
    return {
        "name": e.get("name"),
        "model": e.get("enclosureModel"),
        "serial_number": e.get("serialNumber"),
        "status": e.get("status"),
        "state": e.get("state"),
        "firmware_baseline": e.get("fwBaselineName"),
        "device_bays": {"total": len(bays), "populated": sum(1 for b in bays if b.get("devicePresence") == "Present")},
        "interconnect_bays": e.get("interconnectBayCount"),
        "appliance_bays": [
            {"bay": b.get("bayNumber"), "model": b.get("model"), "status": b.get("status")}
            for b in e.get("applianceBays") or [] if b.get("devicePresence") == "Present"
        ],
        "power": {
            "mode": e.get("powerMode"),
            "capacity_watts": e.get("powerCapacityWatts"),
            "allocated_watts": e.get("powerAllocatedWatts"),
            "available_watts": e.get("powerAvailableWatts"),
        },
        "power_supplies": [{"bay": b.get("bayNumber"), "status": b.get("status")} for b in e.get("powerSupplyBays") or [] if b.get("devicePresence") == "Present"],
        "fans_not_ok": [b.get("bayNumber") for b in e.get("fanBays") or [] if b.get("devicePresence") == "Present" and b.get("status") != "OK"],
        "uri": e.get("uri"),
    }


def interconnect_row(i: dict[str, Any]) -> dict[str, Any]:
    ips = [x.get("ipAddress") for x in i.get("ipAddressList") or [] if "LinkLocal" not in (x.get("ipAddressType") or "")]
    return {
        "name": i.get("name"),
        "model": i.get("model"),
        "firmware": i.get("firmwareVersion"),
        "status": i.get("status"),
        "state": i.get("state"),
        "hardware_health": i.get("interconnectHardwareHealth"),
        "power_state": i.get("powerState"),
        "enclosure": i.get("enclosureName"),
        "hostname": i.get("hostName"),
        "ip": ips[0] if ips else None,
        "serial_number": i.get("serialNumber"),
        "part_number": i.get("partNumber"),
        "ports": i.get("portCount"),
        "uri": i.get("uri"),
    }


def baseline_row(b: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": b.get("name"),
        "short_name": b.get("baselineShortName"),
        "version": b.get("version"),
        "release_date": (b.get("releaseDate") or "")[:10] or None,
        "type": b.get("bundleType"),
        "state": b.get("state"),
        "status": b.get("status"),
        "iso": b.get("isoFileName"),
        "size_gb": round(b["bundleSize"] / 1e9, 1) if b.get("bundleSize") else None,
        "uri": b.get("uri"),
    }


# ── views used by the MCP tools ───────────────────────────────────────────────


def _names(items: list[dict[str, Any]], key: str = "name") -> dict[str, str]:
    return {i["uri"]: i.get(key) for i in items if i.get("uri")}


async def appliance_info(c: OneViewClient) -> dict[str, Any]:
    version, health, hardware, profiles, enclosures, interconnects, alerts = await asyncio.gather(
        c.get("/rest/appliance/nodeinfo/version"),
        c.get("/rest/appliance/health-status"),
        c.get_all("/rest/server-hardware"),
        c.get("/rest/server-profiles", {"count": "1"}),
        c.get("/rest/enclosures", {"count": "1"}),
        c.get("/rest/interconnects", {"count": "1"}),
        c.get_all("/rest/alerts", [("filter", "\"alertState='Active'\"")]),
    )
    return {
        "appliance": c.appliance.name,
        "model": version.get("modelNumber"),
        "software_version": version.get("softwareVersion"),
        "family": version.get("family"),
        "serial_number": version.get("serialNumber"),
        "api_version": int(c._http.headers["X-API-Version"]) if c._http else None,
        "resource_health": [
            {"resource": m.get("resourceType"), "severity": m.get("severity"), "available": m.get("available"), "capacity": (m.get("capacity") or "").strip(), "status": m.get("statusMessage")}
            for m in health.get("members") or []
        ],
        "counts": {
            "server_hardware": len(hardware),
            "server_profiles": profiles.get("total"),
            "enclosures": enclosures.get("total"),
            "interconnects": interconnects.get("total"),
        },
        "server_hardware_by_status": dict(Counter(h.get("status") or "Unknown" for h in hardware)),
        "server_hardware_by_power": dict(Counter(h.get("powerState") or "Unknown" for h in hardware)),
        "active_alerts": {"count": len(alerts), "by_severity": dict(Counter(a.get("severity") or "Unknown" for a in alerts))},
    }


async def list_hardware(c: OneViewClient) -> list[dict[str, Any]]:
    hardware, profiles, enclosures = await asyncio.gather(
        c.get_all("/rest/server-hardware"), c.get_all("/rest/server-profiles"), c.get_all("/rest/enclosures")
    )
    return [hardware_row(h, _names(profiles), _names(enclosures)) for h in hardware]


def _find_hardware(hardware: list[dict[str, Any]], query: str) -> dict[str, Any]:
    for h in hardware:
        mp = h.get("mpHostInfo") or {}
        if matches(query, h.get("name"), h.get("serverName"), h.get("serialNumber"), h.get("uuid"), mp.get("mpHostName"), *_ips(mp)):
            return h
    q = query.strip().lower()
    partial = [h for h in hardware if q in (h.get("name") or "").lower() or q in (h.get("serverName") or "").lower()]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(h.get("name") for h in (partial or hardware)[:15])
    raise OneViewError(f"No OneView server hardware matches '{query}'. {'Partial matches' if partial else 'Known'}: {names}")


async def get_hardware(c: OneViewClient, query: str) -> dict[str, Any]:
    hardware, profiles, enclosures = await asyncio.gather(
        c.get_all("/rest/server-hardware"), c.get_all("/rest/server-profiles"), c.get_all("/rest/enclosures")
    )
    h = _find_hardware(hardware, query)
    firmware, alerts = await asyncio.gather(
        c.get(h["serverFirmwareInventoryUri"]) if h.get("serverFirmwareInventoryUri") else asyncio.sleep(0, {}),
        c.get_all("/rest/alerts", [("filter", "\"alertState='Active'\""), ("filter", f"\"resourceUri='{h['uri']}'\"")], max_items=50),
    )
    return {
        **hardware_row(h, _names(profiles), _names(enclosures)),
        "uid_light": h.get("uidState"),
        "firmware": [
            {"location": (f.get("componentLocation") or "").strip() or None, "name": f.get("componentName"), "version": f.get("componentVersion")}
            for f in (firmware or {}).get("components") or []
        ],
        "active_alerts": [alert_row(a) for a in alerts],
    }


async def list_profiles(c: OneViewClient) -> list[dict[str, Any]]:
    profiles, hardware, templates = await asyncio.gather(
        c.get_all("/rest/server-profiles"), c.get_all("/rest/server-hardware"), c.get_all("/rest/server-profile-templates")
    )
    return [profile_row(p, _names(hardware), _names(templates)) for p in profiles]


async def list_alerts(c: OneViewClient, active_only: bool, severity: str | None, limit: int) -> list[dict[str, Any]]:
    params = [("sort", "created:descending")]
    if active_only:
        params.append(("filter", "\"alertState='Active'\""))
    if severity:
        params.append(("filter", f"\"severity='{severity}'\""))
    return [alert_row(a) for a in await c.get_all("/rest/alerts", params, max_items=limit)]


async def list_enclosures(c: OneViewClient) -> list[dict[str, Any]]:
    return [enclosure_row(e) for e in await c.get_all("/rest/enclosures")]


async def list_interconnects(c: OneViewClient) -> list[dict[str, Any]]:
    return [interconnect_row(i) for i in await c.get_all("/rest/interconnects")]


async def list_baselines(c: OneViewClient) -> list[dict[str, Any]]:
    rows = [baseline_row(b) for b in await c.get_all("/rest/firmware-drivers")]
    return sorted(rows, key=lambda r: r["release_date"] or "", reverse=True)
