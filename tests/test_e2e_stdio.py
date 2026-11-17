"""End to end: launch the real `hpe-compute-mcp` process over stdio against a local fake iLO."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from fake_ilo import FAKE_ILO


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict, headers: dict | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._send(201, {}, {"X-Auth-Token": "tok", "Location": "/redfish/v1/SessionService/Sessions/1/"})

    def do_DELETE(self):
        self._send(200, {})

    def do_GET(self):
        path = self.path.split("?")[0]
        self._send(200, FAKE_ILO[path]) if path in FAKE_ILO else self._send(404, {})

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_ilo_url():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


async def test_stdio_process_serves_tools(fake_ilo_url, tmp_path):
    empty_inventory = tmp_path / "inventory.ini"
    empty_inventory.write_text("")
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "hpe_compute_mcp"],
        env={
            "ILO_HOST": fake_ilo_url, "ILO_NAME": "lab", "ILO_PASSWORD": "good",
            "HPE_COMPUTE_MCP_CONFIG": str(empty_inventory), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
        },
        keep_alive=False,
    )
    async with Client(transport) as c:
        names = {t.name for t in await c.list_tools()}
        assert {"list_servers", "get_server_summary", "get_ilo_info", "com_get_overview", "oneview_get_appliance"} <= names
        summary = (await c.call_tool("get_server_summary", {"server": "lab"})).structured_content
    assert summary["model"] == "HPE ProLiant Compute DL345 Gen12"
    assert summary["power_state"] == "On"
