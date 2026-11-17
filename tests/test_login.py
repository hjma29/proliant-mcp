import json

import httpx
import pytest

from proliant_mcp import login
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


def fake_greenlake(*, push=True, pending_polls=1):
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
        if path == "/idp/idx/identify":
            assert json.loads(request.content)["identifier"] == "you@example.com"
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
        if url.host == "sso.common.cloud.hpe.com" and path == "/as/token.oauth2":
            body = request.content.decode()
            assert "grant_type=authorization_code" in body and "code=abc" in body and "code_verifier=" in body
            return httpx.Response(200, json={"access_token": "at", "id_token": "it"})
        if url.host == GREENLAKE_USER_API:
            assert request.headers["authorization"] == "Bearer at"
            if path == "/authn/v1/session":
                account = {"platform_customer_id": "w1", "company_name": "Lab", "account_status": "ACTIVE"}
                return httpx.Response(200, json={"accounts": [account]}, headers={"set-cookie": "ccs-session=c1; Path=/"})
            if path == "/accounts/ui/v1/customer/list-accounts":
                return httpx.Response(200, json={"customers": []})
            if path == "/authn/v1/session/load-account/w1":
                return httpx.Response(204)
            if path == "/ui-doorway/ui/v1/applications/provisions":
                provision = {"name": "Compute Ops Management", "region": "eu-central", "provision_status": "PROVISIONED"}
                return httpx.Response(200, json={"provisions": [provision]})
            if path == "/authn/v1/token-management/credentials" and request.method == "GET":
                stale = login._credential_prefix() + "000000000000"
                return httpx.Response(200, json=[{"credential_name": stale}, {"credential_name": "someone-elses"}])
            if path.startswith("/authn/v1/token-management/credentials/") and request.method == "DELETE":
                created.append(("deleted", path.rsplit("/", 1)[1]))
                return httpx.Response(204)
            if path == "/authn/v1/token-management/credentials" and request.method == "POST":
                created.append(("created", json.loads(request.content)["credential_name"]))
                return httpx.Response(201, json={"client_id": "cid", "client_secret": "csecret"})
        return httpx.Response(500, text=f"unexpected {request.method} {url}")

    handler.created = created
    return handler


async def test_login_signs_in_with_push_and_saves_an_api_client(home, monkeypatch):
    handler = fake_greenlake()
    monkeypatch.setattr(login, "_transport", httpx.MockTransport(handler))
    said = []

    data = await login.login("you@example.com", say=said.append)

    assert any("tap 21" in line for line in said)
    assert (data["region"], data["workspace"], data["client_id"]) == ("eu-central", "Lab", "cid")
    kinds = [kind for kind, _ in handler.created]
    assert kinds == ["deleted", "created"]  # this machine's stale client removed, other clients untouched
    assert handler.created[0][1].endswith("000000000000")

    saved = home / ".config" / "proliant-mcp" / "com" / "login.json"
    assert json.loads(saved.read_text())["client_secret"] == "csecret"
    assert "csecret" not in " ".join(said)

    account = load_com()["com"]
    assert (account.client_id, account.region, account.workspace, account.source) == ("cid", "eu-central", "Lab", str(saved))


async def test_login_without_okta_verify_push_points_to_api_client(home, monkeypatch):
    monkeypatch.setattr(login, "_transport", httpx.MockTransport(fake_greenlake(push=False)))
    with pytest.raises(login.PushNotAvailable, match="API client"):
        await login.login("you@example.com", say=lambda _: None)
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


def test_choose_workspace():
    labs = [{"company_name": "HPE Labs", "platform_customer_id": "1"}, {"company_name": "Acme", "platform_customer_id": "2"}]
    assert login.choose_workspace(labs[:1], None, None)["platform_customer_id"] == "1"
    assert login.choose_workspace(labs, "acm", None)["platform_customer_id"] == "2"
    assert login.choose_workspace(labs, None, lambda names: names[0])["platform_customer_id"] == "1"
    with pytest.raises(login.LoginError, match="--workspace"):
        login.choose_workspace(labs, None, None)
    with pytest.raises(login.LoginError, match="not found"):
        login.choose_workspace(labs, "zzzzzz", None)
