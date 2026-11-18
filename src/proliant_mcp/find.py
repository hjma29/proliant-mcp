"""``find_server``: one name, IP, hostname or serial looked up in every source at once.

Searches the iLO inventory, every COM account and every OneView appliance in parallel. When
nothing matches and the query is an address, it can read the iLO's login-free identity pages
(``/redfish/v1/`` and ``/xmldata?item=all``) to say what is there, without credentials.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from proliant_mcp import com, oneview
from proliant_mcp.com import ComClient, ComError
from proliant_mcp.config import load_com, load_oneview, load_servers
from proliant_mcp.oneview import OneViewClient, OneViewError
from proliant_mcp.util import matches

_PROBE_TIMEOUT = httpx.Timeout(timeout=8.0, connect=5.0)
_XML_FIELDS = {"SPN": "server_model", "SBSN": "serial_number", "PRODUCTID": "product_id", "PN": "ilo_model", "FWRI": "ilo_firmware",
               "UUID": "uuid"}


def _ilo_matches(query: str) -> list[dict[str, Any]]:
    return [{"name": s.name, "ilo_host": s.host} for s in load_servers().values() if matches(query, s.name, s.host)]


def _com_row(s: dict[str, Any]) -> dict[str, Any]:
    r = com.server_row(s)
    return {k: r[k] for k in ("name", "serial_number", "model", "generation", "health", "power_state", "connected", "ilo", "os", "os_hostname", "oneview")}


async def _com_matches(query: str, transport: httpx.AsyncBaseTransport | None) -> list[dict[str, Any]]:
    async def one(account: Any) -> dict[str, Any]:
        try:
            async with ComClient(account, transport=transport) as c:
                servers = await c.get_all("/servers")
        except ComError as exc:
            return {"account": account.name, "error": str(exc)}
        found = []
        for s in servers:
            hw = s.get("hardware") or {}
            bmc = hw.get("bmc") or {}
            if matches(query, s.get("id"), s.get("name"), hw.get("serialNumber"), bmc.get("hostname"), bmc.get("ip"), (s.get("host") or {}).get("hostname")):
                found.append(_com_row(s))
        return {"account": account.name, "servers_searched": len(servers), "matches": found}

    return list(await asyncio.gather(*(one(a) for a in load_com().values())))


async def _oneview_matches(query: str, transport: httpx.AsyncBaseTransport | None) -> list[dict[str, Any]]:
    async def one(appliance: Any) -> dict[str, Any]:
        try:
            async with OneViewClient(appliance, transport=transport) as c:
                hardware, interconnects = await asyncio.gather(c.get_all("/rest/server-hardware"), c.get_all("/rest/interconnects"))
        except OneViewError as exc:
            return {"appliance": appliance.name, "error": str(exc)}
        servers = []
        for h in hardware:
            mp = h.get("mpHostInfo") or {}
            if matches(query, h.get("name"), h.get("serverName"), h.get("serialNumber"), h.get("uuid"), mp.get("mpHostName"), *oneview._ips(mp)):
                row = oneview.hardware_row(h, {}, {})
                servers.append({k: row[k] for k in ("name", "server_name", "model", "serial_number", "status", "power_state", "bay", "os", "ilo")})
        modules = []
        for i in interconnects:
            row = oneview.interconnect_row(i)
            if matches(query, row["name"], row["hostname"], row["ip"], row["serial_number"]):
                modules.append({k: row[k] for k in ("name", "model", "status", "hostname", "ip", "serial_number")})
        return {"appliance": appliance.name, "server_hardware": servers, "interconnects": modules}

    return list(await asyncio.gather(*(one(a) for a in load_oneview().values())))


async def probe_ilo(host: str, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """What answers at *host*: the iLO's unauthenticated Redfish root and XML identity page."""
    result: dict[str, Any] = {"host": host}
    async with httpx.AsyncClient(base_url=f"https://{host}", verify=False, timeout=_PROBE_TIMEOUT, transport=transport) as http:
        try:
            resp = await http.get("/redfish/v1/")
        except httpx.HTTPError as exc:
            return {**result, "reachable": False, "error": f"cannot reach https://{host} from this computer ({exc.__class__.__name__})"}
        result["reachable"] = True
        if resp.status_code < 400:
            try:
                root = resp.json()
            except ValueError:
                root = {}
            manager = ((root.get("Oem") or {}).get("Hpe") or {}).get("Manager") or [{}]
            m = manager[0] if manager else {}
            result.update({
                "redfish_product": root.get("Product"),
                "redfish_vendor": root.get("Vendor"),
                "ilo_type": m.get("ManagerType"),
                "ilo_firmware": m.get("ManagerFirmwareVersion"),
                "ilo_hostname": m.get("HostName"),
                "ilo_fqdn": m.get("FQDN"),
            })
        try:
            xml = await http.get("/xmldata", params={"item": "all"})
            if xml.status_code < 400:
                for tag, key in _XML_FIELDS.items():
                    hit = re.search(rf"<{tag}>\s*([^<]*?)\s*</{tag}>", xml.text)
                    if hit and hit.group(1):
                        result[key] = hit.group(1)
        except httpx.HTTPError:
            pass
    return {k: v for k, v in result.items() if v not in (None, "")}


_ADDRESS = re.compile(r"^[A-Za-z0-9.:-]+$")


async def find_server(query: str, probe: bool, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    q = query.strip()
    com_results, ov_results = await asyncio.gather(_com_matches(q, transport), _oneview_matches(q, transport))
    ilo = _ilo_matches(q)
    found = bool(ilo) or any(r.get("matches") for r in com_results) or any(
        r.get("server_hardware") or r.get("interconnects") for r in ov_results
    )
    result: dict[str, Any] = {
        "query": q,
        "found": found,
        "ilo_inventory": ilo,
        "compute_ops_management": com_results,
        "oneview": ov_results,
    }
    if not found and probe and _ADDRESS.match(q):
        result["probe"] = await probe_ilo(q, transport)
        result["note"] = (
            "Not in the iLO inventory, COM or OneView. probe shows what answers at that address without logging in "
            "(an HPE iLO gives its model, firmware, server model and serial). To manage it, add it with add_server."
        )
    elif not found:
        result["note"] = "Not in the iLO inventory, COM or OneView (matched by name, iLO IP/hostname, serial or OS hostname)."
    return result
