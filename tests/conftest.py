import os

import pytest

# The server hides five COM list tools by default; most tests exercise them, so they run in the several-tools mode.
os.environ.setdefault("PROLIANT_MCP_COM_LIST", "several")


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    """Sign-in and add-server pages must not open a real browser during tests."""
    monkeypatch.setenv("PROLIANT_MCP_NO_BROWSER", "1")
