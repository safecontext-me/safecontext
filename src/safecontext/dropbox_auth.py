"""Dropbox sign-in for a desktop app.

OAuth with PKCE (S256) and a loopback redirect on 127.0.0.1, same shape as
Google sign-in. The app key comes from the environment, never from the repo.
The token lives in the macOS Keychain. The scopes are read-only: account info,
file metadata, and file bytes. Nothing here can change the user's Dropbox.

SAFECONTEXT_DROPBOX_APP_KEY is required. SAFECONTEXT_DROPBOX_APP_SECRET is
optional, for a Dropbox app that still has a secret; a PKCE app can omit it.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Optional

import keyring
import keyring.errors

from . import auth
from .env import keychain_service
from .errors import TransientError
from .migrate import getenv, keychain_delete, keychain_get

SCOPES = ("account_info.read", "files.metadata.read", "files.content.read")
KEYRING_SERVICE = keychain_service("SafeContext Dropbox")
APP_KEY_ENV = "DROPBOX_APP_KEY"          # SAFECONTEXT_DROPBOX_APP_KEY
APP_SECRET_ENV = "DROPBOX_APP_SECRET"    # SAFECONTEXT_DROPBOX_APP_SECRET, optional

AUTHORIZE_URL = "https://www.dropbox.com/oauth2/authorize"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
REVOKE_URL = "https://api.dropboxapi.com/2/auth/token/revoke"
ACCOUNT_URL = "https://api.dropboxapi.com/2/users/get_current_account"

RECONNECT_MESSAGE = ("Dropbox access was revoked or has expired. Click “Reconnect” on the "
                     "Dropbox card to continue syncing.")
_MISSING_KEY = (
    "Dropbox sign-in needs a Dropbox app key. Set SAFECONTEXT_DROPBOX_APP_KEY to the app key "
    "from the Dropbox App Console. Allow only the read scopes account_info.read, "
    "files.metadata.read and files.content.read."
)


def app_key() -> str:
    key = (getenv(APP_KEY_ENV) or "").strip()
    if not key:
        raise auth.NotConfigured(_MISSING_KEY)
    return key


def app_secret() -> str:
    return (getenv(APP_SECRET_ENV) or "").strip()


def authorize_url(client_id: str, code_challenge: str, redirect_uri: str) -> str:
    q = {
        "client_id": client_id,
        "response_type": "code",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "token_access_type": "offline",
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(q)


class TokenStore:
    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        self.service = service

    def save(self, account: str, token: dict) -> None:
        try:
            keyring.set_password(self.service, account, json.dumps(token))
        except keyring.errors.KeyringError:
            raise auth.NotConfigured("Couldn't access the macOS Keychain, where the Dropbox sign-in is stored.") from None

    def load(self, account: str) -> Optional[dict]:
        try:
            blob = keychain_get(self.service, account, None)
        except keyring.errors.KeyringError:
            raise auth.NotConfigured("Couldn't access the macOS Keychain, where the Dropbox sign-in is stored.") from None
        if not blob:
            return None
        return json.loads(blob)

    def delete(self, account: str) -> None:
        keychain_delete(self.service, account, None)


def connect(email: Optional[str] = None, *, open_browser: bool = True, opener=None) -> str:
    """Sign in and return the Dropbox account email. `email` is unused: Dropbox has no login hint."""
    del email
    key = app_key()
    verifier, challenge = _pkce()
    redirect, query = _browser_roundtrip(key, challenge, open_browser)
    if (query.get("error") or [""])[0]:
        raise auth.NotConfigured("Dropbox sign-in was cancelled.")
    code = (query.get("code") or [""])[0]
    if not code:
        raise auth.NotConfigured("Dropbox sign-in didn't finish. Try Connect again.")
    token = exchange_code(key, app_secret(), code, verifier, redirect, opener=opener)
    address = fetch_account_email(token["access_token"], opener=opener)
    token["email"] = address
    TokenStore().save(address, token)
    return address


def access_token(account: str, *, opener=None) -> str:
    """A usable access token, refreshing it when it is close to expiry."""
    store = TokenStore()
    blob = store.load(account)
    if not blob or not blob.get("access_token"):
        raise auth.NeedsReconnect("revoked_or_expired", RECONNECT_MESSAGE)
    if float(blob.get("expires_at") or 0) > time.time() + 60:
        return blob["access_token"]
    refresh = blob.get("refresh_token") or ""
    if not refresh:
        raise auth.NeedsReconnect("revoked_or_expired", RECONNECT_MESSAGE)
    try:
        key = app_key()
    except auth.NotConfigured as e:
        raise auth.NeedsReconnect("not_configured", str(e)) from None
    fresh = refresh_token(key, app_secret(), refresh, opener=opener)
    blob["access_token"] = fresh["access_token"]
    blob["expires_at"] = fresh["expires_at"]
    if fresh.get("refresh_token"):
        blob["refresh_token"] = fresh["refresh_token"]
    store.save(account, blob)
    return blob["access_token"]


def revoke(account: str, revoke: bool = True, *, opener=None) -> None:
    """Forget the local token. Ask Dropbox to revoke it when we still have one."""
    store = TokenStore()
    blob = store.load(account) if revoke else None
    if blob and blob.get("access_token"):
        try:
            _post_bearer(REVOKE_URL, blob["access_token"], opener=opener)
        except Exception:
            pass  # local delete still happens; a dead token is already useless
    store.delete(account)


def exchange_code(client_id: str, client_secret: str, code: str, verifier: str, redirect_uri: str, *,
                  opener=None) -> dict:
    fields = {
        "code": code,
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
    }
    if client_secret:
        fields["client_secret"] = client_secret
    return _token_response(_post_form(TOKEN_URL, fields, opener=opener))


def refresh_token(client_id: str, client_secret: str, refresh: str, *, opener=None) -> dict:
    fields = {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id}
    if client_secret:
        fields["client_secret"] = client_secret
    return _token_response(_post_form(TOKEN_URL, fields, opener=opener))


def fetch_account_email(token: str, *, opener=None) -> str:
    raw = _post_bearer(ACCOUNT_URL, token, opener=opener, body=b"null")
    try:
        email = json.loads(raw.decode("utf-8")).get("email") or ""
    except Exception:
        email = ""
    if not email:
        raise auth.NeedsReconnect("revoked_or_expired", RECONNECT_MESSAGE)
    return email


def _pkce() -> tuple:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = _b64(digest)
    return verifier, challenge


def _b64(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _token_response(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise auth.NeedsReconnect("revoked_or_expired", RECONNECT_MESSAGE) from None
    access = data.get("access_token") or ""
    if not access:
        raise auth.NeedsReconnect("revoked_or_expired", RECONNECT_MESSAGE)
    return {
        "access_token": access,
        "refresh_token": data.get("refresh_token") or "",
        "expires_at": time.time() + int(data.get("expires_in") or 14400),
        "account_id": data.get("account_id") or "",
    }


def _browser_roundtrip(client_id: str, challenge: str, open_browser: bool):
    """Open the Dropbox page and wait for the loopback redirect. Returns (redirect_uri, query)."""
    with auth._no_reverse_dns_for_loopback():
        httpd = http.server.HTTPServer(("127.0.0.1", 0), _Redirect)
    redirect = f"http://127.0.0.1:{httpd.server_address[1]}/"
    url = authorize_url(client_id, challenge, redirect)
    if open_browser:
        webbrowser.open(url)
    httpd.timeout = 600
    try:
        httpd.handle_request()
    finally:
        httpd.server_close()
    return redirect, getattr(httpd, "query", {})


class _Redirect(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.server.query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)  # type: ignore[attr-defined]
        failed = "error" in self.server.query  # type: ignore[attr-defined]
        body = (b"Dropbox sign-in was cancelled. You can close this window." if failed
                else b"Signed in. You can close this window and return to SafeContext.")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return  # the URL contains the one-time code


def _post_form(url: str, fields: dict, *, opener=None) -> bytes:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    return _read(req, opener)


def _post_bearer(url: str, token: str, *, opener=None, body: Optional[bytes] = None) -> bytes:
    req = urllib.request.Request(url, data=b"" if body is None else body, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    return _read(req, opener)


def _read(req: urllib.request.Request, opener) -> bytes:
    open_ = opener or urllib.request.urlopen
    try:
        with open_(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        code = getattr(e, "code", 0) or 0
        if code in (400, 401):
            raise auth.NeedsReconnect("revoked_or_expired", RECONNECT_MESSAGE) from None
        raise TransientError(f"Dropbox returned HTTP {code}") from None
    except auth.NeedsReconnect:
        raise
    except TransientError:
        raise
    except Exception:
        raise TransientError("Couldn't reach Dropbox.") from None
