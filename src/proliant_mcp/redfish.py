"""Minimal async Redfish client for HPE iLO 5/6/7 (session auth, httpx)."""

from __future__ import annotations

from typing import Any

import httpx

from proliant_mcp.config import Server

_TIMEOUT = httpx.Timeout(timeout=60.0, connect=10.0)
_LOGIN_TIMEOUT = httpx.Timeout(timeout=15.0, connect=8.0)


class RedfishError(RuntimeError):
    pass


class RedfishLoginError(RedfishError):
    """The iLO answered but refused the user name or password."""


def _error_detail(resp: httpx.Response) -> str:
    try:
        info = resp.json().get("error", {}).get("@Message.ExtendedInfo", [])
        ids = [i.get("MessageId", "") for i in info if isinstance(i, dict)]
        if ids:
            return ", ".join(ids)
    except Exception:  # noqa: BLE001
        pass
    return resp.reason_phrase or ""


class RedfishClient:
    """One authenticated Redfish session. Use as ``async with RedfishClient(server)``."""

    def __init__(self, server: Server, transport: httpx.AsyncBaseTransport | None = None):
        self.server = server
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._session_uri: str | None = None
        self._root: dict[str, Any] | None = None
        self._first_member: dict[str, str] = {}

    async def __aenter__(self) -> "RedfishClient":
        self._http = httpx.AsyncClient(
            base_url=self.server.base_url,
            verify=self.server.verify_ssl,
            timeout=_TIMEOUT,
            transport=self._transport,
        )
        try:
            resp = await self._http.post(
                "/redfish/v1/SessionService/Sessions",
                json={"UserName": self.server.username, "Password": self.server.password},
                timeout=_LOGIN_TIMEOUT,
            )
        except httpx.TransportError as exc:
            await self._http.aclose()
            raise RedfishError(f"{self.server.name}: cannot reach iLO at {self.server.host} ({exc.__class__.__name__})") from exc
        if resp.status_code >= 400:
            await self._http.aclose()
            raise RedfishLoginError(f"{self.server.name}: login failed (HTTP {resp.status_code} {_error_detail(resp)})")
        token = resp.headers.get("X-Auth-Token")
        if not token:
            await self._http.aclose()
            raise RedfishError(f"{self.server.name}: login response had no X-Auth-Token")
        self._http.headers["X-Auth-Token"] = token
        self._session_uri = resp.headers.get("Location") or (resp.json() or {}).get("@odata.id")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._http is None:
            return
        try:
            if self._session_uri:
                await self._http.delete(self._session_uri)
        except Exception:  # noqa: BLE001 - logout is best effort
            pass
        finally:
            await self._http.aclose()
            self._http = None

    async def get(self, uri: str) -> dict[str, Any]:
        if self._http is None:
            raise RedfishError("Redfish session is not open")
        try:
            resp = await self._http.get(uri)
        except httpx.TransportError as exc:
            raise RedfishError(f"{self.server.name}: GET {uri} failed ({exc.__class__.__name__})") from exc
        if resp.status_code >= 400:
            raise RedfishError(f"{self.server.name}: GET {uri} -> HTTP {resp.status_code} {_error_detail(resp)}")
        return resp.json() if resp.content else {}

    async def root(self) -> dict[str, Any]:
        if self._root is None:
            self._root = await self.get("/redfish/v1/")
        return self._root

    async def _first(self, collection: str) -> str:
        if collection not in self._first_member:
            uri = (await self.root()).get(collection, {}).get("@odata.id")
            if not uri:
                raise RedfishError(f"{self.server.name}: Redfish root has no {collection}")
            members = (await self.get(uri)).get("Members", [])
            if not members:
                raise RedfishError(f"{self.server.name}: {collection} collection is empty")
            self._first_member[collection] = members[0]["@odata.id"]
        return self._first_member[collection]

    async def system(self) -> dict[str, Any]:
        return await self.get(await self._first("Systems"))

    async def manager(self) -> dict[str, Any]:
        return await self.get(await self._first("Managers"))

    async def chassis(self) -> dict[str, Any]:
        return await self.get(await self._first("Chassis"))

    async def members(self, collection_uri: str | None) -> list[dict[str, Any]]:
        """Return every member of a collection, expanded.

        Uses ``$expand=.`` when the iLO advertises it (one round trip), otherwise
        walks members sequentially (iLO is HTTP/1.1; parallel GETs don't help).
        """
        if not collection_uri:
            return []
        expand = ((await self.root()).get("ProtocolFeaturesSupported") or {}).get("ExpandQuery") or {}
        members: list[dict[str, Any]] | None = None
        if expand.get("NoLinks"):
            sep = "&" if "?" in collection_uri else "?"
            try:
                members = (await self.get(f"{collection_uri}{sep}$expand=.")).get("Members", [])
            except RedfishError:
                members = None
            if members and all(len(m) > 1 for m in members):
                return members
        if members is None:
            members = (await self.get(collection_uri)).get("Members", [])
        return [await self.get(m["@odata.id"]) for m in members if "@odata.id" in m]
