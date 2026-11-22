import json
import re
import urllib.parse

import httpx
import pytest
from fastmcp import Client
from mcp_types import ElicitRequestURLParams, ElicitResult

from proliant_mcp import chat, chat_login, com, login
from proliant_mcp import server as srv
from proliant_mcp.config import load_com

GREENLAKE_USER_API = "aquila-user-api.common.cloud.hpe.com"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("PROLIANT_MCP_CONFIG", raising=False)
    for var in ("HPECOM_CLIENT_ID", "HPECOM_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(login, "POLL_INTERVAL", 0)
    return tmp_path


def idx(handle, *remediation, **extra):
    return {"stateHandle": handle, "remediation": {"value": [dict(zip(("name", "href"), r)) for r in remediation]}, **extra}


def fake_greenlake(*, push=True, pending_polls=1, workspaces=("Lab",), api_clients=None, refuse_delete=()):
    """api_clients: the personal API clients the fake lists (and removes on delete); None keeps the default two."""
    polls = {"n": 0}
    created = []

    def handler(request: httpx.Request) -> httpx.Response:
        url, path = request.url, request.url.path
        if url.host == "common.cloud.hpe.com" and path == "/settings.json":
            return httpx.Response(404)
        if url.host == "sso.common.cloud.hpe.com" and path == "/as/authorization.oauth2":
            return httpx.Response(302, headers={"location": "https://auth.hpe.com/oauth2/default/v1/authorize"})
        if url.host == "auth.hpe.com" and path.endswith("/v1/authorize"):
            return httpx.Response(200, text="<script>var stateToken = '00abc\\x2Ddef';</script>")
        if path == "/idp/idx/introspect":
            return httpx.Response(200, json=idx("h1", ("identify", "https://auth.hpe.com/idp/idx/identify")))
        if path == "/idp/idx/identify" and json.loads(request.content)["identifier"].startswith("locked"):
            return httpx.Response(401, json={**idx("p2", ("identify", "x"), ("unlock-account", "y")),
                                             "messages": {"value": [{"class": "ERROR", "i18n": {"key": "errors.E0000004"}}]}})
        if path == "/idp/idx/identify" and not json.loads(request.content)["identifier"].endswith("@hpe.com"):
            assert request.headers["accept"] == "application/json"
            return httpx.Response(
                200,
                json=idx("p2", ("challenge-authenticator", "https://auth.hpe.com/idp/idx/challenge/answer"),
                         authenticators={"value": [{"id": "p1", "key": "okta_password", "type": "password"}]}),
            )
        if path == "/api/v1/internal/device/nonce":
            return httpx.Response(200, json={})
        if path == "/idp/idx/challenge/answer":
            if json.loads(request.content)["credentials"]["passcode"] != "right-password":
                return httpx.Response(401, json={"errorCode": "E0000004", "errorSummary": "Authentication failed"})
            return httpx.Response(200, json={"success": {"href": "https://auth.hpe.com/login/token/redirect?stateToken=y"}})
        if path == "/idp/idx/identify":
            authenticators = {"value": [{"id": "a1", "key": "okta_verify", "methods": [{"type": "push" if push else "totp"}]}]}
            return httpx.Response(
                200, json=idx("h2", ("select-authenticator-authenticate", "https://auth.hpe.com/idp/idx/challenge"), authenticators=authenticators)
            )
        if path == "/idp/idx/challenge":
            return httpx.Response(
                200,
                json=idx("h3", ("challenge-poll", "https://auth.hpe.com/idp/idx/challenge/poll"),
                         currentAuthenticator={"value": {"contextualData": {"correctAnswer": 21}}}),
            )
        if path == "/idp/idx/challenge/poll":
            polls["n"] += 1
            if polls["n"] <= pending_polls:
                return httpx.Response(200, json=idx("h3"))
            return httpx.Response(200, json={"success": {"href": "https://auth.hpe.com/login/token/redirect?stateToken=x"}})
        if path == "/login/token/redirect":
            return httpx.Response(302, headers={"location": "https://common.cloud.hpe.com/authentication/callback?code=abc"})
        if url.host == "common.cloud.hpe.com":
            return httpx.Response(200, text="app")
        if url.host == "sso.common.cloud.hpe.com" and path == "/as/token.oauth2" and b"client_credentials" in request.content:
            return httpx.Response(200, json={"access_token": "com-token", "expires_in": 7200})
        if url.host.endswith(".api.greenlake.hpe.com") and path == "/compute-ops-mgmt/v1/servers":
            assert request.headers["authorization"] == "Bearer com-token"
            return httpx.Response(200, json={"total": 7, "items": []})
        if url.host == "sso.common.cloud.hpe.com" and path == "/as/token.oauth2":
            body = request.content.decode()
            assert "grant_type=authorization_code" in body and "code=abc" in body and "code_verifier=" in body
            return httpx.Response(200, json={"access_token": "at", "id_token": "it"})
        if url.host == GREENLAKE_USER_API:
            assert request.headers["authorization"] == "Bearer at"
            if path == "/authn/v1/session":
                accounts = [{"platform_customer_id": f"w{i}", "company_name": n, "account_status": "ACTIVE"} for i, n in enumerate(workspaces, 1)]
                return httpx.Response(200, json={"accounts": accounts}, headers={"set-cookie": "ccs-session=c1; Path=/"})
            if path == "/accounts/ui/v1/customer/list-accounts":
                return httpx.Response(200, json={"customers": []})
            if path.startswith("/authn/v1/session/load-account/w"):
                return httpx.Response(204)
            if path == "/ui-doorway/ui/v1/applications/provisions":
                provision = {"name": "Compute Ops Management", "region": "eu-central", "provision_status": "PROVISIONED"}
                return httpx.Response(200, json={"provisions": [provision]})
            if path == "/authn/v1/token-management/credentials" and request.method == "GET" and api_clients is not None:
                return httpx.Response(200, json=list(api_clients))
            if path == "/authn/v1/token-management/credentials" and request.method == "GET":
                stale = login._credential_prefix() + "000000000000"
                return httpx.Response(200, json=[{"credential_name": stale}, {"credential_name": "someone-elses"}])
            if path.startswith("/authn/v1/token-management/credentials/") and request.method == "DELETE":
                name = urllib.parse.unquote(path.rsplit("/", 1)[1])
                if name in refuse_delete:
                    return httpx.Response(403)
                created.append(("deleted", name))
                if api_clients is not None:
                    api_clients[:] = [c for c in api_clients if c["credential_name"] != name]
                return httpx.Response(204)
            if path == "/authn/v1/token-management/credentials" and request.method == "POST":
                created.append(("created", json.loads(request.content)["credential_name"]))
                return httpx.Response(201, json={"client_id": "cid", "client_secret": "csecret"})
        return httpx.Response(500, text=f"unexpected {request.method} {url}")

    handler.created = created
    return handler


class RecordingUI:
    def __init__(self, passwords=("right-password",)):
        self.said, self.numbers, self.errors = [], [], []
        self.passwords = list(passwords)

    def say(self, message):
        self.said.append(message)

    async def password(self, email, error):
        self.errors.append(error)
        return self.passwords.pop(0)

    async def approve(self, number, approval):
        self.numbers.append(number)
        return await approval

    async def workspace(self, names):
        return names[-1]


async def test_hpe_account_signs_in_with_push_and_saves_an_api_client(home, monkeypatch):
    handler = fake_greenlake()
    monkeypatch.setattr(login, "_transport", httpx.MockTransport(handler))
    ui = RecordingUI(passwords=())
    said = ui.said

    data = await login.login("you@hpe.com", ui)

    assert ui.numbers == [21]
    assert (data["region"], data["workspace"], data["client_id"]) == ("eu-central", "Lab", "cid")
    kinds = [kind for kind, _ in handler.created]
    assert kinds == ["deleted", "created"]  # this machine's stale client removed, other clients untouched
    assert handler.created[0][1].endswith("000000000000")

    saved = home / ".config" / "proliant-mcp" / "com" / "login.json"
    assert json.loads(saved.read_text())["client_secret"] == "csecret"
    assert "csecret" not in " ".join(said)

    account = load_com()["com"]
    assert (account.client_id, account.region, account.workspace, account.source) == ("cid", "eu-central", "Lab", str(saved))


async def test_other_accounts_use_their_password_with_retry(home, monkeypatch):
    monkeypatch.setattr(login, "_transport", httpx.MockTransport(fake_greenlake()))
    ui = RecordingUI(passwords=("wrong", "right-password"))

    data = await login.login("you@gmail.com", ui)

    assert ui.errors == [None, "Incorrect password, try again."]
    assert ui.numbers == []
    assert data["email"] == "you@gmail.com" and load_com()["com"].client_id == "cid"


async def test_locked_account_is_reported_before_any_password_retry(home, monkeypatch):
    monkeypatch.setattr(login, "_transport", httpx.MockTransport(fake_greenlake()))
    ui = RecordingUI(passwords=("right-password", "right-password", "right-password"))
    with pytest.raises(login.LoginError, match="locked"):
        await login.login("locked@gmail.com", ui)
    assert ui.errors == [None]  # one attempt: a locked account is not retried


async def test_hpe_account_without_push_falls_back_to_password(home, monkeypatch):
    monkeypatch.setattr(login, "_transport", httpx.MockTransport(fake_greenlake(push=False)))
    ui = RecordingUI(passwords=("anything",))
    with pytest.raises(login.LoginError):
        await login.login("you@hpe.com", ui)
    assert any("password instead" in line for line in ui.said)
    assert ui.errors == [None]
    assert not (home / ".config" / "proliant-mcp" / "com" / "login.json").exists()


def test_saved_login_is_used_only_when_nothing_else_configures_com(home, monkeypatch):
    login.save_login({"client_id": "saved", "client_secret": "s", "region": "us-west"})
    assert load_com()["com"].client_id == "saved"

    monkeypatch.setenv("HPECOM_CLIENT_ID", "env")
    monkeypatch.setenv("HPECOM_CLIENT_SECRET", "s")
    assert load_com()["com"].client_id == "env"


def test_logout_removes_saved_login(home, capsys):
    login.save_login({"client_id": "saved", "client_secret": "s", "region": "us-west", "credential_name": "GLP-proliant-mcp-x-1"})
    assert login.run_logout() == 0
    assert "GLP-proliant-mcp-x-1" in capsys.readouterr().out
    assert load_com() == {}


async def test_choose_workspace():
    labs = [{"company_name": "HPE Labs", "platform_customer_id": "1"}, {"company_name": "Acme", "platform_customer_id": "2"}]
    ui = RecordingUI()
    assert (await login.choose_workspace(labs[:1], None, ui))["platform_customer_id"] == "1"
    assert (await login.choose_workspace(labs, "acm", ui))["platform_customer_id"] == "2"
    assert (await login.choose_workspace(labs, None, ui))["platform_customer_id"] == "2"
    with pytest.raises(login.LoginError, match="not found"):
        await login.choose_workspace(labs, "zzzzzz", ui)


# ── com_login: the same sign-in through MCP pop-ups (elicitation) ──────────────


def chat_user(email, passwords=("right-password",), workspace="Lab"):
    """Plays the person in the chat: answers each pop-up, types the password on the local page."""
    passwords = list(passwords)
    seen = []

    async def type_password(url):
        async with httpx.AsyncClient() as browser:
            page = await browser.get(url)
            assert 'type="password"' in page.text.replace("'", '"') and email in page.text
            done = await browser.post(url, data={"password": passwords.pop(0)})
            assert "password received" in done.text.lower()

    async def handler(message, response_type, params, context):
        if isinstance(params, ElicitRequestURLParams):
            seen.append(("url", message))
            assert params.url.startswith("http://127.0.0.1:")
            await type_password(params.url)
            return ElicitResult(action="accept")
        props = params.requested_schema["properties"]
        seen.append((next(iter(props)), message))
        if "email" in props:
            return {"email": email}
        if "approved" in props:
            return {"approved": True}
        if "workspace" in props:
            assert props["workspace"]["enum"] == ["Lab", "Other"]
            return {"workspace": workspace}
        if "done" in props:  # client without URL mode: the form links to the page
            await type_password(re.search(r"http://127\.0\.0\.1:\d+/\S+", message).group(0))
            return {"done": True}
        raise AssertionError(props)

    return handler, seen


@pytest.fixture
def greenlake(home, monkeypatch):
    def use(**kwargs):
        monkeypatch.setattr(login, "_transport", httpx.MockTransport(fake_greenlake(**kwargs)))
        com._tokens.clear()

    return use


@pytest.mark.parametrize("mode", ["legacy", "auto"])  # 2025-11-25 live pop-ups / 2026-07-28 input-required rounds
async def test_com_login_hpe_account_in_chat(greenlake, mode):
    greenlake(workspaces=("Lab", "Other"))
    handler, seen = chat_user("you@hpe.com", workspace="Other")
    async with Client(srv.mcp, mode=mode, elicitation_handler=handler) as client:
        data = (await client.call_tool("com_login", {})).structured_content
    assert [kind for kind, _ in seen] == ["email", "approved", "workspace"]
    assert "tap 21" in seen[1][1]
    assert (data["status"], data["workspace"], data["region"], data["servers"]) == ("signed_in", "Other", "eu-central", 7)
    assert "csecret" not in json.dumps(data)
    async with Client(srv.mcp, mode=mode) as client:
        accounts = (await client.call_tool("list_servers", {})).data["com_accounts"]
    assert accounts[0]["workspace"] == "Other"


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_com_login_gmail_password_on_local_page(greenlake, mode):
    greenlake()
    handler, seen = chat_user("you@gmail.com", passwords=("wrong", "right-password"))
    async with Client(srv.mcp, mode=mode, elicitation_handler=handler) as client:
        data = (await client.call_tool("com_login", {})).structured_content
    assert [kind for kind, _ in seen] == ["email", "url", "url"]
    assert "Incorrect password" in seen[2][1]
    assert "right-password" not in json.dumps(data) and data["email"] == "you@gmail.com"


async def test_com_login_links_the_password_page_without_url_mode(greenlake, monkeypatch):
    greenlake()
    monkeypatch.setattr(chat, "_elicitation_modes", lambda ctx: (True, False))
    handler, seen = chat_user("you@gmail.com")
    async with Client(srv.mcp, elicitation_handler=handler) as client:
        data = (await client.call_tool("com_login", {})).structured_content
    assert [kind for kind, _ in seen] == ["email", "done"]
    assert data["status"] == "signed_in"


async def test_com_login_cancelled_or_without_popups(greenlake):
    greenlake()

    async def decline(message, response_type, params, context):
        return ElicitResult(action="decline")

    async with Client(srv.mcp, elicitation_handler=decline) as client:
        result = await client.call_tool("com_login", {}, raise_on_error=False)
    assert result.structured_content["status"] == "enter_email"  # declined pop-up: continues on a local page
    async with Client(srv.mcp) as client:
        result = await client.call_tool("com_login", {}, raise_on_error=False)
    assert result.structured_content["status"] == "enter_email"
    assert not (login.com_login_path()).exists()


async def test_com_login_with_email_falls_back_to_no_popup_signin(greenlake):
    greenlake()
    async with Client(srv.mcp) as client:
        result = (await client.call_tool("com_login", {"email": "you@hpe.com"})).structured_content
        assert result["status"] == "approve_push" and "without pop-ups" in result["note"]
        done = (await client.call_tool("com_login_status", {"workspace": "Other"})).structured_content
        if done.get("status") == "choose_workspace":
            done = (await client.call_tool("com_login_status", {"workspace": "Other"})).structured_content
    assert done["status"] in ("signed_in", "choose_workspace", "approve_push")


def test_api_client_names_are_unique_per_install(home, monkeypatch):
    monkeypatch.setattr(login.socket, "gethostname", lambda: "SAMEHOST")
    first = login._credential_prefix()
    assert login._credential_prefix() == first  # stable for this install
    other = home / "other-install"
    monkeypatch.setenv("HOME", str(other))
    monkeypatch.setenv("USERPROFILE", str(other))
    second = login._credential_prefix()
    assert first.startswith("GLP-proliant-mcp-SAMEHOST-") and second.startswith("GLP-proliant-mcp-SAMEHOST-")
    assert first != second  # same hostname, different install: cleanup of one never matches the other


# ── com_login_start / com_login_status: sign-in without pop-ups ───────────────

async def test_relay_sign_in_hpe_account_with_workspace_choice(greenlake):
    greenlake(workspaces=("Lab", "Other"))
    async with Client(srv.mcp) as client:  # no elicitation handler: a client without pop-ups
        first = (await client.call_tool("com_login_start", {"email": "you@hpe.com"})).structured_content
        assert (first["status"], first["number"]) == ("approve_push", 21) and "tap 21" in first["next_step"]
        step = (await client.call_tool("com_login_status", {})).structured_content
        assert (step["status"], step["workspaces"]) == ("choose_workspace", ["Lab", "Other"])
        done = (await client.call_tool("com_login_status", {"workspace": "Other"})).structured_content
        listed = (await client.call_tool("list_servers", {})).data["com_accounts"]
    assert (done["status"], done["workspace"], done["servers"]) == ("signed_in", "Other", 7)
    assert listed[0]["workspace"] == "Other" and "csecret" not in json.dumps(done)


async def test_com_login_without_email_or_popups_asks_for_it_on_a_local_page(greenlake):
    greenlake()
    async with Client(srv.mcp) as client, httpx.AsyncClient() as browser:
        first = (await client.call_tool("com_login", {})).structured_content
        assert first["status"] == "enter_email" and first["url"].startswith("http://127.0.0.1:")
        email_page = await browser.get(first["url"])
        assert "type='email'" in email_page.text and "id='reveal'" not in email_page.text
        assert "script-src" not in email_page.headers["content-security-policy"]
        await browser.post(first["url"], data={"password": "you@hpe.com"})
        step = (await client.call_tool("com_login_status", {})).structured_content
    assert (step["status"], step["email"]) == ("signed_in", "you@hpe.com")


async def test_relay_sign_in_password_on_local_page(greenlake):
    greenlake()
    async with Client(srv.mcp) as client, httpx.AsyncClient() as browser:
        first = (await client.call_tool("com_login_start", {"email": "you@gmail.com"})).structured_content
        assert first["status"] == "enter_password" and first["url"].startswith("http://127.0.0.1:")
        await browser.post(first["url"], data={"password": "wrong"})
        retry = (await client.call_tool("com_login_status", {})).structured_content
        assert retry["status"] == "enter_password" and "Incorrect password" in retry["next_step"] and retry["url"] == first["url"]  # one page for every step
        await browser.post(retry["url"], data={"password": "right-password"})
        done = (await client.call_tool("com_login_status", {})).structured_content
    assert done["status"] == "signed_in" and "right-password" not in json.dumps(done)


async def test_relay_status_says_when_nothing_changed(greenlake, monkeypatch):
    greenlake(workspaces=("Lab", "Other"))
    monkeypatch.setattr(chat_login, "STATUS_WAIT", 0.2)
    async with Client(srv.mcp) as client:
        await client.call_tool("com_login_start", {"email": "you@hpe.com"})
        await client.call_tool("com_login_status", {})  # now choose_workspace
        again = (await client.call_tool("com_login_status", {})).structured_content
        assert again["status"] == "choose_workspace" and again["waiting"] is True
        bad = await client.call_tool("com_login_start", {"email": "not-an-email"}, raise_on_error=False)
    assert bad.is_error and "email" in bad.content[0].text


async def test_relay_sign_in_runs_on_one_page_with_a_clickable_workspace(greenlake):
    greenlake(workspaces=("Lab", "Other"))
    async with Client(srv.mcp) as client, httpx.AsyncClient() as browser:
        first = (await client.call_tool("com_login_start", {"email": "you@hpe.com"})).structured_content
        assert "Do not ask the user to confirm" in first["next_step"]
        step = (await client.call_tool("com_login_status", {})).structured_content
        assert step["status"] == "choose_workspace" and step["url"] == first["url"]
        page = (await browser.get(step["url"])).text
        assert "type='radio' name='choice' value='Other'" in page
        await browser.post(step["url"], data={"step": "workspace", "choice": "Other"})
        done = (await client.call_tool("com_login_status", {})).structured_content
        final = (await browser.get(step["url"])).text
    assert (done["status"], done["workspace"]) == ("signed_in", "Other")
    assert "Signed in to <b>Other</b>" in final and "refresh" not in final


# ── list and delete the user's personal API clients ───────────────────────────

MINE = "GLP-proliant-mcp-HSTHJMA02-1965c8-261118004207"


def api_clients_fixture(home):
    login.save_login({"client_id": "saved", "client_secret": "s", "region": "us-west", "credential_name": MINE, "email": "you@hpe.com"})
    return [
        {"credential_name": MINE, "client_id": "id-mine"},
        {"credential_name": "GLP-proliant-mcp-WIN11-1a6598-261120144148", "client_id": "id-win-1"},
        {"credential_name": "GLP-proliant-mcp-WIN11-5800a7-261118220322", "client_id": "id-win-2"},
        {"credential_name": "terraform-ci", "client_id": "id-tf"},
    ]


async def next_step(client, step, wanted):
    """Follow start/status until the flow reaches *wanted* (the fake sign-in may finish before start returns)."""
    for _ in range(5):
        if step["status"] == wanted:
            return step
        step = (await client.call_tool("greenlake_api_clients_status", {})).structured_content
    raise AssertionError(f"never reached {wanted}: {step}")


async def test_list_api_clients_flags_this_computer_and_foreign_ones(home, greenlake):
    clients = api_clients_fixture(home)
    greenlake(api_clients=clients)
    async with Client(srv.mcp) as client:  # the saved login's email is used, so no email page
        first = (await client.call_tool("greenlake_list_api_clients_start", {})).structured_content
        assert (first["status"], first["number"]) == ("approve_push", 21) and "greenlake_api_clients_status" in first["next_step"]
        done = await next_step(client, first, "listed")
    assert (done["status"], done["count"]) == ("listed", 4)
    by_name = {c["name"]: c for c in done["clients"]}
    assert by_name[MINE]["note"].startswith("used by this computer")
    assert by_name["terraform-ci"]["note"] == "not created by proliant-mcp"
    assert by_name["GLP-proliant-mcp-WIN11-1a6598-261120144148"]["created"] == "2026-11-20 14:41 (from the name)"
    assert "secret" not in json.dumps(done) and len(clients) == 4  # read-only


async def test_delete_api_clients_only_what_the_user_ticks_and_confirms(home, greenlake):
    clients = api_clients_fixture(home)
    greenlake(api_clients=clients)
    async with Client(srv.mcp) as client, httpx.AsyncClient() as browser:
        step = await next_step(client, (await client.call_tool("greenlake_delete_api_clients_start", {"preselect": ["win11", MINE[:20]]})).structured_content, "select_api_clients")
        assert step["status"] == "select_api_clients" and "Do not choose for them" in step["next_step"]
        page = (await browser.get(step["url"])).text
        assert page.count("checked") == 2  # the two WIN11 clients; this computer's client is never pre-ticked
        assert "Used by this computer" in page and "Not created by proliant-mcp" in page

        nothing = await browser.post(step["url"], data={"step": "select", "action": "review"}, follow_redirects=True)
        assert "Tick at least one" in nothing.text and len(clients) == 4

        await browser.post(step["url"], data={"step": "select", "action": "review", "selected_1": "1", "selected_3": "1"})
        status = (await client.call_tool("greenlake_api_clients_status", {})).structured_content
        assert status["status"] == "confirm_deletion" and len(status["selected"]) == 2
        assert len(clients) == 4  # nothing is deleted before the confirmation
        confirm = (await browser.get(step["url"])).text
        assert "Permanently delete 2 API clients" in confirm and "cannot be undone" in confirm

        await browser.post(step["url"], data={"step": "confirm", "action": "delete"})
        done = (await client.call_tool("greenlake_api_clients_status", {})).structured_content
    assert done["status"] == "deleted" and done["remaining"] == 2 and not done["failed"]
    assert sorted(done["deleted"]) == sorted(["terraform-ci", "GLP-proliant-mcp-WIN11-1a6598-261120144148"])
    assert {c["credential_name"] for c in clients} == {MINE, "GLP-proliant-mcp-WIN11-5800a7-261118220322"}
    assert "note" not in done  # this computer's login was not touched


async def test_delete_reports_refusals_warns_about_this_computers_login_and_can_be_cancelled(home, greenlake):
    clients = api_clients_fixture(home)
    greenlake(api_clients=clients, refuse_delete={"terraform-ci"})
    async with Client(srv.mcp) as client, httpx.AsyncClient() as browser:
        step = await next_step(client, (await client.call_tool("greenlake_delete_api_clients_start", {})).structured_content, "select_api_clients")
        await browser.post(step["url"], data={"step": "select", "action": "review", "selected_0": "1", "selected_3": "1"})
        await client.call_tool("greenlake_api_clients_status", {})
        assert "includes the API client used by this computer" in (await browser.get(step["url"])).text
        await browser.post(step["url"], data={"step": "confirm", "action": "back"})
        again = (await client.call_tool("greenlake_api_clients_status", {})).structured_content
        assert again["status"] == "select_api_clients"
        await browser.post(step["url"], data={"step": "select", "action": "review", "selected_0": "1", "selected_3": "1"})
        await client.call_tool("greenlake_api_clients_status", {})
        await browser.post(step["url"], data={"step": "confirm", "action": "delete"})
        done = (await client.call_tool("greenlake_api_clients_status", {})).structured_content
        assert done["deleted"] == [MINE] and done["failed"] == [{"name": "terraform-ci", "reason": "GreenLake refused (HTTP 403)"}]
        assert "saved GreenLake login was deleted" in done["note"]

        step = await next_step(client, (await client.call_tool("greenlake_delete_api_clients_start", {})).structured_content, "select_api_clients")
        await browser.post(step["url"], data={"step": "select", "action": "cancel", "selected_0": "1"})
        cancelled = (await client.call_tool("greenlake_api_clients_status", {})).structured_content
    assert cancelled["status"] == "cancelled" and cancelled["deleted"] == []
    assert len(clients) == 3  # only the one successful deletion happened


async def test_creating_an_api_client_over_the_limit_points_to_the_delete_tool(home, monkeypatch):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(400)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(login, "_transport", httpx.MockTransport(handler))
    with pytest.raises(login.LoginError, match="greenlake_delete_api_clients_start"):
        await login._create_api_client("at", "c1")
