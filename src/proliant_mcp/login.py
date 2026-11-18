"""Sign in to HPE GreenLake with your email, the way the GreenLake web page does, so no
API client has to be created by hand.

- ``@hpe.com`` accounts approve an Okta Verify push (tap the number shown); accounts
  without push fall back to their password.
- Other accounts (e.g. gmail.com) use their GreenLake password, then an Okta Verify
  push if Okta asks for a second factor.

Then it opens a workspace session, creates a temporary GreenLake API client in the
chosen workspace and saves its ID and secret to ``~/.config/proliant-mcp/com/login.json``.
The Compute Ops Management tools use that client like any other API client. The email,
password and session tokens are never stored.

The same engine serves the ``com_login`` MCP tool (pop-ups in the AI chat, see
``chat_login.py``) and the ``proliant-mcp login`` terminal command (``TerminalUI``).

The sign-in endpoints are the web UI's, not a published API (ported from proliant-cli's
``proliant.com.login``). If HPE changes them, create an API client instead (README).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import difflib
import getpass
import hashlib
import json
import os
import re
import secrets
import socket
import sys
import time
import urllib.parse
from collections.abc import Callable
from html import unescape
from typing import Any, Protocol

import httpx

from proliant_mcp.com import ComClient, ComError
from proliant_mcp.config import COM_REGIONS, ComAccount, com_login_path

GL_COMMON_URL = "https://common.cloud.hpe.com"
SSO_URL = "https://sso.common.cloud.hpe.com"
USER_API_BASE = "https://aquila-user-api.common.cloud.hpe.com"
REDIRECT_URI = f"{GL_COMMON_URL}/authentication/callback"
CLIENT_ID = "aquila-user-auth"
CREDENTIALS_URI = f"{USER_API_BASE}/authn/v1/token-management/credentials"
GLP_APP_INSTANCE_ID = "00000000-0000-0000-0000-000000000000"
CRED_NAME_PREFIX = "GLP-proliant-mcp"

POLL_INTERVAL = 2
POLL_TIMEOUT = 300

IDX_HEADERS = {
    "Content-Type": "application/json; okta-version=1.0.0",
    "Accept": "application/ion+json; okta-version=1.0.0",
}
# Plain JSON for password sign-in: with the ion+json headers Okta routes non-HPE accounts
# to certificate (MTLS) sign-in instead of offering the password authenticator.
CLASSIC_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

# Tests swap this to inject a fake GreenLake / Okta (httpx.MockTransport).
_transport: httpx.AsyncBaseTransport | None = None

Say = Callable[[str], None]


class LoginError(RuntimeError):
    pass


class PushNotAvailable(LoginError):
    pass


class WrongPassword(LoginError):
    pass


class LoginUI(Protocol):
    """How the sign-in talks to the person: a terminal, or pop-ups in an AI chat."""

    def say(self, message: str) -> None: ...

    async def password(self, email: str, error: str | None) -> str:
        """Ask for the GreenLake password (never echoed, never stored)."""

    async def approve(self, number: Any, approval: asyncio.Task[str]) -> str:
        """Show the number to tap in Okta Verify; return the approval task's result."""

    async def workspace(self, names: list[str]) -> str:
        """Pick one of several workspaces."""


def _say(message: str = "") -> None:
    print(message, flush=True)


class TerminalUI:
    def say(self, message: str) -> None:
        _say(message)

    async def password(self, email: str, error: str | None) -> str:
        if error:
            _say(error)
        return await asyncio.to_thread(getpass.getpass, f"GreenLake password for {email}: ")

    async def approve(self, number: Any, approval: asyncio.Task[str]) -> str:
        _say("")
        _say(f"Open Okta Verify on your phone and tap {number}." if number is not None else "Approve the push notification in Okta Verify on your phone.")
        _say("Waiting for approval...")
        return await approval

    async def workspace(self, names: list[str]) -> str:
        if not sys.stdin.isatty():
            raise LoginError(f"Several workspaces ({', '.join(names)}); pass --workspace NAME")
        return await asyncio.to_thread(_ask_workspace, names)


def _client(**kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport, **kwargs)


def _session_headers(access_token: str, ccs_session: str, **extra: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json", **extra}
    if ccs_session:
        headers["Cookie"] = f"ccs-session={ccs_session}"
    return headers


# ── PKCE and page scraping ────────────────────────────────────────────────────

def _pkce() -> tuple[str, str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge, secrets.token_urlsafe(16)


def _decode_js_str(raw: str) -> str:
    """Decode the JavaScript \\xNN and \\uNNNN escapes Okta uses inside page scripts."""
    out, i = [], 0
    while i < len(raw):
        if raw[i : i + 2] == r"\x" and i + 4 <= len(raw):
            out.append(chr(int(raw[i + 2 : i + 4], 16)))
            i += 4
        elif raw[i : i + 2] == r"\u" and i + 6 <= len(raw):
            out.append(chr(int(raw[i + 2 : i + 6], 16)))
            i += 6
        else:
            out.append(raw[i])
            i += 1
    return "".join(out)


def _find_state_token(html: str) -> str | None:
    for pattern in (
        r"var\s+stateToken\s*=\s*'([^']+)'",
        r'var\s+stateToken\s*=\s*"([^"]+)"',
        r'"stateToken"\s*:\s*"([^"]+)"',
    ):
        match = re.search(pattern, html)
        if match:
            return _decode_js_str(match.group(1))
    return None


def _form_fields(html: str) -> tuple[str, dict[str, str]] | None:
    """(action, fields) of the first HTML form, as a browser would submit it."""
    action = re.search(r'<form[^>]*action="([^"]+)"', html, re.IGNORECASE)
    if not action:
        return None
    fields = re.findall(r'<input[^>]+name="([^"]*)"[^>]*value="([^"]*)"', html, re.IGNORECASE)
    return unescape(action.group(1)), {name: unescape(value) for name, value in fields}


# ── Okta sign-in ──────────────────────────────────────────────────────────────

async def _fetch_settings() -> dict[str, Any]:
    try:
        async with _client(timeout=15) as client:
            resp = await client.get(f"{GL_COMMON_URL}/settings.json")
            if resp.status_code == 200:
                return resp.json()
    except (httpx.HTTPError, ValueError):
        pass
    return {}


async def _get_state_token(client: httpx.AsyncClient, challenge: str, state: str, email: str) -> tuple[str, str]:
    """Start the PKCE sign-in; return the Okta stateToken and the Okta base URL.

    GreenLake fronts Okta with an SSO broker whose web page normally resolves the user's
    identity provider from the email. We call that resolve step ourselves.
    """
    settings = await _fetch_settings()
    authority = settings.get("authorityURL", SSO_URL).rstrip("/")
    org_api = settings.get("orgApiGw", "https://aquila-org-api.common.cloud.hpe.com").rstrip("/")
    params = {
        "client_id": settings.get("client_id", CLIENT_ID),
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "openid profile email",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": secrets.token_urlsafe(16),
    }
    resp = await client.get(f"{authority}/as/authorization.oauth2?{urllib.parse.urlencode(params)}", follow_redirects=True)
    state_token = _find_state_token(resp.text)
    if not state_token:
        track = re.search(r"[?&]track-id=([^&]+)", str(resp.url))
        if track:
            resolve = (
                f"{org_api}/internal-identity/v1alpha2/sso-resolve"
                f"?login_hint={urllib.parse.quote(email)}&track-id={urllib.parse.quote(urllib.parse.unquote(track.group(1)))}"
            )
            resp = await client.get(resolve, follow_redirects=True)
            state_token = _find_state_token(resp.text)
    if not state_token:
        raise LoginError(f"GreenLake sign-in did not return the expected page ({resp.url})")
    return state_token, str(resp.url).split("/oauth2/")[0]


async def _idx_introspect(client: httpx.AsyncClient, okta_base: str, state_token: str,
                          headers: dict[str, str] = IDX_HEADERS) -> dict[str, Any] | None:
    resp = await client.post(f"{okta_base}/idp/idx/introspect", json={"stateToken": state_token}, headers=headers)
    data = resp.json()
    return data if "stateHandle" in data else None


def _remediation(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((v for v in data.get("remediation", {}).get("value", []) if v.get("name") == name), None)


async def _follow_saml_to_workforce(client: httpx.AsyncClient, redirect_idp: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Corporate SSO (HPE employees): follow the redirect to the workforce Okta."""
    page = await client.get(redirect_idp["href"])
    form = _form_fields(page.text)
    if not form:
        token = re.search(r'"stateToken"\s*:\s*"([^"]+)"', page.text)
        if not token:
            raise LoginError("No SAML form or Okta stateToken in the identity provider redirect")
        okta_base = str(page.url).split("/sso/")[0]
        data = await _idx_introspect(client, okta_base, _decode_js_str(token.group(1)))
        if data is None:
            raise LoginError("The sign-in session expired")
        return data, okta_base

    action, fields = form
    resp = await client.post(action, data=fields)
    jump = re.search(r"window\.location\.href\s*=\s*'([^']+)'", resp.text)
    if not jump:
        raise LoginError("No step-up redirect in the corporate SSO response")
    parsed = urllib.parse.urlparse(_decode_js_str(jump.group(1)))
    token = urllib.parse.parse_qs(parsed.query).get("stateToken", [None])[0]
    if not token:
        raise LoginError("No stateToken in the corporate SSO redirect")
    okta_base = f"{parsed.scheme}://{parsed.netloc}"
    data = await _idx_introspect(client, okta_base, token)
    if data is None:
        raise LoginError("The sign-in session expired")
    return data, okta_base


def _remediation_names(data: dict[str, Any]) -> list[str]:
    return [v.get("name", "") for v in data.get("remediation", {}).get("value", [])]


def _success_href(data: dict[str, Any]) -> str | None:
    return (
        (data.get("success") or {}).get("href")
        or (data.get("successWithInteractionCode") or {}).get("href")
        or (data.get("successWithInteractionCode") or {}).get("redirectUri")
    )


async def _identify_for_push(client: httpx.AsyncClient, email: str, challenge: str, state: str) -> dict[str, Any]:
    """Identify the user; return the IDX state that offers authenticators (after corporate SSO, if any)."""
    state_token, okta_base = await _get_state_token(client, challenge, state, email)
    idx = await _idx_introspect(client, okta_base, state_token)
    if idx is None:
        raise LoginError("The sign-in session expired")
    identify = _remediation(idx, "identify")
    if not identify:
        raise LoginError("GreenLake did not offer an email sign-in")
    identified = (
        await client.post(identify["href"], json={"identifier": email, "stateHandle": idx["stateHandle"]}, headers=IDX_HEADERS)
    ).json()
    if "remediation" not in identified:
        raise LoginError("GreenLake did not accept that email address")
    redirect_idp = _remediation(identified, "redirect-idp")
    if redirect_idp:
        identified, _ = await _follow_saml_to_workforce(client, redirect_idp)
    return identified


async def _select_push(client: httpx.AsyncClient, idx: dict[str, Any]) -> tuple[str, str, Any]:
    """Send the Okta Verify push. Returns (poll href, state handle, number to tap)."""
    select = _remediation(idx, "select-authenticator-authenticate")
    authenticators = idx.get("authenticators", {}).get("value", [])
    verify = next(
        (a for a in authenticators if "okta_verify" in a.get("key", "").lower() or "okta verify" in a.get("displayName", "").lower()),
        None,
    )
    methods = [m.get("type") for m in (verify or {}).get("methods", [])]
    if not select or not verify or "push" not in methods:
        raise PushNotAvailable("Okta Verify push is not set up for this account")
    chosen = (
        await client.post(
            select["href"],
            json={"authenticator": {"id": verify["id"], "methodType": "push"}, "stateHandle": idx["stateHandle"]},
            headers=IDX_HEADERS,
        )
    ).json()
    poll = _remediation(chosen, "challenge-poll")
    if not poll:
        raise LoginError("GreenLake did not start the Okta Verify push")
    answer = chosen.get("currentAuthenticator", {}).get("value", {}).get("contextualData", {}).get("correctAnswer")
    return poll["href"], chosen["stateHandle"], answer


async def _approve_push(client: httpx.AsyncClient, idx: dict[str, Any], ui: LoginUI) -> str:
    """Send the push, show the number and wait for approval. Returns the success href."""
    poll_href, handle, answer = await _select_push(client, idx)
    approval = asyncio.create_task(_poll_push(client, poll_href, handle))
    try:
        return await ui.approve(answer, approval)
    finally:
        if not approval.done():
            approval.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await approval


def _check_password_reply(resp: httpx.Response) -> dict[str, Any]:
    """Raise WrongPassword for Okta's 'incorrect password' answers (HTTP 401 or 200 with an error)."""
    try:
        data = resp.json()
    except ValueError:
        data = {}
    messages = data.get("messages", {}).get("value", [])
    keys = {m.get("i18n", {}).get("key", "") for m in messages} | {"errors." + data.get("errorCode", "")}
    if resp.status_code == 401 or keys & {"errors.E0000004", "errors.E0000207"} or any(m.get("class") == "ERROR" for m in messages):
        raise WrongPassword("Incorrect password, try again.")
    return data


async def _password_challenge(client: httpx.AsyncClient, okta_base: str, idx: dict[str, Any], password: str, ui: LoginUI) -> str:
    """Password via the select/challenge steps (corporate Okta or newer Okta). Returns the success href."""
    select = _remediation(idx, "select-authenticator-authenticate")
    authenticators = idx.get("authenticators", {}).get("value", [])
    password_auth = next(
        (a for a in authenticators if a.get("key") == "okta_password" or "password" in a.get("displayName", "").lower()),
        None,
    )
    if not select or not password_auth:
        raise LoginError("GreenLake does not offer password sign-in for this account")
    chosen = (
        await client.post(select["href"], json={"authenticator": {"id": password_auth["id"]}, "stateHandle": idx["stateHandle"]}, headers=IDX_HEADERS)
    ).json()
    challenge = _remediation(chosen, "challenge-authenticator")
    if not challenge:
        raise LoginError("GreenLake did not ask for the password")
    reply = await client.post(challenge["href"], json={"credentials": {"passcode": password}, "stateHandle": chosen["stateHandle"]}, headers=IDX_HEADERS)
    data = _check_password_reply(reply)
    if href := _success_href(data):
        return href
    if _remediation(data, "select-authenticator-authenticate"):
        ui.say("Password accepted; GreenLake also wants an Okta Verify push.")
        return await _approve_push(client, data, ui)
    raise LoginError(f"Unexpected GreenLake answer after the password ({_remediation_names(data)})")


async def _password_sign_in(client: httpx.AsyncClient, email: str, password: str, challenge: str, state: str, ui: LoginUI) -> str:
    """Email + password (the path for gmail.com and other non-HPE accounts). Returns the success href."""
    state_token, okta_base = await _get_state_token(client, challenge, state, email)
    idx = await _idx_introspect(client, okta_base, state_token, CLASSIC_HEADERS)
    if idx is None:
        raise LoginError("The sign-in session expired")
    identify = _remediation(idx, "identify")
    identify_href = identify["href"] if identify else f"{okta_base}/idp/idx/identify"
    identified = (
        await client.post(identify_href, json={"identifier": email, "stateHandle": idx["stateHandle"]}, headers=CLASSIC_HEADERS)
    ).json()
    names = _remediation_names(identified)
    authenticators = identified.get("authenticators", {}).get("value", [])
    password_auth = next((a for a in authenticators if a.get("key") == "okta_password" or a.get("type") == "password"), None)

    if password_auth and "challenge-authenticator" in names:
        with contextlib.suppress(httpx.HTTPError):
            await client.post(f"{okta_base}/api/v1/internal/device/nonce", headers=CLASSIC_HEADERS)
        reply = await client.post(
            _remediation(identified, "challenge-authenticator")["href"],
            json={"credentials": {"passcode": password}, "stateHandle": identified["stateHandle"]},
            headers=CLASSIC_HEADERS,
        )
        data = _check_password_reply(reply)
        if href := _success_href(data):
            return href
        if _remediation(data, "select-authenticator-authenticate"):
            ui.say("Password accepted; GreenLake also wants an Okta Verify push.")
            return await _approve_push(client, data, ui)
        raise LoginError(f"Unexpected GreenLake answer after the password ({_remediation_names(data)})")

    if "redirect-idp" in names:
        idx, okta_base = await _follow_saml_to_workforce(client, _remediation(identified, "redirect-idp"))
        again = _remediation(idx, "identify")
        if not _remediation(idx, "select-authenticator-authenticate") and again:
            idx = (await client.post(again["href"], json={"identifier": email, "stateHandle": idx["stateHandle"]}, headers=IDX_HEADERS)).json()
        return await _password_challenge(client, okta_base, idx, password, ui)

    if "select-authenticator-authenticate" in names:
        return await _password_challenge(client, okta_base, identified, password, ui)

    if {"enroll-authenticator", "select-authenticator-enroll"} & set(names):
        raise LoginError("This account has no GreenLake password yet. Sign in to the GreenLake web page once to set one.")
    raise LoginError(f"GreenLake did not offer password sign-in for this account ({names})")


async def _poll_push(client: httpx.AsyncClient, poll_href: str, state_handle: str, timeout: float = POLL_TIMEOUT) -> str:
    deadline = time.monotonic() + timeout
    while True:
        data = (await client.post(poll_href, json={"stateHandle": state_handle}, headers=IDX_HEADERS)).json()
        messages = data.get("messages", {}).get("value", [])
        if any(m.get("class") == "ERROR" for m in messages):
            raise LoginError(f"Okta Verify push was denied: {messages[0].get('message', 'no reason given')}")
        href = (data.get("success") or {}).get("href") or (data.get("successWithInteractionCode") or {}).get("href")
        if href:
            return href
        if _remediation(data, "challenge-authenticator"):
            raise LoginError("This account needs a second factor on top of the push, which is not supported. Use an API client instead.")
        if time.monotonic() >= deadline:
            raise LoginError(f"No Okta Verify approval within {int(timeout)} seconds")
        await asyncio.sleep(POLL_INTERVAL)


async def _extract_code(client: httpx.AsyncClient, start_url: str) -> str:
    """Follow the redirects after the push is approved until the OAuth authorization code appears."""
    resp = await client.get(start_url, follow_redirects=True)

    def code_in(url: Any) -> str | None:
        return urllib.parse.parse_qs(urllib.parse.urlparse(str(url)).query).get("code", [None])[0]

    if code := code_in(resp.url):
        return code
    form = _form_fields(resp.text)
    if not form:
        raise LoginError(f"Unexpected page after the push was approved ({resp.url})")
    action, fields = form
    resp = await client.post(action, data=fields, follow_redirects=True)
    if code := code_in(resp.url):
        return code
    token = re.search(r'"stateToken"\s*:\s*"([^"]+)"', resp.text)
    if not token:
        raise LoginError("No sign-in state after the identity provider response")
    data = await _idx_introspect(client, "https://auth.hpe.com", _decode_js_str(token.group(1)))
    href = ((data or {}).get("success") or {}).get("href") or ((data or {}).get("successWithInteractionCode") or {}).get("href")
    if not href:
        raise LoginError("GreenLake did not finish the sign-in")
    final = await client.get(href, follow_redirects=True)
    if code := code_in(final.url):
        return code
    raise LoginError("GreenLake did not return an authorization code")


async def _exchange_token(verifier: str, code: str) -> dict[str, Any]:
    async with _client(timeout=15) as client:
        resp = await client.post(
            f"{SSO_URL}/as/token.oauth2",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI, "code_verifier": verifier, "client_id": CLIENT_ID},
        )
    if resp.status_code != 200:
        raise LoginError(f"GreenLake token exchange failed (HTTP {resp.status_code})")
    return resp.json()


async def _finish(client: httpx.AsyncClient, verifier: str, success_href: str) -> dict[str, Any]:
    code = await _extract_code(client, success_href)
    return await _exchange_token(verifier, code)


async def _push_login(email: str, ui: LoginUI) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(2)
        verifier, challenge, state = _pkce()
        async with _client(follow_redirects=True, timeout=20) as client:
            try:
                idx = await _identify_for_push(client, email, challenge, state)
            except (LoginError, httpx.HTTPError, ValueError, KeyError) as exc:
                last = exc
                continue
            return await _finish(client, verifier, await _approve_push(client, idx, ui))
    raise LoginError(f"Could not start the GreenLake sign-in: {last}")


async def _password_login(email: str, ui: LoginUI) -> dict[str, Any]:
    error: str | None = None
    for _ in range(3):
        password = await ui.password(email, error)
        if not password:
            raise LoginError("No password entered")
        verifier, challenge, state = _pkce()
        async with _client(follow_redirects=True, timeout=20) as client:
            try:
                href = await _password_sign_in(client, email, password, challenge, state, ui)
            except WrongPassword as exc:
                error = str(exc)
                continue
            return await _finish(client, verifier, href)
    raise LoginError("Incorrect password three times")


async def sign_in(email: str, ui: LoginUI) -> dict[str, Any]:
    """Returns GreenLake's token response (kept in memory only)."""
    if email.lower().endswith("@hpe.com"):
        try:
            return await _push_login(email, ui)
        except PushNotAvailable:
            ui.say("Okta Verify push is not set up for this account; signing in with your password instead.")
    return await _password_login(email, ui)


# ── Workspace, region and API client ──────────────────────────────────────────

async def _fetch_list_accounts(access_token: str, ccs_session: str) -> list[dict[str, Any]]:
    """Self-service workspaces; /authn/v1/session only lists the ones you were invited to."""
    if not ccs_session:
        return []
    try:
        async with _client(timeout=15) as client:
            resp = await client.get(
                f"{USER_API_BASE}/accounts/ui/v1/customer/list-accounts",
                params={"count_per_page": 50},
                headers=_session_headers(access_token, ccs_session),
            )
        return (resp.json().get("customers") or []) if resp.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return []


async def _init_workspace_session(access_token: str, id_token: str) -> tuple[list[dict[str, Any]], str]:
    async with _client(timeout=15) as client:
        resp = await client.post(
            f"{USER_API_BASE}/authn/v1/session",
            json={"id_token": id_token},
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            raise LoginError(f"GreenLake workspace session failed (HTTP {resp.status_code})")
        workspaces = [w for w in resp.json().get("accounts", []) if w.get("account_status") == "ACTIVE"]
        ccs_session = next((c.value for c in client.cookies.jar if c.name == "ccs-session"), "")
    seen = {w.get("platform_customer_id") for w in workspaces}
    for extra in await _fetch_list_accounts(access_token, ccs_session):
        if extra.get("account_status") == "ACTIVE" and extra.get("platform_customer_id") not in seen:
            workspaces.append(extra)
    if not workspaces:
        raise LoginError("No active workspaces in this GreenLake account")
    return workspaces, ccs_session


async def _load_workspace(access_token: str, workspace_id: str, ccs_session: str) -> str:
    async with _client(timeout=15) as client:
        resp = await client.get(
            f"{USER_API_BASE}/authn/v1/session/load-account/{workspace_id}",
            headers=_session_headers(access_token, ccs_session),
        )
        if resp.status_code not in (200, 204):
            raise LoginError(f"Could not open the workspace (HTTP {resp.status_code})")
        return next((c.value for c in client.cookies.jar if c.name == "ccs-session"), ccs_session)


def _match_workspace(workspaces: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    needle = wanted.lower()
    for w in workspaces:
        name = w.get("company_name", "")
        if name.lower() == needle or w.get("platform_customer_id") == wanted or needle in name.lower():
            return w
    names = [w.get("company_name", "") for w in workspaces]
    close = difflib.get_close_matches(wanted, names, n=1, cutoff=0.6)
    return next((w for w in workspaces if w.get("company_name") == close[0]), None) if close else None


def _ask_workspace(names: list[str]) -> str:
    _say("\nWorkspaces:")
    for i, name in enumerate(names, 1):
        _say(f"  {i:2}. {name}")
    while True:
        try:
            answer = input("Workspace number or name: ").strip()
        except EOFError as exc:
            raise LoginError("No workspace chosen; pass --workspace") from exc
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
        matches = [n for n in names if answer and answer.lower() in n.lower()]
        if len(matches) == 1:
            return matches[0]
        _say("Not a unique match, try again.")


async def choose_workspace(workspaces: list[dict[str, Any]], wanted: str | None, ui: LoginUI) -> dict[str, Any]:
    names = [w.get("company_name", "") for w in workspaces]
    if wanted:
        found = _match_workspace(workspaces, wanted)
        if not found:
            raise LoginError(f"Workspace '{wanted}' not found. Available: {', '.join(names)}")
        return found
    if len(workspaces) == 1:
        return workspaces[0]
    picked = await ui.workspace(names)
    found = next((w for w in workspaces if w.get("company_name") == picked), None)
    if not found:
        raise LoginError(f"Workspace '{picked}' not found")
    return found


async def _resolve_region(access_token: str, ccs_session: str, wanted: str | None, say: Say) -> str:
    """The COM region provisioned in this workspace (a workspace can have several)."""
    if wanted:
        if wanted not in COM_REGIONS:
            raise LoginError(f"Unknown region '{wanted}'. Valid: us-west, eu-central, ap-northeast")
        return wanted
    provisioned: list[str] = []
    try:
        async with _client(timeout=15) as client:
            resp = await client.get(
                f"{USER_API_BASE}/ui-doorway/ui/v1/applications/provisions",
                headers=_session_headers(access_token, ccs_session),
            )
        if resp.status_code == 200:
            provisioned = sorted(
                {
                    p["region"]
                    for p in resp.json().get("provisions", [])
                    if p.get("name") == "Compute Ops Management" and p.get("provision_status") == "PROVISIONED" and p.get("region")
                }
            )
    except (httpx.HTTPError, ValueError):
        pass
    if not provisioned:
        say("Could not see where Compute Ops Management is provisioned; assuming us-west (use --region to change).")
        return "us-west"
    return "us-west" if "us-west" in provisioned else provisioned[0]


async def _region_with_servers(api_client: dict[str, str], guess: str, say: Say) -> str:
    """Pick the region where this workspace's COM servers are, using the new API client."""
    account = ComAccount("com", api_client["client_id"], api_client["client_secret"], guess, "login")
    try:
        async with ComClient(account, transport=_transport) as client:
            totals = {r: n for r, n in (await client.region_totals()).items() if n is not None}
    except ComError:
        return guess
    if not totals:
        say("Compute Ops Management did not answer in any region for this workspace yet; using " + guess + ".")
        return guess
    best = max(totals, key=lambda r: (totals[r], r == guess))
    if len(totals) > 1:
        per = ", ".join(f"{r}: {n} servers" for r, n in totals.items())
        say(f"Compute Ops Management is set up in several regions ({per}); using {best}. Use --region to choose another.")
    return best


def _install_id() -> str:
    """Random per-install tag, so two installs that share a hostname (WSL and its Windows host,
    cloned VMs, containers) never delete each other's API client."""
    path = com_login_path().with_name("install_id")
    with contextlib.suppress(OSError):
        saved = path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{6}", saved):
            return saved
    new = secrets.token_hex(3)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new, encoding="utf-8")
    return new


def _credential_prefix() -> str:
    host = re.sub(r"[^A-Za-z0-9]", "", socket.gethostname())[:16] or "host"
    return f"{CRED_NAME_PREFIX}-{host}-{_install_id()}-"


async def _create_api_client(access_token: str, ccs_session: str) -> dict[str, str]:
    """Create the temporary API client, first removing this machine's earlier ones."""
    headers = _session_headers(access_token, ccs_session)
    prefix = _credential_prefix()
    async with _client(timeout=15) as client:
        with contextlib.suppress(httpx.HTTPError, ValueError):
            listing = await client.get(CREDENTIALS_URI, headers=headers)
            if listing.status_code == 200:
                body = listing.json()
                items = body if isinstance(body, list) else body.get("items", body.get("credentials", []))
                for item in items:
                    old = item.get("credential_name", item.get("name", ""))
                    if old.startswith(prefix):
                        await client.delete(f"{CREDENTIALS_URI}/{old}", headers=headers)
        name = f"{prefix}{datetime.datetime.now().strftime('%y%m%d%H%M%S')}"
        resp = await client.post(
            CREDENTIALS_URI,
            json={"credential_name": name, "application_instance_id": GLP_APP_INSTANCE_ID},
            headers={**headers, "Content-Type": "application/json"},
        )
    if resp.status_code not in (200, 201):
        raise LoginError(
            f"GreenLake would not create an API client (HTTP {resp.status_code}). Your role may not allow it, "
            "or the workspace is at its API client limit; delete one under Manage Workspace > API, or ask an administrator."
        )
    data = resp.json()
    if not data.get("client_id") or not data.get("client_secret"):
        raise LoginError("GreenLake returned an API client without an ID or secret")
    return {"client_id": data["client_id"], "client_secret": data["client_secret"], "name": name}


# ── Saved login ───────────────────────────────────────────────────────────────

def save_login(data: dict[str, Any]) -> None:
    path = com_login_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def load_login() -> dict[str, Any]:
    try:
        return json.loads(com_login_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def login(email: str, ui: LoginUI, workspace: str | None = None, region: str | None = None) -> dict[str, Any]:
    token = await sign_in(email, ui)
    access_token, id_token = token.get("access_token"), token.get("id_token")
    if not access_token or not id_token:
        raise LoginError("GreenLake returned no tokens")
    ui.say("Signed in. Opening your workspace...")
    workspaces, ccs_session = await _init_workspace_session(access_token, id_token)
    chosen = await choose_workspace(workspaces, workspace, ui)
    workspace_id, workspace_name = chosen["platform_customer_id"], chosen.get("company_name", "")
    ccs_session = await _load_workspace(access_token, workspace_id, ccs_session)
    resolved = await _resolve_region(access_token, ccs_session, region, ui.say)
    ui.say(f"Creating a GreenLake API client in '{workspace_name}'...")
    api_client = await _create_api_client(access_token, ccs_session)
    if not region:
        resolved = await _region_with_servers(api_client, resolved, ui.say)
    data = {
        "client_id": api_client["client_id"],
        "client_secret": api_client["client_secret"],
        "credential_name": api_client["name"],
        "region": resolved,
        "workspace": workspace_name,
        "workspace_id": workspace_id,
        "email": email,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    }
    save_login(data)
    return data


async def server_count(data: dict[str, Any]) -> int | None:
    """First Compute Ops Management call with the new API client."""
    account = ComAccount("com", data["client_id"], data["client_secret"], data["region"], "login")
    async with ComClient(account, transport=_transport) as client:
        return (await client.get("/servers", {"limit": 1})).get("total")


def run_login(email: str | None, workspace: str | None, region: str | None) -> int:
    previous = load_login().get("email", "")
    try:
        if not email:
            email = input(f"HPE GreenLake email{f' [{previous}]' if previous else ''}: ").strip() or previous
        if not email:
            raise LoginError("An email address is required")
        data = asyncio.run(login(email, TerminalUI(), workspace, region))
        _say(f"Logged in as {email}. Saved to {com_login_path()}")
        try:
            _say(f"Compute Ops Management answers: {asyncio.run(server_count(data))} servers.")
        except ComError as exc:
            _say(f"Saved, but the first Compute Ops Management call failed: {exc}\nTry `proliant-mcp --check --connect` in a minute.")
    except (LoginError, httpx.HTTPError) as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.", file=sys.stderr)
        return 1
    return 0


def run_logout() -> int:
    path = com_login_path()
    saved = load_login()
    if not path.is_file():
        _say("No saved GreenLake login.")
        return 0
    path.unlink()
    _say(f"Removed {path}.")
    if saved.get("credential_name"):
        _say(
            f"The API client '{saved['credential_name']}' still exists in GreenLake. Delete it under "
            f"Manage Workspace > API in '{saved.get('workspace', 'your workspace')}' if you no longer need it."
        )
    return 0
