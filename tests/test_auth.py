"""Sign-in, token storage and every way access can be lost. No network."""
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import keyring
import pytest
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from keyring.backend import KeyringBackend

from safecontext import auth
from safecontext.auth import (NeedsReconnect, NotConfigured, TokenStore, TransientAuthError, build_flow,
                            get_credentials, load_client_config)

CLIENT = {"installed": {
    "client_id": "123-test.apps.googleusercontent.com", "client_secret": "GOCSPX-test-not-secret",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
    "redirect_uris": ["http://localhost"]}}


def _utcnow():
    """Naive UTC, which is what google-auth compares Credentials.expiry against."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.items:
            raise keyring.errors.PasswordDeleteError("missing")
        del self.items[(service, username)]


@pytest.fixture(autouse=True)
def mem_keyring():
    old = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    yield kr
    keyring.set_keyring(old)


def creds(scope=auth.SCOPE_READONLY, expired=False, refresh_token="1//refresh-test"):
    c = Credentials(token="ya29.test", refresh_token=refresh_token, client_id="cid", client_secret="cs",
                    token_uri="https://oauth2.googleapis.com/token", scopes=[scope])
    c.expiry = _utcnow() + (timedelta(hours=-1) if expired else timedelta(hours=1))
    return c


# --- the consent URL ----------------------------------------------------------

@pytest.mark.parametrize("mode,scope", [("full", "gmail.readonly"), ("headers", "gmail.metadata")])
def test_authorization_url_uses_pkce_offline_and_scope(mode, scope):
    flow = build_flow(CLIENT, mode)
    flow.redirect_uri = "http://127.0.0.1:53682/"
    url, _ = flow.authorization_url(**auth._auth_params("alex@example.org"))
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["code_challenge_method"] == ["S256"] and len(q["code_challenge"][0]) >= 43
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["login_hint"] == ["alex@example.org"]
    assert q["scope"] == [f"https://www.googleapis.com/auth/{scope}"]
    assert q["redirect_uri"] == ["http://127.0.0.1:53682/"]


def test_bad_mode_rejected():
    with pytest.raises(ValueError):
        build_flow(CLIENT, "everything")


# --- client config ------------------------------------------------------------

def test_missing_client_config_is_clear(tmp_path):
    with pytest.raises(NotConfigured, match="Desktop client JSON"):
        load_client_config(tmp_path / "nope.json")


def test_web_client_rejected(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"web": {}}))
    with pytest.raises(NotConfigured, match="Desktop app"):
        load_client_config(p)


def test_client_config_from_env(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(CLIENT))
    monkeypatch.setenv(auth.CLIENT_CONFIG_ENV, str(p))
    assert load_client_config()["installed"]["client_id"].startswith("123-")


# --- the loopback redirect server ------------------------------------------------

def _sign_in_through_real_loopback_server(monkeypatch, run, reply=None, pages=None):
    """Run a sign-in with oauthlib's real 127.0.0.1 server. A fake browser follows the
    redirect straight back with a code (or `reply`, e.g. Google's error=access_denied);
    the token exchange and Google calls are faked. What the browser got back is
    appended to `pages` as (status, headers, body)."""
    import http.client
    import socket
    import threading
    import webbrowser

    from google_auth_oauthlib.flow import Flow, InstalledAppFlow

    lookups = []
    real_getfqdn, real_gethostbyaddr = socket.getfqdn, socket.gethostbyaddr
    monkeypatch.setattr(socket, "getfqdn", lambda *a: lookups.append(("getfqdn", a)) or real_getfqdn(*a))
    monkeypatch.setattr(socket, "gethostbyaddr",
                        lambda *a: lookups.append(("gethostbyaddr", a)) or real_gethostbyaddr(*a))
    opened = []

    class FakeBrowser:
        def open(self, url, new=0, autoraise=True):
            opened.append(url)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect = urllib.parse.urlparse(q["redirect_uri"][0])
            path = "/?" + urllib.parse.urlencode({**(reply or {"code": "4/test-code"}), "state": q["state"][0]})

            def follow():  # http.client, not urllib: no proxy lookups
                conn = http.client.HTTPConnection(redirect.hostname, redirect.port, timeout=10)
                conn.request("GET", path)
                resp = conn.getresponse()
                got = (resp.status, dict(resp.getheaders()), resp.read().decode("utf-8"))
                conn.close()
                if pages is not None:
                    pages.append(got)
            followers.append(threading.Thread(target=follow, daemon=True))
            followers[-1].start()
            return True

    followers = []
    monkeypatch.setattr(webbrowser, "get", lambda browser=None: FakeBrowser())
    tokens_fetched = []
    if reply is None:  # with an error reply, the real fetch_token must see it (it raises before any network)
        monkeypatch.setattr(InstalledAppFlow, "fetch_token", lambda self, **kw: tokens_fetched.append(kw))
    monkeypatch.setattr(Flow, "credentials", property(
        lambda self: creds(auth.SCOPE_CALENDAR if auth.SCOPE_CALENDAR in self.oauth2session.scope
                           else auth.SCOPE_READONLY)))
    try:
        result = run()
    finally:
        for t in followers:
            t.join(timeout=10)
    assert len(opened) == 1 and urllib.parse.urlparse(opened[0]).hostname == "accounts.google.com"
    assert "code_challenge" in urllib.parse.parse_qs(urllib.parse.urlparse(opened[0]).query)  # PKCE kept
    assert "code=4%2Ftest-code" in tokens_fetched[0]["authorization_response"]
    return result, lookups


@pytest.mark.parametrize("which", ["gmail", "calendar"])
def test_sign_in_server_does_no_reverse_dns_lookup(monkeypatch, which):
    """A reverse lookup of 127.0.0.1 can go over mDNS and trigger macOS's Local Network prompt."""
    import http.server

    if which == "gmail":
        monkeypatch.setattr(auth, "fetch_account_email", lambda c: "a@example.org")
        run = lambda: auth.connect("full", "a@example.org", client_config=CLIENT)
    else:
        monkeypatch.setattr(auth, "fetch_calendar_account", lambda c: "a@example.org")
        run = lambda: auth.connect_calendar("a@example.org", client_config=CLIENT)
    (_, account), lookups = _sign_in_through_real_loopback_server(monkeypatch, run)
    assert account == "a@example.org"
    assert lookups == []
    assert http.server.HTTPServer.server_bind is auth._ORIGINAL_SERVER_BIND  # patch is gone afterwards


def _sign_in_run(monkeypatch, which):
    if which == "gmail":
        monkeypatch.setattr(auth, "fetch_account_email", lambda c: "a@example.org")
        return lambda: auth.connect("full", "a@example.org", client_config=CLIENT)
    monkeypatch.setattr(auth, "fetch_calendar_account", lambda c: "a@example.org")
    return lambda: auth.connect_calendar("a@example.org", client_config=CLIENT)


def _assert_safe_page(headers, body, *secrets):
    assert headers["Content-Type"].startswith("text/html")
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    assert body.lstrip().lower().startswith("<!doctype html>")
    for s in secrets:  # nothing from the query string is echoed back
        assert s not in body
    for remote in ("http://", "https://", "<script", "<link", "@import", "url("):
        assert remote not in body  # self-contained: nothing fetched from anywhere


@pytest.mark.parametrize("which,title", [("gmail", "Gmail connected"), ("calendar", "Google Calendar connected")])
def test_browser_gets_a_branded_finish_page(monkeypatch, which, title):
    import http.server
    from google_auth_oauthlib import flow as oauthlib_flow

    original_app = oauthlib_flow._RedirectWSGIApp
    pages = []
    _sign_in_through_real_loopback_server(monkeypatch, _sign_in_run(monkeypatch, which), pages=pages)
    [(status, headers, body)] = pages
    assert status == 200
    _assert_safe_page(headers, body, "4/test-code", "test-code", "state=")
    assert f"<h1>{title}</h1>" in body and "SafeContext" in body and "close this tab" in body
    assert oauthlib_flow._RedirectWSGIApp is original_app  # both patches are gone afterwards
    assert http.server.HTTPServer.server_bind is auth._ORIGINAL_SERVER_BIND


@pytest.mark.parametrize("which", ["gmail", "calendar"])
def test_cancel_on_consent_screen_shows_cancelled_page_and_app_error(monkeypatch, which):
    pages = []
    with pytest.raises(NotConfigured) as e:
        _sign_in_through_real_loopback_server(monkeypatch, _sign_in_run(monkeypatch, which),
                                              reply={"error": "access_denied"}, pages=pages)
    assert e.value.args[0] == auth.CANCELLED_MESSAGE  # the app shows its own error, not a crash
    [(status, headers, body)] = pages
    _assert_safe_page(headers, body, "access_denied")
    assert "<h1>Sign-in was cancelled</h1>" in body and "Return to SafeContext to try again." in body
    assert "connected" not in body


def test_finish_page_variants_never_echo_the_query():
    secret_code, secret_state = "4/0AUn6Hd84e-SECRET", "5xzGAPZN2siMKKAt"
    ok = auth.finish_page(f"state={secret_state}&code={urllib.parse.quote(secret_code)}&scope=x", "Gmail")
    assert "<h1>Gmail connected</h1>" in ok and 'class="ok"' in ok
    other = auth.finish_page("state=s&error=%3Cscript%3Ealert(1)%3C/script%3E", "Gmail")
    assert "<h1>Sign-in didn’t finish</h1>" in other and "<script" not in other and "alert" not in other
    empty = auth.finish_page("", "Google Calendar")
    assert 'class="bad"' in empty and "connected" not in empty
    for page in (ok, other, empty):
        assert secret_code not in page and secret_state not in page
        assert "prefers-color-scheme:dark" in page  # follows the Mac's light/dark setting
    assert "&lt;b&gt;" in auth.finish_page("code=c", "<b>")  # the source name is escaped


def test_only_loopback_addresses_skip_the_lookup():
    assert auth._is_loopback("127.0.0.1") and auth._is_loopback("::1")
    assert not auth._is_loopback("localhost") and not auth._is_loopback("192.168.1.2")


# --- token storage --------------------------------------------------------------

def test_tokens_live_in_keychain_not_files(mem_keyring, tmp_path):
    TokenStore().save("alex@example.org", creds())
    assert (auth.KEYRING_SERVICE, "alex@example.org") in mem_keyring.items
    loaded = TokenStore().load("alex@example.org")
    assert loaded.refresh_token == "1//refresh-test" and loaded.scopes == [auth.SCOPE_READONLY]


def test_delete_missing_token_is_fine():
    TokenStore().delete("nobody@example.org")


# --- every way to lose access -> NeedsReconnect ---------------------------------

def test_not_connected():
    with pytest.raises(NeedsReconnect) as e:
        get_credentials("")
    assert e.value.reason == "not_connected"
    with pytest.raises(NeedsReconnect):
        get_credentials("alex@example.org")


def test_scope_unticked_on_consent_screen():
    TokenStore().save("a@example.org", creds(scope=auth.SCOPE_METADATA))
    with pytest.raises(NeedsReconnect) as e:
        get_credentials("a@example.org", mode="full")
    assert e.value.reason == "scope_not_granted"


@pytest.mark.parametrize("error", [
    "invalid_grant: Token has been expired or revoked.",
    "('invalid_grant: Bad Request', {'error': 'invalid_grant', 'error_description': 'Bad Request'})",
])
def test_invalid_grant_on_refresh(monkeypatch, error):
    TokenStore().save("a@example.org", creds(expired=True))

    def fail(self, request):
        raise RefreshError(error)

    monkeypatch.setattr(Credentials, "refresh", fail)
    with pytest.raises(NeedsReconnect) as e:
        get_credentials("a@example.org")
    assert e.value.reason == "revoked_or_expired"
    assert "Reconnect Gmail" in e.value.message


@pytest.mark.parametrize("exc", [RefreshError("temporarily_unavailable"), TransportError("connection reset")])
def test_network_trouble_is_not_a_reconnect(monkeypatch, exc):
    TokenStore().save("a@example.org", creds(expired=True))

    def fail(self, request):
        raise exc

    monkeypatch.setattr(Credentials, "refresh", fail)
    with pytest.raises(TransientAuthError):
        get_credentials("a@example.org")


def test_successful_refresh_is_saved(monkeypatch, mem_keyring):
    TokenStore().save("a@example.org", creds(expired=True))

    def ok(self, request):
        self.token = "ya29.new"
        self.expiry = _utcnow() + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", ok)
    assert get_credentials("a@example.org").token == "ya29.new"
    assert json.loads(mem_keyring.items[(auth.KEYRING_SERVICE, "a@example.org")])["token"] == "ya29.new"


def test_valid_creds_not_refreshed(monkeypatch):
    TokenStore().save("a@example.org", creds())
    monkeypatch.setattr(Credentials, "refresh", lambda self, r: pytest.fail("should not refresh"))
    get_credentials("a@example.org")


# --- disconnect ------------------------------------------------------------------

def test_disconnect_revokes_with_token_in_body(monkeypatch, mem_keyring):
    TokenStore().save("a@example.org", creds())
    sent = []

    class Resp:
        def close(self):
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: sent.append(req) or Resp())
    auth.disconnect("a@example.org")
    (req,) = sent
    assert "refresh-test" not in req.full_url and b"refresh-test" in req.data
    assert mem_keyring.items == {}


def test_disconnect_offline_still_forgets(monkeypatch, mem_keyring):
    TokenStore().save("a@example.org", creds())

    def offline(req, timeout):
        raise OSError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    auth.disconnect("a@example.org")
    assert mem_keyring.items == {}


# --- Gmail client error mapping ------------------------------------------------

class _Resp(dict):
    def __init__(self, status):
        super().__init__(status=str(status))
        self.status = status
        self.reason = "x"


def _http_error(status, content=b"{}"):
    from googleapiclient.errors import HttpError
    return HttpError(_Resp(status), content)


class _Req:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def execute(self, num_retries=0):
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


@pytest.fixture
def client():
    from safecontext.gmail_api import GoogleGmailAPI
    return GoogleGmailAPI(creds(), sleep=lambda s: None)


def test_rate_limit_retried(client):
    req = _Req([_http_error(429), _http_error(403, b'{"error":{"errors":[{"reason":"userRateLimitExceeded"}]}}'),
                _http_error(503), {"ok": 1}])
    assert client._execute(req, 5) == {"ok": 1}


def test_401_means_reconnect(client):
    with pytest.raises(NeedsReconnect):
        client._execute(_Req([_http_error(401)]), 5)


def test_refresh_error_inside_api_call_means_reconnect(client):
    with pytest.raises(NeedsReconnect):
        client._execute(_Req([RefreshError("invalid_grant: Token has been expired or revoked.")]), 5)


def test_insufficient_scope_means_reconnect(client):
    with pytest.raises(NeedsReconnect):
        client._execute(_Req([_http_error(403, b'{"error":{"message":"Request had insufficient '
                                                b'authentication scopes."}}')]), 5)


def test_persistent_failure_is_transient(client):
    from safecontext.gmail_api import TransientError
    with pytest.raises(TransientError):
        client._execute(_Req([_http_error(503)] * 10), 5)


def test_history_404_is_expired(client, monkeypatch):
    from safecontext.gmail_api import HistoryExpired
    monkeypatch.setattr(client, "_execute", lambda req, units: (_ for _ in ()).throw(_http_error(404)))
    with pytest.raises(HistoryExpired):
        client.list_history("1", None)


def test_keychain_unavailable_is_clear():
    from keyring.backends.fail import Keyring as FailKeyring
    keyring.set_keyring(FailKeyring())
    with pytest.raises(NotConfigured, match="Keychain"):
        get_credentials("a@example.org")
    TokenStore().delete("a@example.org")  # must not raise


# --- rate limiter ------------------------------------------------------------

class _FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept += s
        self.t += s


def test_rate_limiter_handles_batches_bigger_than_the_bucket():
    """Regression: a 50-message batch costs 250 units but the bucket holds 200.
    It used to wait forever; it must proceed and repay the debt later."""
    from safecontext.gmail_api import _TokenBucket
    clock = _FakeClock()
    b = _TokenBucket(200, 200, clock=clock.now, sleep=clock.sleep)
    for _ in range(4):
        b.take(250)  # must return
    # 1000 units at 200/s, starting with a full bucket of 200: about 4 seconds of waiting
    assert 3.5 <= clock.slept <= 4.5


def test_rate_limiter_keeps_the_average_rate():
    from safecontext.gmail_api import _TokenBucket
    clock = _FakeClock()
    b = _TokenBucket(200, 200, clock=clock.now, sleep=clock.sleep)
    for _ in range(100):
        b.take(5)
    assert clock.slept == pytest.approx((500 - 200) / 200, rel=0.01)


def test_real_client_uses_a_bucket_that_fits_a_batch():
    from safecontext.gmail_api import QUOTA_UNITS, GoogleGmailAPI
    c = GoogleGmailAPI(creds(), sleep=lambda s: None)
    c._bucket.take(QUOTA_UNITS["get"] * 50)  # returns without hanging


def test_per_minute_quota_waits_for_the_window_to_roll_over():
    from safecontext.gmail_api import PER_MINUTE_WAIT, GoogleGmailAPI
    waits = []
    c = GoogleGmailAPI(creds(), sleep=waits.append)
    quota = _http_error(403, b'{"error":{"code":403,"message":"Quota exceeded for quota metric \'Total Query Cost\' '
                             b'and limit \'Units per minute per user\'","errors":[{"reason":"rateLimitExceeded"}]}}')
    assert c._execute(_Req([quota, {"ok": 1}]), 5) == {"ok": 1}
    assert waits and waits[0] >= PER_MINUTE_WAIT


def test_plain_rate_limit_backs_off_more_than_a_second():
    from safecontext.gmail_api import GoogleGmailAPI
    waits = []
    c = GoogleGmailAPI(creds(), sleep=waits.append)
    assert c._execute(_Req([_http_error(429), {"ok": 1}]), 5) == {"ok": 1}
    assert waits[0] >= 5


def test_defaults_are_gentle():
    from safecontext.gmail_api import BATCH_SIZE, DEFAULT_UNITS_PER_SECOND
    assert BATCH_SIZE <= 10 and DEFAULT_UNITS_PER_SECOND <= 85  # under the 6,000 units/min (100/s) per-user quota


# --- adaptive pace -------------------------------------------------------------

def _per_minute_error():
    return _http_error(403, b'{"error":{"message":"Quota exceeded for quota metric \'Total Query Cost\' and limit '
                            b'\'Units per minute per user\'","errors":[{"reason":"rateLimitExceeded"}]}}')


def test_pace_drops_when_the_per_minute_quota_is_hit():
    from safecontext.gmail_api import GoogleGmailAPI
    clock = _FakeClock()
    c = GoogleGmailAPI(creds(), units_per_second=80, sleep=clock.sleep, clock=clock.now)
    c._execute(_Req([_per_minute_error(), {"ok": 1}]), 5)
    assert c.rate == pytest.approx(48)
    for _ in range(20):
        c._execute(_Req([_per_minute_error(), {"ok": 1}]), 5)
    assert c.rate == pytest.approx(2.0)  # floor


def test_pace_recovers_slowly_after_clean_minutes_but_never_exceeds_the_cap():
    from safecontext.gmail_api import GoogleGmailAPI
    clock = _FakeClock()
    c = GoogleGmailAPI(creds(), units_per_second=80, sleep=clock.sleep, clock=clock.now)
    c._execute(_Req([_per_minute_error(), {"ok": 1}]), 5)
    slowed = c.rate
    c._execute(_Req([{"ok": 1}]), 5)
    assert c.rate == slowed  # not yet a clean minute
    clock.t += 61
    c._execute(_Req([{"ok": 1}]), 5)
    assert c.rate == pytest.approx(slowed * 1.1)
    for _ in range(50):
        clock.t += 61
        c._execute(_Req([{"ok": 1}]), 5)
    assert c.rate == pytest.approx(80)
