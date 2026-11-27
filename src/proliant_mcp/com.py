"""HPE Compute Ops Management (GreenLake) client and read-only views.

Auth is the GreenLake OAuth2 client-credentials flow (an API client created in
the GreenLake workspace), the same as proliant-cli's ``proliant.com.auth``.
Endpoints are the public ``/compute-ops-mgmt/v1`` API.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from typing import Any

import httpx

from proliant_mcp import fleet_table, store
from proliant_mcp.config import COM_REGIONS, ComAccount
from proliant_mcp.util import matches

TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"
GLOBAL_API = "https://global.api.greenlake.hpe.com"
API = "/compute-ops-mgmt/v1"
_TIMEOUT = httpx.Timeout(timeout=60.0, connect=10.0)
_PAGE = 500
REGIONS = ("us-west", "eu-central", "ap-northeast")

# client_id -> (token, monotonic expiry); tokens last ~2 h, reuse them across tool calls.
_tokens: dict[str, tuple[str, float]] = {}
# client_id -> region actually used, when the configured one has no COM for that workspace.
_regions: dict[str, str] = {}


class ComError(RuntimeError):
    status: int | None = None  # the HTTP status when COM answered with an error


class ComAuthError(ComError):
    """GreenLake refused the API client: a sign-in problem, not a problem with one server."""

    no_stale = True  # the assistant must sign the user in again, not answer from old data


def _routing_404(resp: httpx.Response) -> bool:
    """GreenLake's answer when COM is not set up in this region for the token's workspace."""
    if resp.status_code != 404:
        return False
    try:
        return "routing details" in str(resp.json().get("message", "")).lower()
    except ValueError:
        return False


def _http_error(resp: httpx.Response, what: str) -> ComError:
    if resp.status_code == 403:
        error = ComError(
            f"COM {what}: HTTP 403. The API client has no Compute Ops Management role in this "
            "workspace; ask the GreenLake administrator to assign one."
        )
    else:
        try:
            detail = resp.json().get("message") or resp.text[:200]
        except ValueError:
            detail = resp.text[:200]
        error = ComError(f"COM {what}: HTTP {resp.status_code} {detail}")
    error.status = resp.status_code
    return error


class ComClient:
    """``async with ComClient(account) as c: await c.get("/servers")``"""

    def __init__(self, account: ComAccount, transport: httpx.AsyncBaseTransport | None = None):
        self.account = account
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self.region = _regions.get(account.client_id, account.region)

    async def __aenter__(self) -> "ComClient":
        try:
            self.account.base_url  # noqa: B018 - validates the configured region
            base = COM_REGIONS[self.region]
        except (ValueError, KeyError) as exc:
            raise ComError(str(exc)) from exc
        # The token is fetched on the first request, so an answer served from the local cache never contacts GreenLake.
        self._http = httpx.AsyncClient(base_url=base, timeout=_TIMEOUT, transport=self._transport)
        return self

    async def _authorize(self) -> None:
        assert self._http is not None
        if "Authorization" not in self._http.headers:
            self._http.headers["Authorization"] = f"Bearer {await self._token()}"

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
        if resp.status_code >= 500:
            raise ComError(f"COM: the GreenLake sign-in service answered HTTP {resp.status_code}; try again in a moment.")
        if resp.status_code >= 400:
            raise ComAuthError(
                f"COM login failed for account '{self.account.name}' (HTTP {resp.status_code}): the GreenLake API client "
                f"from {self.account.source or 'the configuration'} was deleted or its secret is wrong. Sign the user in "
                "again now with com_login (pop-ups) or com_login_start (no pop-ups)."
            )
        data = resp.json()
        _tokens[self.account.client_id] = (data["access_token"], time.monotonic() + float(data.get("expires_in", 7200)))
        return data["access_token"]

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        await self._authorize()
        assert self._http is not None
        url = path if path.startswith("/compute-ops-mgmt/") else API + path
        for attempt in range(2):
            try:
                resp = await self._http.get(url, params=params)
            except httpx.TransportError as exc:
                raise ComError(f"COM GET {url} failed ({exc.__class__.__name__})") from exc
            if attempt == 0 and _routing_404(resp):
                await self._switch_region()
                continue
            break
        if resp.status_code == 401:
            _tokens.pop(self.account.client_id, None)
        if resp.status_code >= 400:
            raise _http_error(resp, f"GET {url}")
        return resp.json() if resp.content else {}

    async def get_global(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET on GreenLake's workspace-wide API (service catalog, workspace), same token as COM."""
        await self._authorize()
        assert self._http is not None
        try:
            resp = await self._http.get(GLOBAL_API + path, params=params)
        except httpx.TransportError as exc:
            raise ComError(f"GreenLake GET {path} failed ({exc.__class__.__name__})") from exc
        if resp.status_code in (401, 403):
            raise ComError(
                f"GreenLake GET {path}: HTTP {resp.status_code}. This API client may only be allowed to use "
                "Compute Ops Management. Sign the user in again with com_login (it creates a workspace-wide API client) "
                "or use an API client created for the whole workspace."
            )
        if resp.status_code >= 400:
            raise _http_error(resp, f"GET {path}")
        return resp.json() if resp.content else {}

    async def get_global_all(self, path: str, max_items: int = 1000) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while len(items) < max_items:
            page = await self.get_global(path, {"limit": 100, "offset": len(items)})
            batch = page.get("items") or []
            items.extend(batch)
            if not batch or len(items) >= int(page.get("total") or 0):
                break
        return items

    async def region_totals(self) -> dict[str, int | None]:
        """Server count per region for this login's workspace; None where COM is not set up."""
        await self._authorize()
        assert self._http is not None
        auth = {"Authorization": self._http.headers["Authorization"]}

        async def one(client: httpx.AsyncClient, region: str) -> tuple[str, int | None]:
            try:
                resp = await client.get(f"{COM_REGIONS[region]}{API}/servers", params={"limit": 1}, headers=auth)
            except httpx.TransportError:
                return region, None
            if resp.status_code == 200:
                return region, int(resp.json().get("total") or 0)
            return region, None

        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
            return dict(await asyncio.gather(*(one(client, r) for r in REGIONS)))

    async def _switch_region(self) -> None:
        """The configured region has no COM for this workspace: use the region that has the most servers."""
        assert self._http is not None
        totals = {r: n for r, n in (await self.region_totals()).items() if n is not None}
        if not totals:
            ws = f" '{self.account.workspace}'" if self.account.workspace else ""
            raise ComError(
                f"Compute Ops Management is not set up for this GreenLake workspace{ws} in any region "
                f"({', '.join(REGIONS)}). The COM login in use comes from {self.account.source or 'the configuration'}. "
                "Sign the user in again now with com_login (pop-ups) or com_login_start (no pop-ups) and choose a "
                "workspace that has Compute Ops Management."
            )
        best = max(totals, key=lambda r: (totals[r], r == self.account.region))
        _regions[self.account.client_id] = best
        self.region = best
        self._http.base_url = httpx.URL(COM_REGIONS[best])

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


def _section(inv: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return (inv.get(name) or {}).get("data") or []


def _ok(item: dict[str, Any]) -> bool:
    return ((item.get("Status") or {}).get("Health") or "OK").upper() in ("OK", "")


def _drive_groups(drives: list[dict[str, Any]]) -> list[str]:
    """'N x SIZE TYPE' per distinct capacity and type, largest group first (decimal GB, as drive vendors label)."""
    groups: Counter[tuple[int, str]] = Counter()
    for d in drives:
        size = round((d.get("CapacityBytes") or 0) / 1000**3)
        kind = "NVMe" if str(d.get("Protocol", "")).upper() == "NVME" else (d.get("MediaType") or "?")
        groups[(size, kind)] += 1
    return [f"{n} x {size}GB {kind}" if size else f"{n} x ? {kind}" for (size, kind), n in groups.most_common()]


def storage_summary(inv: dict[str, Any], detail: bool = False) -> dict[str, Any]:
    """Storage controllers and drives from the cached Redfish ``storage`` inventory.

    HPE's Storage Id tells the attachment (ported from proliant-cli's storage report):
    ``DE*`` is a real controller (Smart Array / MegaRAID RAID card, NS204i boot controller)
    with drives behind it; ``DA*`` is drives attached straight to the CPU/PCH, whose
    "controller" entry is only the drive's own NVMe chip. Other Ids (older iLO 5) count as a
    controller when they list one. ``smartStorage`` repeats the same controllers, so it is not used.
    """
    controllers: dict[str, dict[str, Any]] = {}
    direct: list[dict[str, Any]] = []
    for res in _section(inv, "storage"):
        drives = [d for d in res.get("Drives") or [] if (d.get("Status") or {}).get("State") != "Absent"]
        ctrl = (res.get("StorageControllers") or res.get("Controllers") or [{}])[0]
        sid = str(res.get("Id", "")).upper()
        is_controller = sid.startswith("DE") or (not sid.startswith("DA") and bool(ctrl))
        if not is_controller:
            direct.extend(drives)
            continue
        model = re.sub(r"^\s*HPE\s+", "", str(ctrl.get("Model") or ctrl.get("Name") or res.get("Name") or "?")).strip()
        entry = controllers.setdefault(model, {"model": model, "firmware": ctrl.get("FirmwareVersion"), "health": "OK", "_drives": []})
        if not _ok(ctrl) or not _ok(res):
            entry["health"] = "NOT OK"
        entry["_drives"].extend(drives)
    out_controllers = []
    sized = [d for c in controllers.values() for d in c["_drives"]] + direct
    total_gb = round(sum(d.get("CapacityBytes") or 0 for d in sized) / 1000**3)
    unsized = sum(1 for d in sized if not d.get("CapacityBytes"))
    for c in controllers.values():
        drives = c.pop("_drives")
        c["drives"] = _drive_groups(drives)
        c["drive_count"] = len(drives)
        if detail:
            c["drive_detail"] = [
                {"model": d.get("Model"), "serial": d.get("SerialNumber"), "health": (d.get("Status") or {}).get("Health")} for d in drives
            ]
        out_controllers.append(c)
    return {
        "controllers": out_controllers,
        "direct_drives": _drive_groups(direct),
        "drive_count": sum(c["drive_count"] for c in out_controllers) + len(direct),
        "total_gb": total_gb,
        "unsized_drives": unsized,
    }


_GENERIC_NIC_NAMES = {"", "network adapter", "networkadapter"}


def _nic_label(a: dict[str, Any]) -> str:
    """The most descriptive of Model / SKU / Name: iLOs put the marketing name in different fields
    ('BCM57414' + SKU '10/25Gb 2-port SFP28 BCM57414 OCP3 Adapter'; Mellanox 'P42046-001 / Mellanox ...')."""
    names = [re.sub(r"^\s*P\d{5}-\d{3}\s*/\s*", "", str(a.get(k) or "")).strip() for k in ("Model", "SKU", "Name")]
    useful = [n for n in names if n.lower() not in _GENERIC_NIC_NAMES and not re.fullmatch(r"[A-Z0-9#-]{4,20}", n)]
    return max(useful, key=len) if useful else next((n for n in names if n), "?")


def _gbps(value: Any) -> str:
    return f"{value:g}Gb" if isinstance(value, (int, float)) else "unknown speed"


def nic_summary(inv: dict[str, Any], detail: bool = False) -> dict[str, Any]:
    """Network adapters from the cached Redfish ``networkAdapter`` inventory. The mode of a port is
    its ``LinkNetworkTechnology`` (Ethernet, InfiniBand or FibreChannel); NVIDIA/Mellanox VPI cards can
    run either Ethernet or InfiniBand, so it is the configured mode, not just the card type."""
    adapters = []
    for a in _section(inv, "networkAdapter"):
        ports = a.get("Ports") or []
        label = _nic_label(a)
        if label == "?" and not ports:
            continue
        modes: Counter[str] = Counter(str(p.get("LinkNetworkTechnology") or "unknown") for p in ports)
        up = [p for p in ports if str(p.get("LinkStatus") or "").lower() == "linkup"]
        mixed = len(modes) > 1
        ctrl = (a.get("Controllers") or [{}])[0]
        entry: dict[str, Any] = {
            "name": label,
            "manufacturer": (a.get("Manufacturer") or "").strip() or None,
            "part_number": (a.get("PartNumber") or "").strip() or None,
            "mode": "+".join(sorted(modes)) if modes else None,
            "ports": len(ports),
            "ports_up": len(up),
            # a VPI card can run one port as Ethernet and the other as InfiniBand: name each port's mode then
            "speeds_up": [
                f"{n} x {s}"
                for s, n in Counter(
                    _gbps(p.get("CurrentSpeedGbps")) + (f" {p.get('LinkNetworkTechnology') or 'unknown'}" if mixed else "") for p in up
                ).most_common()
            ],
            "firmware": ctrl.get("FirmwarePackageVersion"),
            "health": "OK" if _ok(a) else "NOT OK",
        }
        if mixed:
            entry["port_modes"] = {
                m: {"ports": n, "ports_up": sum(1 for p in up if str(p.get("LinkNetworkTechnology") or "unknown") == m)} for m, n in modes.items()
            }
        if detail:
            entry["serial_number"] = a.get("SerialNumber")
            entry["port_detail"] = [
                {
                    "port": p.get("PortId") or p.get("Id"),
                    "mode": p.get("LinkNetworkTechnology"),
                    "link": p.get("LinkStatus"),
                    "speed_gbps": p.get("CurrentSpeedGbps"),
                    "max_speed_gbps": p.get("MaxSpeedGbps"),
                    "mac": ((p.get("Ethernet") or {}).get("AssociatedMACAddresses") or [None])[0],
                }
                for p in ports
            ]
        adapters.append(entry)
    return {"known": bool(_section(inv, "networkAdapter")), "adapters": adapters}


# iLO names some GPUs generically ("Intel Video Controller"); the PCI vendor:device ID tells which.
_GPU_PCI_NAMES = {
    ("0X8086", "0X0BD5"): "Intel Data Center GPU Max 1550",
    ("0X8086", "0X0BD6"): "Intel Data Center GPU Max 1550",
    ("0X8086", "0X0BD7"): "Intel Data Center GPU Max 1350",
    ("0X8086", "0X0BD8"): "Intel Data Center GPU Max 1350",
    ("0X8086", "0X0BD9"): "Intel Data Center GPU Max 1100",
    ("0X8086", "0X0BDA"): "Intel Data Center GPU Max 1100",
    ("0X8086", "0X0BDB"): "Intel Data Center GPU Max 1100",
    ("0X8086", "0X56C0"): "Intel Data Center GPU Flex 170",
    ("0X8086", "0X56C1"): "Intel Data Center GPU Flex 140",
}


def _pci_gpu_names(pci: list[dict[str, Any]]) -> dict[str, str]:
    """Generic iLO device name -> real GPU model, when every such PCI device has the same known ID."""
    found: dict[str, set[str]] = {}
    for dev in pci:
        for f in dev.get("PCIeFunctions") or []:
            key = (str(f.get("VendorId", "")).upper(), str(f.get("DeviceId", "")).upper())
            found.setdefault((dev.get("Name") or "").strip(), set()).add(_GPU_PCI_NAMES.get(key, ""))
    return {name: next(iter(models)) for name, models in found.items() if name and len(models) == 1 and "" not in models}


def _dimm_vendor(item: dict[str, Any]) -> str:
    """One name per memory maker: iLO says "Hynix" in Manufacturer and "SK Hynix" in the HPE VendorName."""
    raw = (((item.get("Oem") or {}).get("Hpe") or {}).get("VendorName") or item.get("Manufacturer") or "").strip()
    low = raw.lower()
    for key, name in (("hynix", "SK Hynix"), ("samsung", "Samsung"), ("micron", "Micron"), ("kingston", "Kingston")):
        if key in low:
            return name
    return raw or "Unknown"


def memory_summary(inv: dict[str, Any]) -> dict[str, Any]:
    """Installed DIMMs from a server's COM inventory, grouped by maker, size, type and speed (empty slots are skipped)."""
    items = _section(inv, "memory")
    groups: Counter[tuple[str, int, str, Any, str]] = Counter()
    for m in items:
        size = int(m.get("CapacityMiB") or 0)
        if (m.get("Status") or {}).get("State") == "Absent" or not size:
            continue
        hpe = (m.get("Oem") or {}).get("Hpe") or {}
        groups[(
            _dimm_vendor(m), size // 1024, " ".join(filter(None, [m.get("MemoryDeviceType") or m.get("MemoryType"), hpe.get("BaseModuleType")])),
            m.get("OperatingSpeedMhz") or hpe.get("MaxOperatingSpeedMTs"), (m.get("PartNumber") or "").strip(),
        )] += 1
    modules = [
        {"manufacturer": maker, "size_gib": gib, "type": kind or None, "speed_mts": speed, "part_number": part or None, "count": n}
        for (maker, gib, kind, speed, part), n in sorted(groups.items(), key=lambda kv: (-kv[1], kv[0][0]))
    ]
    return {"known": bool(items), "dimms": sum(g["count"] for g in modules), "total_gib": sum(g["count"] * g["size_gib"] for g in modules), "modules": modules}


def hardware_summary(inv: dict[str, Any], detail: bool = False, network: bool = False) -> dict[str, Any]:
    """CPUs and GPUs from a server's cached COM inventory (``/servers/{id}/inventory``).

    GPUs are the ``processor`` entries iLO reports with ``ProcessorType: GPU``. Older iLOs
    may only list them as PCIe display controllers, which is the fallback. ``detail`` adds
    serial numbers; ``network`` adds the network adapters.
    """
    processors, pci = _section(inv, "processor"), _section(inv, "pciDevice")
    cpus = [p for p in processors if str(p.get("ProcessorType", "")).upper() == "CPU"]
    gpu_items = [p for p in processors if str(p.get("ProcessorType", "")).upper() == "GPU"]
    source = "processor"
    if not gpu_items:
        source = "pci"
        for dev in pci:
            functions = dev.get("PCIeFunctions") or [{}]
            display = any(f.get("DeviceClass") == "DisplayController" or str(f.get("ClassCode", "")).upper().startswith("0X03") for f in functions)
            if display and "embedded" not in (dev.get("Name") or "").lower():
                gpu_items.append(dev)

    cpu_groups = Counter((" ".join((c.get("Model") or c.get("Name") or "").split()), c.get("TotalCores"), c.get("TotalThreads")) for c in cpus)
    gpu_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    real_names = _pci_gpu_names(pci)
    for g in gpu_items:
        name = (g.get("Name") or g.get("Model") or "").strip()
        key = (real_names.get(name, name), (g.get("Manufacturer") or "").strip(), (g.get("PartNumber") or g.get("Model") or "").strip())
        gpu_groups.setdefault(key, []).append(g)
    gpus = []
    for (name, vendor, part), items in gpu_groups.items():
        entry: dict[str, Any] = {
            "name": name,
            "manufacturer": vendor or None,
            "part_number": part if part and part != name else None,
            "count": len(items),
            "health": "OK" if all(_ok(i) for i in items) else "NOT OK",
        }
        if detail:
            entry["serial_numbers"] = [i.get("SerialNumber") for i in items if i.get("SerialNumber")]
        gpus.append(entry)
    storage_known = bool(_section(inv, "storage") or _section(inv, "smartStorage"))
    summary = {
        "inventory_available": bool(processors or pci or storage_known),
        # COM can hold firmware and storage for a server whose full inventory (CPU, PCI) was never collected.
        "cpu_gpu_known": bool(processors or pci),
        "inventory_collected_at": inv.get("lastFullInventoryCollectionAt"),
        "cpu": [{"model": m, "count": n, "cores_each": cores, "threads_each": threads} for (m, cores, threads), n in cpu_groups.items()],
        "gpu_count": len(gpu_items),
        "gpus": gpus,
        "gpu_source": source if gpu_items else None,
        "storage": {**storage_summary(inv, detail), "known": storage_known},
        "memory": memory_summary(inv),
    }
    if network:
        summary["network"] = nic_summary(inv, detail)
    return summary


def _scope(c: ComClient) -> tuple[str, str]:
    """Cache partition for one GreenLake login (API client and workspace)."""
    return (c.account.client_id, c.account.workspace or "")


async def server_rows(c: ComClient, cached: bool = False, state: bool = False) -> list[dict[str, Any]]:
    """COM's server list as ``server_row`` dicts; ``cached`` reads it through the local store.

    The rows hold both inventory (name, model, serial) and state (health, power). ``state`` asks for the state
    lifetime, for health and power questions; inventory questions accept the longer inventory lifetime of the
    same entry."""

    async def fetch() -> list[dict[str, Any]]:
        return [server_row(s) for s in await c.get_all("/servers")]

    if not cached:
        return await fetch()
    return await store.fetch_one("com_servers", _scope(c), fetch, (ComError,), state=state)


async def _hardware_for(c: ComClient, ids: list[str], workers: int = 8, network: bool = False) -> dict[str, dict[str, Any]]:
    """Hardware summary per server id; a server whose inventory cannot be read gets an error entry.

    Inside a ``store.session`` fresh summaries come from the local cache and only the rest are read from COM."""
    gate = asyncio.Semaphore(workers)

    async def fetch(sid: str) -> dict[str, Any]:
        async with gate:
            try:
                return hardware_summary(await c.get(f"/servers/{sid}/inventory"), network=True)
            except ComError as exc:
                # COM's definite answer for this server (no inventory, or not readable with this login): worth caching,
                # unlike a timeout or a 5xx, which are tried live again next time.
                if exc.status in (403, 404):
                    return {"inventory_available": False, "inventory_error": str(exc)}
                raise

    got = await store.fetch_many("com_hw2", _scope(c), ids, fetch, (ComError,))
    auth = next((v for v in got.values() if isinstance(v, ComAuthError)), None)
    if auth is not None:
        raise auth  # a sign-in problem applies to every server: report it once
    out: dict[str, dict[str, Any]] = {}
    for sid in ids:
        value = got[sid]
        if isinstance(value, BaseException):
            out[sid] = {"inventory_available": False, "inventory_error": str(value)}
        else:
            out[sid] = value if network else {k: v for k, v in value.items() if k != "network"}
    return out


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


def _generation_number(value: str | None) -> str:
    """'GEN_11', 'Gen11', 'gen 11' and '11' all mean 11."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


async def list_servers(
    c: ComClient,
    health: str | None,
    connected: bool | None,
    name_contains: str | None,
    include_hardware: bool = False,
    network: bool = False,
    generation: str | None = None,
) -> list[dict[str, Any]]:
    rows = await server_rows(c, cached=True, state=not include_hardware)
    if health:
        rows = [r for r in rows if (r["health"] or "").upper() == health.upper()]
    if connected is not None:
        rows = [r for r in rows if bool(r["connected"]) == connected]
    if generation:
        rows = [r for r in rows if _generation_number(r["generation"]) == _generation_number(generation)]
    if name_contains:
        rows = [r for r in rows if name_contains.lower() in (r["name"] or "").lower()]
    if include_hardware:
        hardware = await _hardware_for(c, [r["id"] for r in rows], network=network)
        rows = [{**r, "hardware": hardware[r["id"]]} for r in rows]
    return rows


async def list_gpus(
    c: ComClient, name_contains: str | None, connected: bool | None, include_without_gpus: bool, gpu_model: str | None = None,
) -> dict[str, Any]:
    """Fleet GPU report: one row per server with its model, iLO firmware, CPU and GPUs.

    ``gpu_model`` keeps only GPUs whose name or vendor contains the text (any case), so only servers with
    such a GPU are listed and the totals count only those GPUs."""
    rows = await list_servers(c, None, connected, name_contains, include_hardware=True)
    wanted = (gpu_model or "").strip().lower()
    totals: Counter[str] = Counter()
    report, unavailable, with_gpus = [], [], 0
    for r in rows:
        hw = r["hardware"]
        if not hw.get("cpu_gpu_known"):
            unavailable.append(r["name"])
        gpus = [
            g for g in hw.get("gpus", [])
            if not wanted or wanted in f"{g.get('manufacturer') or ''} {g['name']}".lower()
        ]
        gpu_count = sum(g["count"] for g in gpus) if wanted else hw.get("gpu_count", 0)
        with_gpus += bool(gpu_count)
        for g in gpus:
            vendor = g["manufacturer"]
            label = f"{vendor} {g['name']}" if vendor and vendor.lower() not in g["name"].lower() else g["name"]
            totals[label] += g["count"]
        if gpu_count or (include_without_gpus and not wanted):
            report.append({
                "name": r["name"],
                "serial_number": r["serial_number"],
                "model": r["model"],
                "generation": r["generation"],
                "ilo_version": r["ilo"]["version"],
                "health": r["health"],
                "power_state": r["power_state"],
                "connected": r["connected"],
                "cpu": hw.get("cpu"),
                "gpu_count": gpu_count,
                "gpus": gpus,
                "inventory_collected_at": hw.get("inventory_collected_at"),
            })
    return {
        "servers_checked": len(rows),
        "servers_with_gpus": with_gpus,
        "total_gpus": sum(totals.values()),
        "gpus_by_model": dict(totals),
        "inventory_unavailable": unavailable,
        "note": "Data is COM's cached inventory. Servers in inventory_unavailable have no cached CPU/PCI inventory "
        "(full inventory never collected, or not connected to COM), so their GPUs are unknown, not absent.",
        "servers": report,
    }


def _module_text(m: dict[str, Any]) -> str:
    detail = " ".join(str(x) for x in (m["type"], f"{m['speed_mts']} MT/s" if m["speed_mts"] else None) if x)
    return f"{m['count']} x {m['size_gib']}GB {detail}".strip() + (f" ({m['part_number']})" if m["part_number"] else "")


async def list_memory(
    c: ComClient, manufacturer: str | None, name_contains: str | None, connected: bool | None, generation: str | None = None,
) -> dict[str, Any]:
    """Fleet DIMM report from COM's cached inventory. Without ``manufacturer``: the DIMM makers with their server and DIMM
    counts. With it (any case, e.g. Hynix): only the servers that have such DIMMs, one table row each."""
    rows = await list_servers(c, None, connected, name_contains, include_hardware=True, generation=generation)
    wanted = (manufacturer or "").strip().lower()
    known = [r for r in rows if (r["hardware"].get("memory") or {}).get("known")]
    unavailable = [r["name"] for r in rows if r not in known]
    by_maker: dict[str, dict[str, Any]] = {}
    matches: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for r in known:
        mods = [m for m in r["hardware"]["memory"]["modules"] if not wanted or wanted in m["manufacturer"].lower()]
        if not mods:
            continue
        matches.append((r, mods))
        for maker in {m["manufacturer"] for m in mods}:
            entry = by_maker.setdefault(maker, {"servers": 0, "dimms": 0, "names": []})
            entry["servers"] += 1
            entry["names"].append(r["name"])
            entry["dimms"] += sum(m["count"] for m in mods if m["manufacturer"] == maker)
    ordered = sorted(by_maker.items(), key=lambda kv: (-kv[1]["servers"], kv[0]))
    dimms = sum(m["count"] for _, mods in matches for m in mods)
    tail = f" ({len(unavailable)} more servers have no memory data)" if unavailable else ""
    if not matches:
        table = f"No server with memory data has {manufacturer or 'any'} DIMMs{tail}."
    elif wanted:
        lines = [f"{len(matches)} of {len(known)} servers with memory data have {manufacturer} DIMMs ({dimms} DIMMs){tail}:", "",
                 "| Server | DIMMs | Modules |", "|---|---|---|"]
        for r, mods in matches:
            lines.append(f"| {fleet_table._cell(r['name'])} | {sum(m['count'] for m in mods)} | {fleet_table._cell('; '.join(_module_text(m) for m in mods))} |")
        table = "\n".join(lines)
    else:
        grouped, _, _ = fleet_table.grouped_table([(k, v["names"]) for k, v in ordered], None, "DIMM maker", "Servers")
        table = f"{len(known)} servers with memory data{tail}:\n\n{grouped}"
    return {
        "counts": {
            "servers_checked": len(rows), "servers_with_memory_data": len(known), "servers_without_memory_data": len(unavailable),
            "servers_with_match": len(matches), "dimms_matching": dimms,
            "by_maker": {k: {"servers": v["servers"], "dimms": v["dimms"]} for k, v in ordered},
        },
        "table": table,
        "inventory_unavailable": unavailable,
        "note": "Show `table` as written and quote `counts`: they are exact. Data is COM's cached inventory; servers in "
        "inventory_unavailable have no memory data (unknown, not absent). Hynix and SK Hynix are the same maker.",
    }


def _mode_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Adapters, ports and linked ports per mode (Ethernet / InfiniBand / FibreChannel), most used first.
    Ports count by their own mode; a VPI card running both modes counts as an adapter in each."""
    modes: dict[str, dict[str, int]] = {}
    for r in rows:
        for a in ((r["hardware"].get("network") or {}).get("adapters") or []):
            per_mode = a.get("port_modes") or {a["mode"] or "unknown": {"ports": a["ports"], "ports_up": a["ports_up"]}}
            for mode, counts in per_mode.items():
                m = modes.setdefault(mode, {"adapters": 0, "ports": 0, "ports_up": 0})
                m["adapters"] += 1
                m["ports"] += counts["ports"]
                m["ports_up"] += counts["ports_up"]
    return dict(sorted(modes.items(), key=lambda kv: (-kv[1]["ports_up"], -kv[1]["adapters"])))


def match_storage_controller(rows: list[dict[str, Any]], text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep the servers (rows with ``hardware``) that have a storage controller whose model contains *text*.

    Case, spaces and dashes are ignored, so ``ns204``, ``NS 204`` and ``NS-204i`` all match "NS204i-u Gen11 Boot
    Controller". Servers COM has no storage inventory for cannot be judged: they are counted as unknown, not as
    non-matching."""
    wanted = re.sub(r"[^a-z0-9]", "", text.lower())
    matched, unknown = [], []
    for r in rows:
        storage = (r.get("hardware") or {}).get("storage") or {}
        if not storage.get("known"):
            unknown.append(r["name"])
        elif any(wanted in re.sub(r"[^a-z0-9]", "", (c.get("model") or "").lower()) for c in storage.get("controllers") or []):
            matched.append(r)
    summary = {
        "controller": text,
        "servers_with": len(matched),
        "servers_without": len(rows) - len(matched) - len(unknown),
        "servers_unknown": len(unknown),
        "unknown_servers": unknown,
    }
    return matched, summary


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _network_known(hw: dict[str, Any]) -> bool:
    return bool((hw.get("network") or {}).get("known"))


def _storage_known(hw: dict[str, Any]) -> bool:
    return bool((hw.get("storage") or {}).get("known"))


def _cpu_gpu_known(hw: dict[str, Any]) -> bool:
    return bool(hw.get("cpu_gpu_known"))


def _memory_known(hw: dict[str, Any]) -> bool:
    return bool((hw.get("memory") or {}).get("known"))


def _adapter_modes(a: dict[str, Any]) -> dict[str, dict[str, int]]:
    return a.get("port_modes") or {a["mode"] or "unknown": {"ports": a["ports"], "ports_up": a["ports_up"]}}


def _adapter_matches(a: dict[str, Any], ports: int | None, mode: str | None, linked: bool) -> bool:
    """An adapter with exactly *ports* ports and/or a port in *mode* (substring, any case); with *linked* it needs a link up
    (in that mode, when one is given)."""
    if ports is not None and a["ports"] != ports:
        return False
    modes = {m: c for m, c in _adapter_modes(a).items() if not mode or mode.lower() in m.lower()}
    return bool(modes) and (not linked or any(c["ports_up"] for c in modes.values()))


# filter -> (is the inventory known for a server, does the server match). Servers whose inventory is unknown are
# counted as unknown, never as non-matching. The third argument is linked_only (only the NIC filters use it).
HARDWARE_FILTERS: dict[str, tuple[Any, Any]] = {
    "gpu_model": (_cpu_gpu_known, lambda hw, text, linked: any(text in f"{g.get('manufacturer') or ''} {g['name']}".lower() for g in hw.get("gpus") or [])),
    "cpu_contains": (_cpu_gpu_known, lambda hw, text, linked: any(text in (c["model"] or "").lower() for c in hw.get("cpu") or [])),
    "nic_ports": (_network_known, lambda hw, ports, linked: any(_adapter_matches(a, ports, None, linked) for a in hw["network"].get("adapters") or [])),
    "nic_mode": (_network_known, lambda hw, mode, linked: any(_adapter_matches(a, None, mode, linked) for a in hw["network"].get("adapters") or [])),
    "dimm_maker": (_memory_known, lambda hw, text, linked: any(_norm(text) in _norm(m["manufacturer"]) for m in hw["memory"].get("modules") or [])),
    "direct_attached": (_storage_known, lambda hw, want, linked: bool(hw["storage"].get("direct_drives")) == want),
    "storage_over_tb": (_storage_known, lambda hw, tb, linked: (hw["storage"].get("total_gb") or 0) > tb * 1000),
}


def filter_hardware(
    rows: list[dict[str, Any]], filters: dict[str, Any], linked_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Keep the servers (rows with ``hardware``) that satisfy every given filter; one summary per filter with exact counts."""
    summaries: dict[str, dict[str, Any]] = {}
    for name, value in filters.items():
        if value is None:
            continue
        known, matches_filter = HARDWARE_FILTERS[name]
        needle = value.lower() if name in ("gpu_model", "cpu_contains") else value
        kept: list[dict[str, Any]] = []
        unknown: list[str] = []
        for r in rows:
            hw = r.get("hardware") or {}
            if not known(hw):
                unknown.append(r["name"])
            elif matches_filter(hw, needle, linked_only):
                kept.append(r)
        summaries[name] = {
            "value": value, "servers_with": len(kept), "servers_without": len(rows) - len(kept) - len(unknown),
            "servers_unknown": len(unknown), "unknown_servers": unknown,
        }
        rows = kept
    return rows, summaries


GROUP_BY = ("nic", "gpu", "memory", "cpu", "storage_controller", "ilo_version", "model", "subscription")


def _group_labels(r: dict[str, Any], key: str, filters: dict[str, Any], linked_only: bool) -> list[str] | None:
    """The groups one server belongs to for *key* (a server can be in several), or None when COM has no data for it."""
    hw = r.get("hardware") or {}
    if key == "model":
        return [fleet_table.COLUMNS["model"][1](r) or "unknown model"]
    if key == "ilo_version":
        return [(r.get("ilo") or {}).get("version") or "unknown"]
    if key == "subscription":
        return [fleet_table._subscription(r) or "no subscription"]
    if key == "nic":
        if not _network_known(hw):
            return None
        adapters = [a for a in hw["network"]["adapters"] if _adapter_matches(a, filters.get("nic_ports"), filters.get("nic_mode"), linked_only)]
        return [f"{a['name']} ({a['ports']}p {a['mode'] or 'unknown mode'})" for a in adapters]
    if key == "gpu":
        if not _cpu_gpu_known(hw):
            return None
        text = (filters.get("gpu_model") or "").lower()
        return [f"{g['count']} x {g['name']}" for g in hw.get("gpus") or [] if text in f"{g.get('manufacturer') or ''} {g['name']}".lower()]
    if key == "cpu":
        if not _cpu_gpu_known(hw):
            return None
        return [f"{c['model']}" for c in hw.get("cpu") or []]
    if key == "memory":
        if not _memory_known(hw):
            return None
        wanted = _norm(filters.get("dimm_maker") or "")
        return list(dict.fromkeys(f"{m['manufacturer']} {m['size_gib']}GB {m['type'] or ''}".strip() for m in hw["memory"].get("modules") or [] if wanted in _norm(m["manufacturer"])))
    if key == "storage_controller":
        if not _storage_known(hw):
            return None
        found = [c["model"] for c in hw["storage"].get("controllers") or []]
        return found or (["direct-attached drives (no controller)"] if hw["storage"].get("direct_drives") else ["no controller"])
    raise ValueError(key)


def group_servers(rows: list[dict[str, Any]], key: str, filters: dict[str, Any], linked_only: bool) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """Servers grouped by one hardware aspect, biggest group first, plus the servers COM has no data for."""
    groups: dict[str, list[str]] = {}
    unknown: list[str] = []
    for r in rows:
        labels = _group_labels(r, key, filters, linked_only)
        if labels is None:
            unknown.append(r["name"])
        for label in dict.fromkeys(labels or []):
            groups.setdefault(label, []).append(r["name"])
    return sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])), unknown


def fleet_totals(rows: list[dict[str, Any]], aspects: set[str]) -> dict[str, Any]:
    """Exact fleet-wide totals (all servers in *rows*) for the hardware a question is about, so the model never counts rows."""
    totals: dict[str, Any] = {}
    if "gpu" in aspects:
        known = [r for r in rows if _cpu_gpu_known(r["hardware"])]
        by_model: Counter[str] = Counter()
        for r in known:
            for g in r["hardware"].get("gpus") or []:
                by_model[g["name"]] += g["count"]
        totals["gpus"] = {
            "servers_with_gpus": sum(1 for r in known if r["hardware"].get("gpu_count")), "servers_with_data": len(known),
            "total_gpus": sum(by_model.values()), "by_model": dict(by_model.most_common()),
        }
    if "nic" in aspects:
        totals["nic_modes"] = _mode_totals([r for r in rows if _network_known(r["hardware"])])
    return totals


async def hardware_report(
    c: ComClient, name_contains: str | None, connected: bool | None, storage_over_tb: float | None = None, generation: str | None = None,
    storage_controller: str | None = None,
) -> dict[str, Any]:
    """Fleet hardware table in one compact answer: servers with identical hardware share one row.

    ``storage_over_tb`` keeps only servers whose raw drive capacity (RAID, boot and direct-attached
    drives together, decimal TB) is strictly greater, and counts matching, smaller and unknown servers.
    ``storage_controller`` keeps only servers with a controller whose model contains the text (see
    ``match_storage_controller``); ``servers_checked`` then counts only those."""
    rows = await list_servers(c, None, connected, name_contains, include_hardware=True, network=True, generation=generation)
    controller_filter: dict[str, Any] = {}
    if storage_controller:
        rows, summary = match_storage_controller(rows, storage_controller)
        controller_filter = {"storage_controller_filter": summary}
    groups: dict[str, dict[str, Any]] = {}
    storage_unknown: list[str] = []
    gpu_unknown: list[str] = []
    nic_unknown: list[str] = []
    for r in rows:
        hw = r["hardware"]
        st = hw.get("storage") or {}
        net = hw.get("network") or {}
        storage_known, gpu_known, nic_known = bool(st.get("known")), bool(hw.get("cpu_gpu_known")), bool(net.get("known"))
        if not storage_known:
            storage_unknown.append(r["name"])
        if not gpu_known:
            gpu_unknown.append(r["name"])
        if not nic_known:
            nic_unknown.append(r["name"])
        nics: Counter[str] = Counter(f"{a['name']} ({a['mode'] or 'unknown mode'}, {a['ports']}p)" for a in net.get("adapters", []))
        config = {
            "model": r["model"],
            "cpu": [f"{c['count']} x {c['model']}" for c in hw.get("cpu", [])] if gpu_known else None,
            "total_storage_gb": st.get("total_gb") if storage_known else None,
            **({"drives_without_size": st["unsized_drives"]} if storage_known and st.get("unsized_drives") else {}),
            "storage_controllers": [
                {"model": c["model"], "drives": c["drives"] or ["no drives"], **({"health": c["health"]} if c["health"] != "OK" else {})}
                for c in st.get("controllers", [])
            ] if storage_known else None,
            "direct_attached_drives": st.get("direct_drives", []) if storage_known else None,
            "gpus": [f"{g['count']} x {g['name']}" for g in hw.get("gpus", [])] if gpu_known else None,
            "nics": [f"{n} x {label}" for label, n in nics.most_common()] if nic_known else None,
        }
        entry = groups.setdefault(json.dumps(config, sort_keys=True), {"servers": [], **config})
        entry["servers"].append(r["name"])
    configs = sorted(groups.values(), key=lambda g: (g["model"] or "~", g["servers"][0]))
    capacity: dict[str, Any] = {}
    if storage_over_tb is not None:
        limit = storage_over_tb * 1000
        over = [g for g in configs if g["total_storage_gb"] is not None and g["total_storage_gb"] > limit]
        capacity = {
            "storage_filter": {
                "over_tb": storage_over_tb,
                "servers_over": sum(len(g["servers"]) for g in over),
                "servers_not_over": sum(len(g["servers"]) for g in configs if g["total_storage_gb"] is not None and g["total_storage_gb"] <= limit),
                "servers_unknown": len(storage_unknown),
            }
        }
        configs = over
    modes = _mode_totals(rows)
    return {
        "servers_checked": len(rows),
        **controller_filter,
        **capacity,
        "configurations": [{"count": len(g["servers"]), **g} for g in configs],
        "nic_modes": modes,
        "most_used_nic_mode": next(iter(modes), None),
        "no_inventory": [n for n in dict.fromkeys(storage_unknown) if n in set(gpu_unknown) and n in set(nic_unknown)],
        "storage_unknown": storage_unknown,
        "gpu_unknown": gpu_unknown,
        "nic_unknown": nic_unknown,
        "note": "Servers with identical hardware share one configuration. null means COM has no cached data for that part "
        "(unknown, not absent; no_inventory lists servers COM has no data for at all); an empty list means none. Drive sizes are decimal GB; total_storage_gb is the raw capacity of "
        "all drives (boot, RAID and direct-attached), not usable RAID space. NIC mode is the port's network "
        "technology (Ethernet, InfiniBand, FibreChannel); nic_modes counts adapters, ports and linked ports per mode.",
    }


def _matching_adapters(rows: list[dict[str, Any]], ports: int, linked_only: bool) -> tuple[list[tuple[dict[str, Any], list[dict[str, Any]]]], int]:
    """Servers with an adapter of exactly *ports* ports (and a link up when *linked_only*), plus how many servers have NIC data."""
    known = [r for r in rows if (r["hardware"].get("network") or {}).get("known")]
    hits = []
    for r in known:
        found = [a for a in r["hardware"]["network"]["adapters"] if a["ports"] == ports and (a["ports_up"] or not linked_only)]
        if found:
            hits.append((r, found))
    return hits, len(known)


SAMPLE_PER_MODEL = 3  # names shown per adapter model when the list is long


def _ports_summary(rows: list[dict[str, Any]], ports: int, linked_only: bool) -> dict[str, Any]:
    """Short answer for "which servers have N-port NICs": counts and servers grouped by adapter model.

    A model writes what it shows at roughly 200 tokens a second, so repeating the adapter name on every row
    of a table is the slow part. Up to COMPACT_ABOVE adapters every server is named; above that each model
    shows a few names and the rest sit in ``remaining`` for when the user asks for the full list."""
    hits, with_data = _matching_adapters(rows, ports, linked_only)
    groups: dict[str, list[str]] = {}
    for r, found in hits:
        for a in found:
            groups.setdefault(a["name"], []).append(f"{r['name']} ({a['ports_up']}/{a['ports']})")
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    adapters = sum(len(names) for _, names in ordered)
    without = len(rows) - with_data
    what = f"a {ports}-port adapter" + (" with a link up" if linked_only else "")
    tail = f" ({without} more servers have no NIC data)" if without else ""
    sample = adapters > fleet_table.COMPACT_ABOVE
    shown = SAMPLE_PER_MODEL if sample else None
    lines = [f"{len(hits)} of {with_data} servers with NIC data have {what}{tail}:" if hits
             else f"No server with NIC data has {what}{tail}."]
    remaining, hidden_total = {}, 0
    if hits:
        grouped, remaining, hidden_total = fleet_table.grouped_table(ordered, shown, "Adapter", "Servers (ports up/total)")
        lines += ["", grouped]
    note = (
        "Show `table` to the user exactly as written. The counts are exact (computed by the server): quote them, do not recount. "
        "Servers without network data are unknown, not absent. Ports up is linked/total ports."
    )
    if sample:
        note += (
            f" Then tell the user {hidden_total} more servers match and offer the full list. Do NOT show `remaining` unless the user "
            "asks for the full list; then show every server per adapter model (`table` names plus `remaining`) with no new tool call."
        )
    else:
        note += " For each adapter's firmware, link speeds and health call again without ports."
    return {
        "filter": {"ports_per_adapter": ports, "linked_only": linked_only},
        "total": len(hits),
        "counts": {
            "servers_checked": len(rows),
            "servers_with_nic_data": with_data,
            "servers_without_nic_data": without,
            "servers_with_match": len(hits),
            "adapters_matching": adapters,
            "by_adapter": {name: len(names) for name, names in ordered},
        },
        "layout": "sample" if sample else "all",
        "table": "\n".join(lines),
        **({"remaining": remaining} if remaining else {}),
        "note": note,
    }


async def list_nics(
    c: ComClient, name_contains: str | None, connected: bool | None, linked_only: bool, generation: str | None = None,
    ports: int | None = None,
) -> dict[str, Any]:
    """Fleet network adapter report: per server its adapters with mode, ports and link speeds, plus totals.

    ``ports`` keeps only adapters with exactly that many ports and answers with the short grouped summary of
    ``_ports_summary`` instead of the full report."""
    rows = await list_servers(c, None, connected, name_contains, include_hardware=True, network=True, generation=generation)
    if ports is not None:
        return _ports_summary(rows, ports, linked_only)
    by_model: dict[str, dict[str, Any]] = {}
    speeds: Counter[str] = Counter()
    report, unknown = [], []
    for r in rows:
        net = r["hardware"].get("network") or {}
        if not net.get("known"):
            unknown.append(r["name"])
            continue
        adapters = [a for a in net["adapters"] if a["ports_up"] or not linked_only]
        for a in net["adapters"]:
            m = by_model.setdefault(f"{a['name']} ({a['mode'] or 'unknown mode'})", {"adapters": 0, "ports": 0, "ports_up": 0, "speeds_up": Counter()})
            m["adapters"] += 1
            m["ports"] += a["ports"]
            m["ports_up"] += a["ports_up"]
            for s in a["speeds_up"]:
                n, _, speed = s.partition(" x ")
                speeds[speed if a.get("port_modes") else f"{speed} {a['mode'] or 'unknown'}"] += int(n)
                m["speeds_up"][speed] += int(n)
        report.append({
            "name": r["name"],
            "model": r["model"],
            "connected": r["connected"],
            "adapters": [{k: v for k, v in a.items() if k not in ("manufacturer", "part_number")} for a in adapters],
        })
    modes = _mode_totals(rows)
    return {
        "servers_checked": len(rows),
        "servers_with_nic_data": len(report),
        "nic_modes": modes,
        "most_used_nic_mode": next(iter(modes), None),
        "adapters_by_model": {
            k: {**v, "speeds_up": [f"{n} x {s}" for s, n in v["speeds_up"].most_common()]}
            for k, v in sorted(by_model.items(), key=lambda kv: (-kv[1]["adapters"], -kv[1]["ports_up"]))
        },
        "most_used_model": {
            "by_adapters": max(by_model, key=lambda k: (by_model[k]["adapters"], by_model[k]["ports_up"]), default=None),
            "by_linked_ports": max(by_model, key=lambda k: (by_model[k]["ports_up"], by_model[k]["adapters"]), default=None),
        },
        "linked_ports_by_speed": dict(speeds.most_common()),
        "nic_unknown": unknown,
        "note": "COM's cached inventory. Mode is each port's network technology (Ethernet, InfiniBand, FibreChannel), "
        "so a VPI card shows the mode it runs in. Servers in nic_unknown have no cached network inventory "
        "(full inventory never collected, common on Gen10 and Synergy), so their NICs are unknown, not absent.",
        "servers": report,
    }


async def list_services(c: ComClient, provisioned_only: bool = False) -> dict[str, Any]:
    """GreenLake services: the whole catalog with where each is set up in this workspace."""
    catalog, provisions, workspace = await asyncio.gather(
        c.get_global_all("/service-catalog/v1/service-managers"),
        c.get_global_all("/service-catalog/v1/service-manager-provisions"),
        _workspace_name(c),
    )
    by_service: dict[str, list[dict[str, Any]]] = {}
    for p in provisions:
        by_service.setdefault((p.get("serviceManager") or {}).get("id", ""), []).append(
            {"region": p.get("region"), "status": p.get("provisionStatus"), **({"reason": p["reason"]} if p.get("reason") else {})}
        )
    known = {s.get("id") for s in catalog}
    rows = []
    for s in catalog:
        where = sorted(by_service.get(s.get("id"), []), key=lambda x: str(x["region"]))
        if provisioned_only and not any(w["status"] == "PROVISIONED" for w in where):
            continue
        rows.append({
            "name": s.get("name"),
            "provisioned": any(w["status"] == "PROVISIONED" for w in where),
            "regions": where,
            "description": " ".join((s.get("description") or "").split()) or None,
        })
    rows.sort(key=lambda r: (not r["provisioned"], r["name"] or ""))
    return {
        "workspace": workspace,
        "services_available": len(catalog),
        "services_provisioned": sum(1 for r in rows if r["provisioned"]),
        "services": rows,
        **({"unmatched_provisions": [p for k, v in by_service.items() if k not in known for p in v]} if set(by_service) - known else {}),
        "note": "services_available is the GreenLake service catalog; regions lists where a service is set up in this "
        "API client's workspace (PROVISIONED means ready to use).",
    }


async def _workspace_name(c: ComClient) -> str | None:
    """The workspace this API client belongs to, from the saved sign-in when known."""
    return c.account.workspace or None


async def get_server(c: ComClient, query: str) -> dict[str, Any]:
    s = find_server(await c.get_all("/servers"), query)
    detail = server_detail(s)
    alerts = await c.get_all(f"/servers/{s['id']}/alerts", {"sort": "createdAt desc"}, max_items=200)
    detail["active_alerts"] = [alert_row(a) for a in alerts if not a.get("cleared")][:20]
    try:
        detail["hardware"] = hardware_summary(await c.get(f"/servers/{s['id']}/inventory"), detail=True, network=True)
    except ComError as exc:
        detail["hardware"] = {"inventory_available": False, "inventory_error": str(exc)}
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
