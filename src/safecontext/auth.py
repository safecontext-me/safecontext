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

import json
import os
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
    creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=open_browser,
                                  timeout_seconds=600, **_auth_params(login_hint))
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
    creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=open_browser,
                                  timeout_seconds=600, **params)
    if SCOPE_CALENDAR not in set(creds.granted_scopes or creds.scopes or []):
        raise NeedsReconnect("scope_not_granted",
                             "Calendar access wasn't granted. On Google's consent screen, tick the box that lets "
                             "SafeContext see your calendar events, then try again.")
    account = fetch_calendar_account(creds)
    (tokens or calendar_tokens()).save(account, creds)
    return creds, account


def fetch_calendar_account(creds: Credentials) -> str:
    """The primary calendar's id is the account's email address."""
    from googleapiclient.discovery import build

    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return svc.calendars().get(calendarId="primary").execute()["id"].lower()


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
