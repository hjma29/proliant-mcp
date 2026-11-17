"""HPE Compute Ops Management (GreenLake) client and read-only views.

Auth is the GreenLake OAuth2 client-credentials flow (an API client created in
the GreenLake workspace), the same as proliant-cli's ``proliant.com.auth``.
Endpoints are the public ``/compute-ops-mgmt/v1`` API.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

import httpx

from proliant_mcp.config import ComAccount
from proliant_mcp.util import matches

TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"
API = "/compute-ops-mgmt/v1"
_TIMEOUT = httpx.Timeout(timeout=60.0, connect=10.0)
_PAGE = 500

# client_id -> (token, monotonic expiry); tokens last ~2 h, reuse them across tool calls.
_tokens: dict[str, tuple[str, float]] = {}


class ComError(RuntimeError):
    pass


def _http_error(resp: httpx.Response, what: str) -> ComError:
    if resp.status_code == 403:
        return ComError(
            f"COM {what}: HTTP 403. The API client has no Compute Ops Management role in this "
            "workspace; ask the GreenLake administrator to assign one."
        )
    try:
        detail = resp.json().get("message") or resp.text[:200]
    except ValueError:
        detail = resp.text[:200]
    return ComError(f"COM {what}: HTTP {resp.status_code} {detail}")


class ComClient:
    """``async with ComClient(account) as c: await c.get("/servers")``"""

    def __init__(self, account: ComAccount, transport: httpx.AsyncBaseTransport | None = None):
        self.account = account
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ComClient":
        try:
            base = self.account.base_url
        except ValueError as exc:
            raise ComError(str(exc)) from exc
        self._http = httpx.AsyncClient(base_url=base, timeout=_TIMEOUT, transport=self._transport)
        try:
            self._http.headers["Authorization"] = f"Bearer {await self._token()}"
        except BaseException:
            await self._http.aclose()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _token(self) -> str:
        cached = _tokens.get(self.account.client_id)
        if cached and cached[1] > time.monotonic() + 60:
            return cached[0]
        assert self._http is not None
        try:
            resp = await self._http.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self.account.client_id, self.account.client_secret),
                timeout=20,
            )
        except httpx.TransportError as exc:
            raise ComError(f"COM: cannot reach GreenLake SSO ({exc.__class__.__name__})") from exc
        if resp.status_code >= 400:
            raise ComError(
                f"COM login failed for account '{self.account.name}' (HTTP {resp.status_code}). "
                "Check the GreenLake API client ID/secret."
            )
        data = resp.json()
        _tokens[self.account.client_id] = (data["access_token"], time.monotonic() + float(data.get("expires_in", 7200)))
        return data["access_token"]

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._http is not None
        url = path if path.startswith("/compute-ops-mgmt/") else API + path
        try:
            resp = await self._http.get(url, params=params)
        except httpx.TransportError as exc:
            raise ComError(f"COM GET {url} failed ({exc.__class__.__name__})") from exc
        if resp.status_code == 401:
            _tokens.pop(self.account.client_id, None)
        if resp.status_code >= 400:
            raise _http_error(resp, f"GET {url}")
        return resp.json() if resp.content else {}

    async def get_all(self, path: str, params: dict[str, Any] | None = None, max_items: int = 5000) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset = 0
        while len(items) < max_items:
            page = await self.get(path, {**(params or {}), "limit": _PAGE, "offset": offset})
            batch = page.get("items") or []
            items.extend(batch)
            offset += len(batch)
            if not batch or offset >= int(page.get("total") or 0):
                break
        return items[:max_items]


# ── normalizers ───────────────────────────────────────────────────────────────


def server_row(s: dict[str, Any]) -> dict[str, Any]:
    hw = s.get("hardware") or {}
    bmc = hw.get("bmc") or {}
    state = s.get("state") or {}
    host = s.get("host") or {}
    return {
        "id": s.get("id"),
        "name": s.get("name"),
        "serial_number": hw.get("serialNumber"),
        "model": hw.get("model"),
        "product_id": hw.get("productId"),
        "generation": s.get("serverGeneration"),
        "health": (hw.get("health") or {}).get("summary"),
        "power_state": hw.get("powerState"),
        "connected": state.get("connected"),
        "connection_type": s.get("connectionType"),
        "subscription": {"state": state.get("subscriptionState"), "tier": state.get("subscriptionTier"), "expires_at": state.get("subscriptionExpiresAt")},
        "maintenance_mode": s.get("maintenanceMode"),
        "ilo": {"hostname": bmc.get("hostname"), "ip": bmc.get("ip"), "version": bmc.get("version")},
        "os": " ".join(x for x in (host.get("osName"), host.get("osVersion")) if x) or None,
        "os_hostname": host.get("hostname"),
        "processor": (s.get("processorVendor") or "").strip() or None,
        "firmware_bundle_id": (s.get("firmwareBundleUri") or "").rsplit("/", 1)[-1] or None,
        "oneview": (s.get("oneview") or {}).get("name"),
    }


def server_detail(s: dict[str, Any]) -> dict[str, Any]:
    hw = s.get("hardware") or {}
    health = hw.get("health") or {}
    last = s.get("lastFirmwareUpdate") or {}
    return {
        **server_row(s),
        "uuid": hw.get("uuid"),
        "manufacturing_date": hw.get("manufacturingDate"),
        "uid_light": hw.get("indicatorLed"),
        "health_detail": {k: v for k, v in health.items() if k != "summary"},
        "ilo_license": (hw.get("bmc") or {}).get("license"),
        "ilo_mac": (hw.get("bmc") or {}).get("mac"),
        "auto_ilo_firmware_update": s.get("autoIloFwUpdate"),
        "last_firmware_update": {
            "status": last.get("status"),
            "attempted_at": last.get("attemptedAt"),
            "completed_at": last.get("completedAt"),
            "bundle_id": (last.get("attemptedBaselineUri") or "").rsplit("/", 1)[-1] or None,
        } if last else None,
        "last_inventory_at": s.get("lastFullInventoryCollectionAt"),
        "firmware": [
            {"name": f.get("name"), "version": f.get("version"), "location": f.get("deviceContext")}
            for f in s.get("firmwareInventory") or []
        ],
    }


def alert_row(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": a.get("id"),
        "server_id": a.get("serverId") or (a.get("device") or {}).get("id"),
        "severity": a.get("severity"),
        "category": a.get("category"),
        "message": a.get("message") or a.get("description"),
        "resolution": a.get("resolution"),
        "created_at": a.get("createdAt"),
        "cleared": a.get("cleared"),
        "cleared_at": a.get("clearedAt"),
        "service_event": a.get("serviceEvent"),
        "message_id": a.get("messageId"),
    }


def job_row(j: dict[str, Any]) -> dict[str, Any]:
    res = j.get("resource") or {}
    return {
        "id": j.get("id"),
        "name": j.get("name"),
        "state": j.get("state"),
        "result": j.get("resultCode"),
        "status": j.get("status"),
        "resource": {"id": res.get("id"), "type": res.get("type")},
        "created_at": j.get("createdAt"),
        "updated_at": j.get("updatedAt"),
    }


def group_row(g: dict[str, Any]) -> dict[str, Any]:
    compliance = g.get("groupCompliance") or {}
    return {
        "id": g.get("id"),
        "name": g.get("name"),
        "description": g.get("description") or None,
        "device_type": g.get("deviceType"),
        "server_count": len(g.get("devices") or []),
        "servers": [d.get("serial") or d.get("id") for d in g.get("devices") or []],
        "compliance": compliance.get("summary") or g.get("complianceStatus"),
        "firmware_compliance": (compliance.get("firmware") or {}).get("status"),
        "ilo_settings_compliance": (compliance.get("iloSettings") or {}).get("status"),
        "settings_ids": [u.rsplit("/", 1)[-1] for u in g.get("settingsUris") or []],
        "auto_firmware_update_on_add": ((g.get("policies") or {}).get("onDeviceAdd") or {}).get("firmwareUpdate"),
    }


def bundle_row(b: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": b.get("id"),
        "name": b.get("displayName") or b.get("name"),
        "release_version": b.get("releaseVersion"),
        "release_date": (b.get("releaseDate") or "")[:10] or None,
        "type": b.get("bundleType"),
        "generation": b.get("bundleGeneration"),
        "active": b.get("isActive"),
        "summary": (b.get("summary") or "").strip() or None,
    }


# ── views used by the MCP tools ───────────────────────────────────────────────


def find_server(servers: list[dict[str, Any]], query: str) -> dict[str, Any]:
    for s in servers:
        hw = s.get("hardware") or {}
        bmc = hw.get("bmc") or {}
        if matches(query, s.get("id"), s.get("name"), hw.get("serialNumber"), bmc.get("hostname"), bmc.get("ip"), (s.get("host") or {}).get("hostname")):
            return s
    q = query.strip().lower()
    partial = [s for s in servers if q in (s.get("name") or "").lower()]
    if len(partial) == 1:
        return partial[0]
    hint = f" Partial matches: {', '.join(s.get('name') or s.get('id') for s in partial[:10])}" if partial else ""
    raise ComError(f"No COM server matches '{query}' (try name, serial number, iLO IP or id).{hint}")


async def overview(c: ComClient) -> dict[str, Any]:
    servers, alerts = await asyncio.gather(c.get_all("/servers"), c.get_all("/alerts", {"sort": "createdAt desc"}))
    rows = [server_row(s) for s in servers]
    active = [a for a in alerts if not a.get("cleared")]
    names = {s.get("id"): s.get("name") for s in servers}
    attention = [r for r in rows if (r["health"] or "").upper() not in ("OK", "")]
    return {
        "server_count": len(rows),
        "by_health": dict(Counter(r["health"] or "UNKNOWN" for r in rows)),
        "by_power_state": dict(Counter(r["power_state"] or "UNKNOWN" for r in rows)),
        "connected": sum(1 for r in rows if r["connected"]),
        "not_connected": [r["name"] for r in rows if not r["connected"]],
        "by_connection_type": dict(Counter(r["connection_type"] or "UNKNOWN" for r in rows)),
        "by_generation": dict(Counter(r["generation"] or "UNKNOWN" for r in rows)),
        "by_model": dict(Counter(r["model"] or "UNKNOWN" for r in rows)),
        "subscriptions_expiring": sorted(
            ({"name": r["name"], "expires_at": r["subscription"]["expires_at"]} for r in rows if r["subscription"]["expires_at"]),
            key=lambda x: x["expires_at"],
        )[:5],
        "needs_attention": [{"name": r["name"], "health": r["health"], "power_state": r["power_state"], "connected": r["connected"]} for r in attention],
        "active_alerts": {
            "count": len(active),
            "by_severity": dict(Counter(a.get("severity") or "UNKNOWN" for a in active)),
            "newest": [{**alert_row(a), "server": names.get(alert_row(a)["server_id"])} for a in active[:10]],
        },
    }


async def list_servers(c: ComClient, health: str | None, connected: bool | None, name_contains: str | None) -> list[dict[str, Any]]:
    rows = [server_row(s) for s in await c.get_all("/servers")]
    if health:
        rows = [r for r in rows if (r["health"] or "").upper() == health.upper()]
    if connected is not None:
        rows = [r for r in rows if bool(r["connected"]) == connected]
    if name_contains:
        rows = [r for r in rows if name_contains.lower() in (r["name"] or "").lower()]
    return rows


async def get_server(c: ComClient, query: str) -> dict[str, Any]:
    s = find_server(await c.get_all("/servers"), query)
    detail = server_detail(s)
    alerts = await c.get_all(f"/servers/{s['id']}/alerts", {"sort": "createdAt desc"}, max_items=200)
    detail["active_alerts"] = [alert_row(a) for a in alerts if not a.get("cleared")][:20]
    return detail


async def list_alerts(c: ComClient, server: str | None, severity: str | None, include_cleared: bool, limit: int) -> list[dict[str, Any]]:
    servers = await c.get_all("/servers")
    names = {s.get("id"): s.get("name") for s in servers}
    if server:
        sid = find_server(servers, server)["id"]
        alerts = await c.get_all(f"/servers/{sid}/alerts", {"sort": "createdAt desc"}, max_items=1000)
    else:
        alerts = await c.get_all("/alerts", {"sort": "createdAt desc"})
    out = []
    for a in alerts:
        if not include_cleared and a.get("cleared"):
            continue
        if severity and (a.get("severity") or "").upper() != severity.upper():
            continue
        row = alert_row(a)
        out.append({**row, "server": names.get(row["server_id"])})
        if len(out) >= limit:
            break
    return out


async def list_jobs(c: ComClient, limit: int, state: str | None) -> list[dict[str, Any]]:
    jobs = await c.get_all("/jobs", {"sort": "createdAt desc"}, max_items=limit if not state else 2000)
    rows = [job_row(j) for j in jobs]
    if state:
        rows = [r for r in rows if (r["state"] or "").upper() == state.upper()]
    return rows[:limit]


async def list_groups(c: ComClient) -> list[dict[str, Any]]:
    return [group_row(g) for g in await c.get_all("/groups")]


async def list_bundles(c: ComClient, include_inactive: bool) -> list[dict[str, Any]]:
    rows = [bundle_row(b) for b in await c.get_all("/firmware-bundles")]
    if not include_inactive:
        rows = [r for r in rows if r["active"]]
    return sorted(rows, key=lambda r: r["release_date"] or "", reverse=True)
