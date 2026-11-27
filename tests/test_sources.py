"""The connector interface: wrapping a plain reader (an iterator of dataclasses,
like the modules coming in readers/) takes a few lines; sources sync
independently; disconnecting one Google source doesn't revoke the other; the
incremental Calendar grant leaves Gmail's token alone."""
import time
from datetime import datetime, timedelta, timezone
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional

import pytest

from safecontext import auth
from safecontext.app.backend import Backend
from safecontext.app.demo import DEMO_ACCOUNT
from safecontext.demo.fake_calendar import make_fake_calendar
from safecontext.demo.fake_gmail import FakeGmail
from safecontext.demo.synthetic import mailbox
from safecontext.errors import NeedsPermission
from safecontext.scrub import RawItem
from safecontext.sources import default_sources
from safecontext.sources.base import FOLDER, MACOS_PERMISSION, ReaderSource, Requirement
from safecontext.store import Store
from safecontext.tools import run_tool


# --- a reader-backed source, written the way readers/ modules will be wrapped ---------

@dataclass
class ChatRecord:  # the shape a reader yields: a plain dataclass
    rowid: int
    chat: str
    handle: str
    text: str
    sent_ms: int


class FakeReader:
    def __init__(self, records: List[ChatRecord]) -> None:
        self.records = records
        self.allowed = True
        self.reads: List[Optional[str]] = []

    def messages(self, since_rowid: int = 0):
        return (r for r in self.records if r.rowid > since_rowid)


class ChatSource(ReaderSource):
    id = "testchat"
    name = "Test chat"
    kinds = ("chat",)
    requirements = (Requirement(MACOS_PERMISSION, "full_disk_access", "Full Disk Access", "To read chats."),)

    def __init__(self, store, reader):
        super().__init__(store)
        self.reader = reader

    def check_access(self):
        if not self.reader.allowed:
            raise NeedsPermission("Give SafeContext Full Disk Access in System Settings.", "full_disk_access")

    def read(self, cursor):
        self.reader.reads.append(cursor)
        return self.reader.messages(int(cursor or 0))

    def to_item(self, r):
        return RawItem(id=str(r.rowid), kind="chat", thread_id=r.chat, date_ms=r.sent_ms, sender=r.handle,
                       participants=[r.handle, "me"], text=r.text)

    def cursor_of(self, r):
        return str(r.rowid)


def chats(n=5, start=1):
    texts = {2: "Your verification code is 482913", 4: "my card is 4111 1111 1111 1111"}
    return [ChatRecord(i, "chat-jamie", "+14155550134", texts.get(i, f"see you at {i}pm"), 1_700_000_000_000 + i)
            for i in range(start, start + n)]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data" / "mail.db")
    yield s
    s.close()


def test_reader_source_syncs_scrubs_and_keeps_a_cursor(store):
    reader = FakeReader(chats(5))
    src = ChatSource(store, reader)
    src.batch_size = 2
    assert src.connect() == "This Mac"
    r = src.sync()
    assert r.status == "ok" and (r.kept, r.dropped) == (4, 1)
    assert store.state("testchat").get("cursor") == "5"
    assert dict(store._db.execute("SELECT id, reason FROM dropped WHERE source='testchat'").fetchall()) == \
        {"2": "security:body"}
    (h,) = store.search("card", source="testchat")
    assert "4111" not in h.snippet and h.kind == "chat"
    reader.records += chats(2, start=6)
    r = src.sync()
    assert r.phase == "incremental" and r.fetched == 2 and reader.reads[-1] == "5"
    assert store.counts("testchat") == (6, 1)


def test_reader_source_missing_permission(store):
    reader = FakeReader(chats(3))
    src = ChatSource(store, reader)
    src.connect()
    reader.allowed = False
    r = src.sync()
    assert r.status == "needs_permission" and "Full Disk Access" in r.message
    st = src.status()
    assert st.state == "needs_permission" and "Full Disk Access" in st.message
    reader.allowed = True
    assert src.sync().status == "ok" and src.status().state == "connected"


def test_reader_connect_needs_permission_first(store):
    reader = FakeReader(chats(1))
    reader.allowed = False
    with pytest.raises(NeedsPermission):
        ChatSource(store, reader).connect()
    assert not ChatSource(store, reader).connected


def test_reader_bad_record_is_dropped_and_cursor_still_advances(store):
    reader = FakeReader(chats(3))
    src = ChatSource(store, reader)
    orig = src.to_item
    src.to_item = lambda r: (_ for _ in ()).throw(ValueError("odd")) if r.rowid == 2 else orig(r)
    src.connect()
    r = src.sync()
    assert r.status == "ok" and r.errors == {"ValueError": 1}
    assert store.state("testchat").get("cursor") == "3"


def test_reader_disconnect_wipes_its_data(store):
    src = ChatSource(store, FakeReader(chats(3)))
    src.connect()
    src.sync()
    src.disconnect()
    assert store.counts("testchat") == (0, 0) and not src.connected


def test_tools_reach_reader_sources(store):
    src = ChatSource(store, FakeReader(chats(3)))
    src.connect()
    src.sync()
    r = run_tool(store, "search", {"query": "see you", "source": "testchat"})
    assert r["count"] == 2 and r["results"][0]["kind"] == "chat"
    t = run_tool(store, "read", {"thread_id": "chat-jamie"})
    assert [m["body"] for m in t["messages"]] == ["see you at 1pm", "see you at 3pm"]


# --- the registry ----------------------------------------------------------------------------

def test_statuses_include_coming_soon_cards(store):
    st = default_sources(store).statuses()
    by = {s["id"]: s for s in st}
    assert list(by)[:3] == ["gmail", "gcal", "imessage"]
    assert by["imessage"]["available"] and by["imessage"]["state"] == "not_connected"
    for cid in ("whatsapp", "recordings", "dropbox"):
        assert by[cid]["state"] == "coming_soon" and not by[cid]["available"] and by[cid]["requirements"]
    assert any(r["kind"] == FOLDER for r in by["recordings"]["requirements"])
    gcal_req = by["gcal"]["requirements"][0]
    assert gcal_req["value"] == auth.SCOPE_CALENDAR and gcal_req["why"]


def test_link_allowlist(store):
    s = default_sources(store)
    assert s.link_allowed("https://mail.google.com/mail/#all/1")
    assert s.link_allowed("https://www.google.com/calendar/event?eid=abc")
    for bad in ("https://www.dropbox.com/home/Notes/a.txt", "https://evil.example/", "javascript:alert(1)", "https://www.google.com.evil/calendar/", None):
        assert not s.link_allowed(bad)


def test_disconnecting_one_google_source_keeps_the_shared_grant(store):
    calls = []
    s = default_sources(store, revoke_fn=lambda account, revoke=True: calls.append(revoke))
    store.state("gmail").set("account", DEMO_ACCOUNT)
    store.state("gcal").set("account", DEMO_ACCOUNT)
    s.disconnect("gcal")
    assert calls == [False]  # Gmail still uses the grant: forget Calendar's token, don't revoke
    s.disconnect("gmail", wipe=True)
    assert calls == [False, True]  # last Google source: revoke


# --- Calendar's incremental grant ---------------------------------------------------------------

CLIENT = {"installed": {
    "client_id": "123-test.apps.googleusercontent.com", "client_secret": "GOCSPX-test-not-secret",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
    "redirect_uris": ["http://localhost"]}}


def test_calendar_grant_is_incremental_and_separate(monkeypatch, fake_keyring):
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    gmail_token = '{"token": "gmail"}'
    fake_keyring.set_password(auth.KEYRING_SERVICE, DEMO_ACCOUNT, gmail_token)
    seen = {}

    def fake_run(self, **kw):
        self.redirect_uri = "http://127.0.0.1:5000/"
        seen["url"], _ = self.authorization_url(**{k: v for k, v in kw.items()
                                                    if k not in ("host", "port", "open_browser", "timeout_seconds")})
        c = Credentials(token="t", refresh_token="r", client_id="c", client_secret="s",
                        token_uri="https://oauth2.googleapis.com/token",
                        scopes=[auth.SCOPE_CALENDAR, auth.SCOPE_READONLY])
        c.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)  # no refresh, no network
        return c

    monkeypatch.setattr(InstalledAppFlow, "run_local_server", fake_run)
    monkeypatch.setattr(auth, "fetch_calendar_account", lambda creds: DEMO_ACCOUNT)
    _, account = auth.connect_calendar("alex@example.org", client_config=CLIENT)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    assert q["scope"] == [auth.SCOPE_CALENDAR]                  # only Calendar is asked for
    assert q["include_granted_scopes"] == ["true"] and q["access_type"] == ["offline"]
    assert q["code_challenge_method"] == ["S256"]
    assert fake_keyring.get_password(auth.KEYRING_SERVICE, DEMO_ACCOUNT) == gmail_token  # Gmail untouched
    assert fake_keyring.get_password(auth.CALENDAR_KEYRING_SERVICE, DEMO_ACCOUNT)
    assert auth.get_calendar_credentials(DEMO_ACCOUNT).refresh_token == "r"


def test_calendar_credentials_not_connected_message():
    with pytest.raises(auth.NeedsReconnect) as e:
        auth.get_calendar_credentials(DEMO_ACCOUNT)
    assert "Google Calendar" in e.value.message


# --- the app backend with two sources -------------------------------------------------------------

def wait_idle(b, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        s = b.status()
        if not any(x["syncing"] for x in s["sources"]):
            return s
        time.sleep(0.05)
    raise AssertionError("sync did not finish")


@pytest.fixture
def gmail():
    g = FakeGmail(page_size=5, email=DEMO_ACCOUNT)
    for m in mailbox():
        g.add(m, record=False)
    return g


@pytest.fixture
def backend(tmp_path, gmail):
    cal = make_fake_calendar(DEMO_ACCOUNT)
    b = Backend(tmp_path / "data", demo=True, gmail_factory=lambda a, m: gmail,
                connect_fn=lambda mode, email: DEMO_ACCOUNT, calendar_factory=lambda a: cal,
                calendar_connect_fn=lambda email: DEMO_ACCOUNT, auto_sync=False)
    b._cal = cal
    yield b
    b._shutdown()


def by_id(status):
    return {x["id"]: x for x in status["sources"]}


def test_connect_calendar_from_the_sources_screen(backend):
    assert backend.connect_source("gcal", {"email": ""}) == {"ok": True, "account": DEMO_ACCOUNT}
    s = wait_idle(backend)
    g = by_id(s)["gcal"]
    assert g["state"] == "connected" and g["item_count"] == 6 and g["last_sync_ms"]
    assert s["any_connected"] and s["account"] is None  # Gmail isn't connected


def test_one_failing_source_does_not_block_another(backend, gmail):
    backend.connect("full")
    backend.connect_source("gcal")
    wait_idle(backend)
    backend._cal.fail_list_call = backend._cal.calls["events"] + 1
    gmail.add(next(m for m in mailbox() if m.id == "m0001"))
    assert backend.sync_now()["started"]
    s = wait_idle(backend)
    assert by_id(s)["gcal"]["state"] == "error" and by_id(s)["gcal"]["last_result"]["status"] == "retry_later"
    assert by_id(s)["gmail"]["state"] == "connected" and s["last_result"]["status"] == "ok"


def test_sync_one_source(backend):
    backend.connect_source("gcal")
    wait_idle(backend)
    assert backend.sync_source("gcal")["started"]
    assert backend.sync_source("nope")["ok"] is False


def test_disconnect_source_wipes_only_it(backend):
    backend.connect("full")
    backend.connect_source("gcal")
    wait_idle(backend)
    kept = backend.status()["kept"]
    assert backend.disconnect_source("gcal")["ok"]
    s = backend.status()
    assert by_id(s)["gcal"]["state"] == "not_connected" and by_id(s)["gcal"]["item_count"] == 0
    assert s["kept"] == kept and s["account"] == DEMO_ACCOUNT


def test_search_across_sources_in_the_app(backend):
    backend.connect("full")
    backend.connect_source("gcal")
    wait_idle(backend)
    r = backend.search("rent")
    assert {x["source"] for x in r["results"]} == {"gmail", "gcal"}
    assert {x["source"] for x in backend.search("rent", source="gcal")["results"]} == {"gcal"}
    ev = backend.recent(source="gcal")["results"][0]
    t = backend.read_thread(ev["thread_id"], "gcal")
    assert t["ok"] and t["messages"][0]["kind"] == "event"


def test_open_link_allows_calendar(backend, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
    assert backend.open_link("https://www.google.com/calendar/event?eid=abc")["ok"]
    assert backend.open_link("https://evil.example/")["ok"] is False
    assert backend.open_in_gmail("https://www.google.com/calendar/event?eid=abc")["ok"] is False
    assert opened == ["https://www.google.com/calendar/event?eid=abc"]


def test_privacy_report_per_source(backend):
    backend.connect("full")
    backend.connect_source("gcal")
    wait_idle(backend)
    r = backend.privacy_report()
    by = {x["id"]: (x["kept"], x["dropped"]) for x in r["by_source"]}
    assert by["gcal"] == (6, 2) and by["gmail"] == backend._store.counts("gmail")
    assert r["kept"] == 13


def test_unknown_source(backend):
    assert backend.connect_source("fax")["ok"] is False
    assert backend.disconnect_source("fax")["ok"] is False

