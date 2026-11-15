"""Server inventory: which iLOs this MCP server may talk to, and their credentials.

Credentials never live in this repository. They come from (first match wins):

1. ``PROLIANT_MCP_CONFIG`` environment variable -> path to an ini file
2. ``~/.config/proliant-mcp/inventory.ini``
3. ``~/.config/proliant-cli/inventory.ini`` (same format as proliant-cli)

plus an optional single server from ``ILO_HOST`` / ``ILO_USERNAME`` /
``ILO_PASSWORD`` (name from ``ILO_NAME``, default ``default``).

Ini format::

    [defaults]
    username = Administrator
    password = ...

    [dl380-gen11]
    host = 192.0.2.10
    # username / password / verify_ssl may override [defaults]

username defaults to ``Administrator`` when not set anywhere.

Sections with ``type`` other than ``ilo`` (e.g. ``type = oneview``) are skipped.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Server:
    name: str
    host: str
    username: str
    password: str = field(repr=False)
    verify_ssl: bool = False

    @property
    def base_url(self) -> str:
        host = self.host.strip().rstrip("/")
        return host if host.startswith(("http://", "https://")) else f"https://{host}"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def config_path() -> Path | None:
    explicit = os.environ.get("PROLIANT_MCP_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    for candidate in (
        Path("~/.config/proliant-mcp/inventory.ini").expanduser(),
        Path("~/.config/proliant-cli/inventory.ini").expanduser(),
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


def load_servers(path: Path | None = None) -> dict[str, Server]:
    servers: dict[str, Server] = {}
    path = path if path is not None else config_path()

    if path is not None:
        if not path.is_file():
            raise FileNotFoundError(f"Inventory file not found: {path}")
        parser = configparser.ConfigParser(default_section="defaults", interpolation=None)
        parser.read_string(_read_text(path), source=str(path))
        for name in parser.sections():
            sec = parser[name]
            if sec.get("type", "ilo").strip().lower() != "ilo":
                continue
            host = sec.get("host")
            if not host:
                continue
            servers[name] = Server(
                name=name,
                host=host,
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
