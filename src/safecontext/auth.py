"""Google sign-in for a Desktop OAuth client: installed-app flow with PKCE
(S256) and a loopback redirect on 127.0.0.1. Tokens live in the macOS
Keychain via `keyring`, never in a plain file.

Gmail and Google Calendar share one Google OAuth client. Calendar is an
incremental grant: `calendar.readonly` is requested only when the user
connects Calendar (with include_granted_scopes), so Gmail-only users never see
it and existing Gmail tokens keep working untouched. Each source keeps its own
token in its own Keychain item.

Every way access can be lost (revoked, expired, password changed, scope
unticked on the consent screen) surfaces as `NeedsReconnect`, which the app
shows as a "Reconnect Gmail" prompt instead of crashing.
"""
from __future__ import annotations

import contextlib
import http.server
import ipaddress
import json
import os
import socketserver
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

import keyring
import keyring.errors
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .env import keychain_service
from .migrate import getenv, keychain_delete, keychain_get, legacy_service

SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_METADATA = "https://www.googleapis.com/auth/gmail.metadata"
SCOPE_CALENDAR = "https://www.googleapis.com/auth/calendar.readonly"
MODES = {"full": SCOPE_READONLY, "headers": SCOPE_METADATA}

# Development checkouts use "… (Dev)" names so they never read the packaged app's items
# (or vice versa); see env.py.
KEYRING_SERVICE = keychain_service("SafeContext Gmail")
LEGACY_KEYRING_SERVICE = legacy_service("MyContext Gmail")  # before the rename; see migrate.py
CALENDAR_KEYRING_SERVICE = keychain_service("SafeContext Google Calendar")
CLIENT_CONFIG_ENV = "SAFECONTEXT_OAUTH_CLIENT"  # MYCONTEXT_OAUTH_CLIENT still works
BUNDLED_CLIENT_CONFIG = Path(__file__).with_name("oauth_client.json")

RECONNECT_MESSAGE = "Gmail access was revoked or has expired. Click “Reconnect Gmail” to continue syncing."
CALENDAR_RECONNECT_MESSAGE = ("Google Calendar access was revoked or has expired. Click “Reconnect” on the "
                              "Google Calendar card to continue syncing.")
CALENDAR_NOT_CONNECTED = "Google Calendar isn't connected yet. Connect it on the Sources screen."
CALENDAR_API_DISABLED = ("Google turned the request down because the Google Calendar API isn't switched on for "
                         "SafeContext yet. This is on SafeContext's side, not your account. Please try again later.")


class NeedsReconnect(Exception):
    """Access is gone; the user must sign in again. `reason` is machine-readable."""

    def __init__(self, reason: str, message: str = RECONNECT_MESSAGE) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class NotConfigured(Exception):
    pass


class TransientAuthError(Exception):
    """Network trouble while refreshing; retry later, don't ask to reconnect."""


# --- client config ---------------------------------------------------------

def load_client_config(path: Optional[Path] = None) -> dict:
    p = Path(path or getenv("OAUTH_CLIENT") or BUNDLED_CLIENT_CONFIG)
    if not p.exists():
        raise NotConfigured(
            f"No Google OAuth client found at {p}. Download the Desktop client JSON from Google Cloud "
            f"Console and save it there (or set {CLIENT_CONFIG_ENV}).")
    cfg = json.loads(p.read_text())
    if "installed" not in cfg:
        raise NotConfigured("The OAuth client must be of type 'Desktop app' (JSON key 'installed').")
    return cfg


def build_flow(client_config: dict, mode: str = "full"):
    from google_auth_oauthlib.flow import InstalledAppFlow

    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    # autogenerate_code_verifier=True gives PKCE with S256.
    return InstalledAppFlow.from_client_config(client_config, scopes=[MODES[mode]],
                                               autogenerate_code_verifier=True)


def _auth_params(login_hint: Optional[str]) -> dict:
    params = {"access_type": "offline", "prompt": "consent"}
    if login_hint:
        params["login_hint"] = login_hint
    return params


def required_scope_granted(creds: Credentials, mode: str) -> bool:
    granted = set(creds.granted_scopes or creds.scopes or [])
    return MODES[mode] in granted


# --- loopback redirect server ----------------------------------------------
#
# `run_local_server` starts a wsgiref server, and `http.server.HTTPServer.server_bind`
# sets `server_name = socket.getfqdn(host)`: a reverse DNS lookup of 127.0.0.1. On
# recent macOS that lookup can go out over mDNS, which shows the "find devices on
# local networks" privacy prompt in the middle of sign-in. The server only needs
# its name for WSGI's SERVER_NAME, so for a loopback address we use the address
# itself. Patched only while a sign-in is running.

_ORIGINAL_SERVER_BIND = http.server.HTTPServer.server_bind
_bind_patch_lock = threading.Lock()
_bind_patch_users = 0


def _is_loopback(host) -> bool:
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _server_bind_without_dns(self) -> None:
    if not _is_loopback(self.server_address[0]):
        return _ORIGINAL_SERVER_BIND(self)
    socketserver.TCPServer.server_bind(self)
    host, port = self.server_address[:2]
    self.server_name = str(host)
    self.server_port = port


# The library's redirect app answers the browser with a bare text/plain line. We swap
# in `_FinishPageApp` (same request handling, HTML answer) for the same span, and
# pass the source's name through `success_message`, the one value the library
# hands to the app's constructor.

_ORIGINAL_REDIRECT_APP = None  # google_auth_oauthlib.flow._RedirectWSGIApp, set on first use


@contextlib.contextmanager
def _loopback_sign_in_patches():
    global _bind_patch_users, _ORIGINAL_REDIRECT_APP
    from google_auth_oauthlib import flow as oauthlib_flow

    with _bind_patch_lock:
        if _bind_patch_users == 0:
            http.server.HTTPServer.server_bind = _server_bind_without_dns
            _ORIGINAL_REDIRECT_APP = oauthlib_flow._RedirectWSGIApp
            oauthlib_flow._RedirectWSGIApp = _finish_page_app_class(_ORIGINAL_REDIRECT_APP)
        _bind_patch_users += 1
    try:
        yield
    finally:
        with _bind_patch_lock:
            _bind_patch_users -= 1
            if _bind_patch_users == 0:
                http.server.HTTPServer.server_bind = _ORIGINAL_SERVER_BIND
                oauthlib_flow._RedirectWSGIApp = _ORIGINAL_REDIRECT_APP



CANCELLED_MESSAGE = "Sign-in was cancelled. Click Connect to try again."


def _run_sign_in(flow, open_browser: bool, params: dict, source_name: str = "Google") -> Credentials:
    """Browser sign-in through a loopback redirect on 127.0.0.1 (PKCE comes from the flow)."""
    from oauthlib.oauth2.rfc6749.errors import AccessDeniedError

    with _loopback_sign_in_patches():
        try:
            return flow.run_local_server(host="127.0.0.1", port=0, open_browser=open_browser,
                                         timeout_seconds=600, success_message=source_name, **params)
        except AccessDeniedError:  # the user clicked Cancel on Google's consent screen
            raise NotConfigured(CANCELLED_MESSAGE) from None


# --- the page the browser lands on after Google -----------------------------
#
# Served from this Mac, so it is fully self-contained: inline CSS only, no fonts,
# scripts or images fetched from anywhere (the CSP header enforces that). It never
# echoes anything from the query string: not the code, the state, or Google's
# error text. Colors and type follow app/ui/tokens.css.

_LOGO = ('<svg class="logo" viewBox="0 0 32 32" aria-hidden="true"><path fill="currentColor" fill-rule="evenodd" '
         'clip-rule="evenodd" d="M24 0C28.4183 0 32 3.58172 32 8V24C32 28.4183 28.4183 32 24 32H8C3.58172 32 0 '
         '28.4183 0 24V8C0 3.58172 3.58172 0 8 0H24ZM8.5 9.2998V14.9004C8.50017 19.7002 11.7001 23.5001 16 25.5C'
         '20.2999 23.5001 23.4998 19.7002 23.5 14.9004V9.2998L16 6.5L8.5 9.2998Z"/></svg>')
_ICON_OK = ('<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" '
            'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>')
_ICON_STOP = ('<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 5l6 6M11 5l-6 6" fill="none" '
              'stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>')

_FINISH_CSS = """
:root{color-scheme:light dark;--chrome:#ebeae6;--background:#f6f6f4;--foreground:#1d1d1b;--card:#fff;
--muted:#6b6a66;--border:#eae9e4;--primary:#2f5bd3;--ok:#2e8b57;--ok-subtle:#e3f3ea;--bad:#c0392b;
--bad-subtle:#fbe9e7}
@media (prefers-color-scheme:dark){:root{--chrome:#141416;--background:#1b1b1d;--foreground:#ececea;
--card:#242427;--muted:#a09f9a;--border:#2b2b2e;--primary:#7c9cff;--ok:#5cc78a;--ok-subtle:#1f3a2b;
--bad:#ff7a6b;--bad-subtle:#3d2320}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;display:flex;align-items:center;justify-content:center;padding:24px;background:var(--chrome);
color:var(--foreground);font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,
sans-serif;-webkit-font-smoothing:antialiased}
main{width:100%;max-width:400px;background:var(--card);border:1px solid var(--border);border-radius:12px;
padding:32px;text-align:center;box-shadow:0 8px 24px rgba(0,0,0,.06)}
.brand{display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:24px;font-weight:600;
color:var(--foreground)}
.logo{width:22px;height:22px;color:var(--primary)}
.status{width:40px;height:40px;margin:0 auto 16px;border-radius:999px;display:flex;align-items:center;
justify-content:center}
.status svg{width:20px;height:20px}
.ok .status{background:var(--ok-subtle);color:var(--ok)}
.bad .status{background:var(--bad-subtle);color:var(--bad)}
h1{margin:0 0 8px;font-size:20px;font-weight:600;line-height:1.3}
p{margin:0;color:var(--muted)}
p+p{margin-top:4px}
"""

_FINISH_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'"


def _escape(text: str) -> str:
    import html
    return html.escape(text, quote=True)


def finish_page(query: str, source_name: str = "Google") -> str:
    """The HTML shown after Google redirects back. Only reads which keys are present
    (`code`, `error`) and whether the error is access_denied; never echoes values."""
    params = urllib.parse.parse_qs(query or "", keep_blank_values=True)
    name = _escape(source_name or "Google")
    if "error" in params:
        cancelled = params["error"][:1] == ["access_denied"]
        ok, title = False, "Sign-in was cancelled" if cancelled else "Sign-in didn\u2019t finish"
        lines = ["Return to SafeContext to try again.", "You can close this tab."]
    elif params.get("code", [""])[0]:
        ok, title = True, (f"{name} connected" if source_name != "Google" else "You\u2019re signed in")
        lines = ["You\u2019re signed in. Go back to SafeContext to finish.", "It\u2019s safe to close this tab."]
    else:
        ok, title = False, "Sign-in didn\u2019t finish"
        lines = ["Return to SafeContext to try again.", "You can close this tab."]
    body = "".join(f"<p>{line}</p>" for line in lines)
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"referrer\" content=\"no-referrer\">"
            f"<title>{title} \u2013 SafeContext</title><style>{_FINISH_CSS}</style></head>"
            f"<body><main class=\"{'ok' if ok else 'bad'}\">"
            f"<div class=\"brand\">{_LOGO}<span>SafeContext</span></div>"
            f"<div class=\"status\">{_ICON_OK if ok else _ICON_STOP}</div>"
            f"<h1>{title}</h1>{body}</main></body></html>")


def _finish_page_app_class(base):
    """Subclass of the library's `_RedirectWSGIApp` that answers with `finish_page`.
    It still records `last_request_uri`, which `run_local_server` reads for the code."""
    import wsgiref.util

    class _FinishPageApp(base):
        def __call__(self, environ, start_response):
            self.last_request_uri = wsgiref.util.request_uri(environ)
            page = finish_page(environ.get("QUERY_STRING", ""), self._success_message)
            start_response("200 OK", [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Security-Policy", _FINISH_CSP),
                ("Cache-Control", "no-store"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Content-Type-Options", "nosniff"),
            ])
            return [page.encode("utf-8")]

    return _FinishPageApp


# --- token storage ---------------------------------------------------------

KEYCHAIN_UNAVAILABLE = "Couldn't access the macOS Keychain, where the Gmail sign-in is stored."


class TokenStore:
    def __init__(self, service: str = KEYRING_SERVICE,
                 legacy_service: Optional[str] = LEGACY_KEYRING_SERVICE) -> None:
        self.service = service
        self.legacy_service = legacy_service

    def save(self, account: str, creds: Credentials) -> None:
        try:
            keyring.set_password(self.service, account, creds.to_json())
        except keyring.errors.KeyringError:
            raise NotConfigured(KEYCHAIN_UNAVAILABLE) from None

    def load(self, account: str) -> Optional[Credentials]:
        try:
            blob = keychain_get(self.service, account, self.legacy_service)
        except keyring.errors.KeyringError:
            raise NotConfigured(KEYCHAIN_UNAVAILABLE) from None
        if not blob:
            return None
        info = json.loads(blob)
        return Credentials.from_authorized_user_info(info, scopes=info.get("scopes"))

    def delete(self, account: str) -> None:
        keychain_delete(self.service, account, self.legacy_service)


# --- connect / refresh / disconnect ---------------------------------------

def connect(mode: str = "full", login_hint: Optional[str] = None, *, client_config: Optional[dict] = None,
            tokens: Optional[TokenStore] = None, open_browser: bool = True) -> Tuple[Credentials, str]:
    """Run the browser sign-in. Returns (credentials, account email)."""
    flow = build_flow(client_config or load_client_config(), mode)
    creds = _run_sign_in(flow, open_browser, _auth_params(login_hint), "Gmail")
    if not required_scope_granted(creds, mode):
        raise NeedsReconnect("scope_not_granted",
                             "Gmail access wasn't granted. On Google's consent screen, tick the box that lets "
                             "SafeContext read your email, then try again.")
    account = fetch_account_email(creds)
    (tokens or TokenStore()).save(account, creds)
    return creds, account


def fetch_account_email(creds: Credentials) -> str:
    from googleapiclient.discovery import build

    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return svc.users().getProfile(userId="me").execute()["emailAddress"].lower()


def is_invalid_grant(err: Exception) -> bool:
    text = str(err).lower()
    return "invalid_grant" in text or "token has been expired or revoked" in text


def _usable_credentials(account: str, scope: str, tokens: TokenStore, request: Optional[Request],
                        not_connected: str, reconnect: str) -> Credentials:
    if not account:
        raise NeedsReconnect("not_connected", not_connected)
    creds = tokens.load(account)
    if creds is None or not creds.refresh_token:
        raise NeedsReconnect("not_connected", not_connected)
    if scope not in set(creds.scopes or []):
        raise NeedsReconnect("scope_not_granted", reconnect)
    if not creds.valid:
        try:
            refresh(creds, request)
        except NeedsReconnect as e:
            raise NeedsReconnect(e.reason, reconnect) from None
        tokens.save(account, creds)
    return creds


def get_credentials(account: str, mode: str = "full", tokens: Optional[TokenStore] = None,
                    request: Optional[Request] = None) -> Credentials:
    """Load stored Gmail credentials and make sure they are usable right now."""
    return _usable_credentials(account, MODES[mode], tokens or TokenStore(), request,
                               "Gmail isn't connected yet. Click “Connect Gmail”.", RECONNECT_MESSAGE)


def calendar_tokens() -> TokenStore:
    return TokenStore(CALENDAR_KEYRING_SERVICE, legacy_service=None)


def get_calendar_credentials(account: str, tokens: Optional[TokenStore] = None,
                             request: Optional[Request] = None) -> Credentials:
    return _usable_credentials(account, SCOPE_CALENDAR, tokens or calendar_tokens(), request,
                               CALENDAR_NOT_CONNECTED, CALENDAR_RECONNECT_MESSAGE)


def connect_calendar(login_hint: Optional[str] = None, *, client_config: Optional[dict] = None,
                     tokens: Optional[TokenStore] = None, open_browser: bool = True) -> Tuple[Credentials, str]:
    """Ask for calendar.readonly as an incremental grant on the same Google
    client. Returns (credentials, account email)."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    # include_granted_scopes returns a token that also lists scopes granted
    # earlier (e.g. Gmail); oauthlib would otherwise reject the "changed" scope.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_config(client_config or load_client_config(), scopes=[SCOPE_CALENDAR],
                                               autogenerate_code_verifier=True)
    params = {**_auth_params(login_hint), "include_granted_scopes": "true"}
    creds = _run_sign_in(flow, open_browser, params, "Google Calendar")
    if SCOPE_CALENDAR not in set(creds.granted_scopes or creds.scopes or []):
        raise NeedsReconnect("scope_not_granted",
                             "Calendar access wasn't granted. On Google's consent screen, tick the box that lets "
                             "SafeContext see your calendar events, then try again.")
    account = fetch_calendar_account(creds)
    (tokens or calendar_tokens()).save(account, creds)
    return creds, account


def is_api_disabled(err: Exception) -> bool:
    """HTTP 403 because the API isn't enabled in the OAuth client's Google Cloud project."""
    status = int(getattr(getattr(err, "resp", None), "status", 0) or 0)
    text = str(getattr(err, "content", b"") or err).lower()
    return status == 403 and ("accessnotconfigured" in text or "service_disabled" in text
                              or "has not been used in project" in text)


def fetch_calendar_account(creds: Credentials) -> str:
    """The primary calendar's id is the account's email address."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    try:
        return svc.calendars().get(calendarId="primary").execute()["id"].lower()
    except HttpError as e:
        if is_api_disabled(e):
            raise NotConfigured(CALENDAR_API_DISABLED) from None
        raise


def refresh(creds: Credentials, request: Optional[Request] = None) -> None:
    try:
        creds.refresh(request or Request())
    except RefreshError as e:
        if is_invalid_grant(e):
            raise NeedsReconnect("revoked_or_expired") from None
        raise TransientAuthError(str(e)) from None
    except TransportError as e:
        raise TransientAuthError(str(e)) from None


def disconnect(account: str, tokens: Optional[TokenStore] = None, revoke: bool = True) -> None:
    """Revoke the grant at Google (best effort) and forget the token.

    Revoking any token revokes the whole grant for this Google client, Gmail
    and Calendar together, so callers pass revoke=False while another Google
    source on the same account is still connected; then only this source's
    token is forgotten."""
    tokens = tokens or TokenStore()
    creds = tokens.load(account) if revoke else None
    if creds is not None:
        token = creds.refresh_token or creds.token
        if token:
            data = urllib.parse.urlencode({"token": token}).encode()  # in the body, never the URL
            req = urllib.request.Request("https://oauth2.googleapis.com/revoke", data=data,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            try:
                urllib.request.urlopen(req, timeout=15).close()
            except Exception:
                pass  # already revoked or offline; still forget locally
    tokens.delete(account)


def scopes_for(mode: str) -> List[str]:
    return [MODES[mode]]
