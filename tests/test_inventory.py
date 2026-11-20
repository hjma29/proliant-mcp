import configparser
import json
import os
import re
import stat
import sys

import httpx
import pytest
from fastmcp import Client
from mcp_types import ElicitRequestURLParams, ElicitResult

from fake_ilo import fake_ilo
from fake_mgmt import fake_mgmt
from proliant_mcp import inventory, inventory_cli
from proliant_mcp import server as srv
from proliant_mcp.config import load_oneview, load_servers

UNREACHABLE = "192.0.2.99"


def fake_world(request: httpx.Request) -> httpx.Response:
    if request.url.host == UNREACHABLE:
        raise httpx.ConnectError("no route to host")
    return fake_mgmt(request) or fake_ilo(request)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for var in ("PROLIANT_MCP_CONFIG", "ILO_HOST", "ONEVIEW_HOST", "HPECOM_CLIENT_ID", "HPECOM_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(srv, "_transport", httpx.MockTransport(fake_world))
    return tmp_path


def our_inventory(home):
    return home / ".config" / "proliant-mcp" / "inventory.ini"


# ── editing the file ──────────────────────────────────────────────────────────

def test_upsert_changes_only_the_sections_it_is_asked_to():
    text = "# my lab\n[defaults]\npassword = x\n\n[a]\nhost = 1.1.1.1\nusername = u\n\n# second box\n[b]\nhost = 2.2.2.2\n"
    a2 = inventory.make_entry("A", "ilo", "3.3.3.3", "root", "pw")  # names match without regard to case
    c = inventory.make_entry("c", "oneview", "4.4.4.4", "", "pw2")

    new, added, replaced, skipped = inventory.upsert_sections(text, [a2, c])
    assert (added, replaced, skipped) == (["c"], ["A"], [])
    assert new.startswith("# my lab\n[defaults]\npassword = x\n\n[A]\ntype = ilo\nhost = 3.3.3.3\nusername = root\npassword = pw\n")
    assert "\n# second box\n[b]\nhost = 2.2.2.2\n" in new  # the next section and its comment are untouched
    assert "1.1.1.1" not in new and new.endswith("[c]\ntype = oneview\nhost = 4.4.4.4\nusername = Administrator\npassword = pw2\n")
    parser = configparser.ConfigParser(default_section="defaults", interpolation=None)
    parser.read_string(new)
    assert parser.sections() == ["A", "b", "c"]

    same, added, replaced, skipped = inventory.upsert_sections(text, [a2], replace_existing=False)
    assert (same, added, replaced, skipped) == (text, [], [], ["A"])


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"host": ""}, "no host"),
        ({"host": "a b"}, "not a valid host"),
        ({"host": "1.1.1.1", "name": "defaults"}, "reserved"),
        ({"host": "1.1.1.1", "name": "x[1]"}, "not a valid name"),
        ({"host": "1.1.1.1", "password": "line\nbreak"}, "control character"),
        ({"host": "1.1.1.1", "password": " lead"}, "space"),
        ({"host": "1.1.1.1", "kind": "switch"}, "ilo or oneview"),
    ],
)
def test_make_entry_rejects_what_would_corrupt_the_file(kwargs, message):
    with pytest.raises(inventory.InventoryError, match=message):
        inventory.make_entry(**{"name": "", "kind": "ilo", **kwargs})
    assert inventory.make_entry("", "ilo", "https://10.0.0.5/").name == "10.0.0.5"


def test_save_entries_locations_and_permissions(home, monkeypatch):
    lab = inventory.make_entry("lab", "ilo", "192.0.2.10", "", "good")
    cli = home / ".config" / "proliant-cli" / "inventory.ini"
    cli.parent.mkdir(parents=True)
    cli.write_text("[old]\nhost = 192.0.2.50\npassword = good\n")

    first = inventory.save_entries([lab])  # proliant-cli's file is copied, never edited
    assert first.path == our_inventory(home) and first.copied_from == cli and first.added == ["lab"]
    assert cli.read_text() == "[old]\nhost = 192.0.2.50\npassword = good\n"
    assert set(load_servers()) == {"old", "lab"}
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(first.path).st_mode) == 0o600

    lab2 = inventory.make_entry("lab", "ilo", "192.0.2.11", "", "good")
    second = inventory.save_entries([lab2])  # replacing an existing, owner-only file works
    assert (second.replaced, second.copied_from) == (["lab"], None)
    assert load_servers()["lab"].host == "192.0.2.11"

    elsewhere = home / "elsewhere.ini"
    monkeypatch.setenv("PROLIANT_MCP_CONFIG", str(elsewhere))
    assert inventory.save_entries([lab]).path == elsewhere and elsewhere.is_file()


# ── reading a file the user gives ─────────────────────────────────────────────

def test_import_csv_with_excel_style_columns_and_problems(tmp_path):
    csv_file = tmp_path / "servers.csv"
    csv_file.write_bytes(
        "Server Name;Product;iLO IP;User;Pass;Verify SSL\n"
        "köln-1;iLO;192.0.2.10;root;good;yes\n"
        ";;192.0.2.11;;;\n"
        "synergy;Synergy Composer;192.0.2.100;;good;\n"
        "greenlake;COM;x.example.com;;;\n"
        "bad;iLO;;;;\n"
        "köln-1;iLO;192.0.2.12;;;\n".encode("cp1252")  # Excel's default "CSV" on Windows
    )
    parsed = inventory.parse_inventory_file(f'"{csv_file}"')  # Windows "Copy as path" adds quotes
    by_name = {e.name: e for e in parsed.entries}
    assert list(by_name) == ["köln-1", "192.0.2.11", "synergy"]
    assert (by_name["köln-1"].username, by_name["köln-1"].verify_ssl) == ("root", True)
    assert by_name["synergy"].kind == "oneview" and by_name["192.0.2.11"].password == ""
    text = " | ".join(parsed.problems)
    assert "use the com_login tool" in text and "no host" in text and "listed more than once" in text


def test_import_ini_resolves_defaults_and_rejects_bad_files(tmp_path):
    ini = tmp_path / "inv.ini"
    ini.write_text("[defaults]\nusername = admin\npassword = good\n\n[a]\nhost = 192.0.2.10\n\n[oneview]\nhost = 192.0.2.100\n\n[greenlake]\ntype = com\nclient_id = c\n", encoding="utf-16")
    parsed = inventory.parse_inventory_file(str(ini))
    assert [(e.name, e.kind, e.username, e.password) for e in parsed.entries] == [("a", "ilo", "admin", "good"), ("oneview", "oneview", "admin", "good")]
    assert len(parsed.problems) == 1 and "com_login" in parsed.problems[0]

    broken = tmp_path / "broken.ini"
    broken.write_text("[a]\nhost = 1.1.1.1\npassword = topsecretpw\nthis line is not valid\n")
    with pytest.raises(inventory.InventoryError, match="line 4") as excinfo:
        inventory.parse_inventory_file(str(broken))
    assert "topsecretpw" not in str(excinfo.value)

    book = tmp_path / "servers.csv"
    book.write_bytes(b"PK\x03\x04 pretend workbook")
    with pytest.raises(inventory.InventoryError, match="Save As > CSV"):
        inventory.parse_inventory_file(str(book))
    with pytest.raises(inventory.InventoryError, match="File not found"):
        inventory.parse_inventory_file(str(tmp_path / "nope.csv"))
    empty = tmp_path / "hosts.csv"
    empty.write_text("name,type\nx,ilo\n")
    with pytest.raises(inventory.InventoryError, match="no host column"):
        inventory.parse_inventory_file(str(empty))


# ── the pop-up tools ──────────────────────────────────────────────────────────

def user(answers, passwords):
    """Plays the person: answers each form by its first matching field, types passwords on the local page."""
    seen = []

    async def handler(message, response_type, params, context):
        if isinstance(params, ElicitRequestURLParams):
            seen.append(("password page", message))
            async with httpx.AsyncClient() as browser:
                page = await browser.get(params.url)
                assert 'type="password"' in page.text.replace("'", '"')
                assert "password received" in (await browser.post(params.url, data={"password": passwords.pop(0)})).text.lower()
            return ElicitResult(action="accept")
        props = params.requested_schema["properties"]
        key = next(k for k in answers if k in props)
        seen.append((key, message))
        reply = answers[key]
        return ElicitResult(action="decline") if reply is None else reply

    return handler, seen


@pytest.mark.parametrize("mode", ["legacy", "auto"])  # 2025-11-25 live pop-ups / 2026-07-28 input-required rounds
async def test_add_server_checks_the_password_before_saving(home, mode):
    form = {"type": "ilo", "host": "192.0.2.10", "name": "lab1", "username": "Administrator"}
    handler, seen = user({"host": form}, passwords=["wrong", "good"])
    async with Client(srv.mcp, mode=mode, elicitation_handler=handler) as client:
        data = (await client.call_tool("add_server", {})).structured_content
        listed = (await client.call_tool("list_servers", {})).data
    assert [kind for kind, _ in seen] == ["host", "password page", "password page"]
    assert "rejected that user name or password" in seen[2][1]
    assert (data["status"], data["name"], data["checked"]) == ("saved", "lab1", "HPE ProLiant Compute DL345 Gen12, iLO 7 1.21.00 Apr 07 2026")
    assert "password" not in data and "wrong" not in json.dumps(data)
    assert [s["name"] for s in listed["servers"]] == ["lab1"]  # usable straight away, no restart
    saved = our_inventory(home).read_text()
    assert "password = good" in saved and "wrong" not in saved


async def test_add_oneview_and_failures_save_nothing(home):
    handler, _ = user({"host": {"type": "oneview", "host": "https://192.0.2.100/", "name": "synergy"}}, passwords=["good"])
    async with Client(srv.mcp, elicitation_handler=handler) as client:
        data = (await client.call_tool("add_server", {})).structured_content
    assert (data["type"], data["host"], data["checked"]) == ("oneview", "192.0.2.100", "Synergy Composer2 11.10.00-1")
    assert list(load_oneview()) == ["synergy"]
    before = our_inventory(home).read_text()

    unreachable, _ = user({"host": {"type": "ilo", "host": UNREACHABLE}}, passwords=["good"])
    async with Client(srv.mcp, elicitation_handler=unreachable) as client:
        result = await client.call_tool("add_server", {}, raise_on_error=False)
    assert result.is_error and "cannot reach iLO" in result.content[0].text and "Nothing was saved" in result.content[0].text

    wrong, seen = user({"host": {"type": "ilo", "host": "192.0.2.10"}}, passwords=["a", "b", "c"])
    async with Client(srv.mcp, elicitation_handler=wrong) as client:
        result = await client.call_tool("add_server", {}, raise_on_error=False)
    assert result.is_error and "three times" in result.content[0].text and len(seen) == 4
    assert our_inventory(home).read_text() == before


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_import_inventory_from_a_csv_in_chat(home, mode):
    csv_file = home / "lab.csv"
    csv_file.write_text("name,type,host,username,password\ngpu1,ilo,192.0.2.10,Administrator,good\ngpu2,ilo,192.0.2.11,,\nsynergy,oneview,192.0.2.100,,\n")
    answers = {"path": {"path": str(csv_file), "replace": False}, "proceed": {"proceed": True, "shared_password": True, "check": True}}
    handler, seen = user(answers, passwords=["good"])
    async with Client(srv.mcp, mode=mode, elicitation_handler=handler) as client:
        data = (await client.call_tool("import_inventory", {})).structured_content
    assert [kind for kind, _ in seen] == ["path", "proceed", "password page"]
    assert "Found 3 entries (2 iLO, 1 OneView) in lab.csv" in seen[1][1]
    assert data["added"] == ["gpu1", "gpu2", "synergy"] and data["without_password"] == []
    assert data["checked"] == {"ok": 3, "password_rejected": [], "unreachable_or_failed": []}
    assert any("plain text" in note for note in data["notes"])
    assert set(load_servers()) == {"gpu1", "gpu2"} and set(load_oneview()) == {"synergy"}
    assert load_servers()["gpu2"].password == "good"

    again, _ = user({"path": {"path": str(csv_file), "replace": False}, "proceed": {"proceed": True, "check": False}}, passwords=[])
    async with Client(srv.mcp, mode=mode, elicitation_handler=again) as client:
        second = (await client.call_tool("import_inventory", {})).structured_content
    assert second["added"] == [] and second["skipped_already_present"] == ["gpu1", "gpu2", "synergy"]


async def test_import_can_be_declined_and_needs_pop_ups(home):
    csv_file = home / "lab.csv"
    csv_file.write_text("name,host,password\ngpu1,192.0.2.10,good\n")
    handler, _ = user({"path": {"path": str(csv_file)}, "proceed": {"proceed": False}}, passwords=[])
    async with Client(srv.mcp, elicitation_handler=handler) as client:
        result = await client.call_tool("import_inventory", {}, raise_on_error=False)
    assert result.is_error and "Cancelled" in result.content[0].text
    assert not our_inventory(home).exists()

    async with Client(srv.mcp) as client:  # no pop-ups: continues on a local page
        result = await client.call_tool("add_server", {}, raise_on_error=False)
    assert result.structured_content["status"] == "enter_server"

    bad, _ = user({"path": {"path": str(home / "missing.csv")}}, passwords=[])
    async with Client(srv.mcp, elicitation_handler=bad) as client:
        result = await client.call_tool("import_inventory", {}, raise_on_error=False)
    assert result.is_error and "File not found" in result.content[0].text


async def test_add_server_without_pop_ups_checks_the_password_on_a_local_page(home):
    async with Client(srv.mcp) as client, httpx.AsyncClient() as browser:  # no elicitation handler
        first = (await client.call_tool("add_server_start", {"host": "192.0.2.10", "name": "lab1"})).structured_content
        assert first["status"] == "enter_password" and first["url"].startswith("http://127.0.0.1:")
        assert "add_server_status" in first["next_step"] and not our_inventory(home).exists()
        await browser.post(first["url"], data={"password": "wrong"})
        retry = (await client.call_tool("add_server_status", {})).structured_content
        assert retry["status"] == "enter_password" and "rejected" in retry["next_step"] and retry["url"] == first["url"]  # one page for every step
        await browser.post(retry["url"], data={"password": "good"})
        done = (await client.call_tool("add_server_status", {})).structured_content
        listed = (await client.call_tool("list_servers", {})).data
        bad = await client.call_tool("add_server_start", {"host": "bad host name!"}, raise_on_error=False)
    assert (done["status"], done["name"], done["checked"]) == ("saved", "lab1", "HPE ProLiant Compute DL345 Gen12, iLO 7 1.21.00 Apr 07 2026")
    assert "wrong" not in json.dumps(done) and "good" not in json.dumps(done)
    assert [s["name"] for s in listed["servers"]] == ["lab1"]
    assert bad.is_error and "Cannot add that server" in bad.content[0].text


async def test_add_server_without_host_or_pop_ups_asks_for_everything_on_one_page(home):
    async with Client(srv.mcp) as client, httpx.AsyncClient() as browser:
        first = (await client.call_tool("add_server", {})).structured_content
        assert first["status"] == "enter_server"
        reply = await browser.get(first["url"])
        page = reply.text
        assert "name='host'" in page and "<select" in page and "type='password'" in page
        nonce = re.search(r"script-src 'nonce-([^']+)'", reply.headers["content-security-policy"]).group(1)
        assert "id='reveal'" in page and f"<script nonce='{nonce}'>" in page  # show-password eye, the only script allowed
        assert nonce not in (await browser.get(first["url"])).text  # a fresh nonce per page load
        missing = await browser.post(first["url"], data={"type": "ilo", "host": "", "username": "Administrator", "password": "good"}, follow_redirects=True)
        assert "Enter iLO or OneView IP address or name" in missing.text
        await browser.post(first["url"], data={"type": "ilo", "host": "192.0.2.10", "name": "lab2", "username": "Administrator", "password": "good"})
        done = (await client.call_tool("add_server_status", {})).structured_content
    assert (done["status"], done["name"], done["host"]) == ("saved", "lab2", "192.0.2.10")
    assert "good" not in json.dumps(done)


def test_terminal_import(home, capsys):
    csv_file = home / "lab.csv"
    csv_file.write_text("name,host,password\ngpu1,192.0.2.10,good\ngpu2,192.0.2.10,wrong\n")
    assert inventory_cli.run_import(str(csv_file), replace=False, check=True) == 1  # gpu2's password is rejected
    out = capsys.readouterr().out
    assert "gpu1  OK  HPE ProLiant Compute DL345 Gen12" in out and "gpu2  FAILED" in out and "2 added" in out
    assert set(load_servers()) == {"gpu1", "gpu2"}
    assert inventory_cli.run_import(str(home / "nope.csv"), replace=False) == 1
    assert re.search(r"Import failed: File not found", capsys.readouterr().err)
