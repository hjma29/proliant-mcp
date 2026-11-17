"""Inventory: which iLOs, OneView appliances and Compute Ops Management (COM)
accounts this MCP server may talk to, and their credentials.

Credentials never live in this repository. The ini file is the first match of:

1. ``PROLIANT_MCP_CONFIG`` -> path to an ini file
2. ``~/.config/proliant-mcp/inventory.ini``
3. ``~/.config/proliant-cli/inventory.ini`` (same format as proliant-cli)

Ini format (section ``type`` defaults to ``ilo``; a section named ``[oneview]``
is a OneView appliance)::

    [defaults]
    username = Administrator
    password = ...

    [dl380-gen11]
    host = 192.0.2.10
    # username / password / verify_ssl may override [defaults]

    [synergy]
    type = oneview
    host = 192.0.2.100

    [greenlake]
    type = com
    client_id = ...
    client_secret = ...
    region = us-west          # us-west, eu-central or ap-northeast

Environment variables add one more of each: ``ILO_HOST`` / ``ILO_USERNAME`` /
``ILO_PASSWORD`` (name ``ILO_NAME``), ``ONEVIEW_HOST`` / ``ONEVIEW_USERNAME`` /
``ONEVIEW_PASSWORD`` (name ``ONEVIEW_NAME``) and ``HPECOM_CLIENT_ID`` /
``HPECOM_CLIENT_SECRET`` / ``HPECOM_REGION``.

When no COM account is configured either way, proliant-cli's own COM login is
reused: ``~/.config/proliant-cli/com/credentials.yml`` or the GreenLake API
client that ``proliant com login`` stores in ``~/.config/proliant-cli/com/token.json``.
"""

from __future__ import annotations

import configparser
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

COM_REGIONS = {
    "us-west": "https://us-west.api.greenlake.hpe.com",
    "eu-central": "https://eu-central.api.greenlake.hpe.com",
    "ap-northeast": "https://ap-northeast.api.greenlake.hpe.com",
    # proliant-cli legacy aliases
    "us-east": "https://us-west.api.greenlake.hpe.com",
    "ap-southeast": "https://ap-northeast.api.greenlake.hpe.com",
}


def _base_url(host: str) -> str:
    host = host.strip().rstrip("/")
    return host if host.startswith(("http://", "https://")) else f"https://{host}"


@dataclass(frozen=True)
class Server:
    name: str
    host: str
    username: str
    password: str = field(repr=False)
    verify_ssl: bool = False

    @property
    def base_url(self) -> str:
        return _base_url(self.host)


@dataclass(frozen=True)
class OneViewAppliance:
    name: str
    host: str
    username: str
    password: str = field(repr=False)
    verify_ssl: bool = False

    @property
    def base_url(self) -> str:
        return _base_url(self.host)


@dataclass(frozen=True)
class ComAccount:
    name: str
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    region: str = "us-west"
    source: str = ""
    workspace: str = ""

    @property
    def base_url(self) -> str:
        url = COM_REGIONS.get(self.region)
        if not url:
            raise ValueError(f"Unknown COM region '{self.region}'. Valid: {', '.join(COM_REGIONS)}")
        return url


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _home_config(*parts: str) -> Path:
    return Path("~/.config").expanduser().joinpath(*parts)


def config_path() -> Path | None:
    explicit = os.environ.get("PROLIANT_MCP_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    for candidate in (
        _home_config("proliant-mcp", "inventory.ini"),
        _home_config("proliant-cli", "inventory.ini"),
    ):
        if candidate.is_file():
            return candidate
    return None


def _read_text(path: Path) -> str:
    """Read an ini file saved by any common Windows editor.

    Windows PowerShell 5.1 (`>` / Out-File) writes UTF-16 and Notepad may add a UTF-8 BOM;
    both would otherwise be misread or rejected.
    """
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def _sections(path: Path | None) -> list[tuple[str, str, configparser.SectionProxy]]:
    """(name, type, section) for every section of the inventory file."""
    path = path if path is not None else config_path()
    if path is None:
        return []
    if not path.is_file():
        raise FileNotFoundError(f"Inventory file not found: {path}")
    parser = configparser.ConfigParser(default_section="defaults", interpolation=None)
    parser.read_string(_read_text(path), source=str(path))
    out = []
    for name in parser.sections():
        sec = parser[name]
        kind = sec.get("type", "oneview" if name.lower() == "oneview" else "ilo").strip().lower()
        out.append((name, kind, sec))
    return out


def load_servers(path: Path | None = None) -> dict[str, Server]:
    servers: dict[str, Server] = {}
    for name, kind, sec in _sections(path):
        if kind != "ilo" or not sec.get("host"):
            continue
        servers[name] = Server(
            name=name,
            host=sec["host"],
            username=sec.get("username", "Administrator"),
            password=sec.get("password", ""),
            verify_ssl=_truthy(sec.get("verify_ssl")),
        )

    env_host = os.environ.get("ILO_HOST")
    if env_host:
        name = os.environ.get("ILO_NAME", "default")
        servers[name] = Server(
            name=name,
            host=env_host,
            username=os.environ.get("ILO_USERNAME", "Administrator"),
            password=os.environ.get("ILO_PASSWORD", ""),
            verify_ssl=_truthy(os.environ.get("ILO_VERIFY_SSL")),
        )
    return servers


def load_oneview(path: Path | None = None) -> dict[str, OneViewAppliance]:
    appliances: dict[str, OneViewAppliance] = {}
    for name, kind, sec in _sections(path):
        if kind != "oneview" or not sec.get("host"):
            continue
        appliances[name] = OneViewAppliance(
            name=name,
            host=sec["host"],
            username=sec.get("username", "Administrator"),
            password=sec.get("password", ""),
            verify_ssl=_truthy(sec.get("verify_ssl")),
        )

    env_host = os.environ.get("ONEVIEW_HOST")
    if env_host:
        name = os.environ.get("ONEVIEW_NAME", "oneview")
        appliances[name] = OneViewAppliance(
            name=name,
            host=env_host,
            username=os.environ.get("ONEVIEW_USERNAME", "Administrator"),
            password=os.environ.get("ONEVIEW_PASSWORD", ""),
            verify_ssl=_truthy(os.environ.get("ONEVIEW_VERIFY_SSL")),
        )
    return appliances


def _proliant_cli_com() -> ComAccount | None:
    """Reuse proliant-cli's COM login (API client file, or the client `proliant com login` stored)."""
    com_dir = _home_config("proliant-cli", "com")
    creds = com_dir / "credentials.yml"
    if creds.is_file():
        data: dict[str, str] = {}
        for line in creds.read_text().splitlines():
            key, sep, value = line.partition(":")
            if sep and not line.lstrip().startswith("#"):
                data[key.strip()] = value.strip().strip("'\"")
        if data.get("client_id") and data.get("client_secret"):
            return ComAccount("com", data["client_id"], data["client_secret"], data.get("region") or "us-west", str(creds))
    token = com_dir / "token.json"
    if token.is_file():
        try:
            data = json.loads(token.read_text())
        except (OSError, ValueError):
            return None
        if data.get("glp_client_id") and data.get("glp_client_secret"):
            return ComAccount("com", data["glp_client_id"], data["glp_client_secret"], data.get("region") or "us-west", str(token),
                              " ".join((data.get("workspace_name") or "").split()))
    return None


def load_com(path: Path | None = None) -> dict[str, ComAccount]:
    accounts: dict[str, ComAccount] = {}
    for name, kind, sec in _sections(path):
        if kind != "com" or not sec.get("client_id"):
            continue
        accounts[name] = ComAccount(name, sec["client_id"], sec.get("client_secret", ""), sec.get("region", "us-west").strip(), "inventory")

    cid, secret = os.environ.get("HPECOM_CLIENT_ID"), os.environ.get("HPECOM_CLIENT_SECRET")
    if cid and secret:
        accounts["com"] = ComAccount("com", cid, secret, os.environ.get("HPECOM_REGION", "us-west"), "environment")

    if not accounts:
        fallback = _proliant_cli_com()
        if fallback:
            accounts[fallback.name] = fallback
    return accounts
