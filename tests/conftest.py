import pytest


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    """Sign-in and add-server pages must not open a real browser during tests."""
    monkeypatch.setenv("PROLIANT_MCP_NO_BROWSER", "1")
