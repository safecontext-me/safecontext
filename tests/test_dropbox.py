"""Dropbox: file mapping, scrubbing, cursor sync, and read-only sign-in.

All against FakeDropbox or a stand-in HTTP opener. No network and no real account.
"""
import inspect
import io
import json
import urllib.error
import urllib.parse

import pytest

from safecontext import auth, dropbox_auth
from safecontext.demo.fake_dropbox import FakeDropbox
from safecontext.dropbox_api import DropboxHTTP
from safecontext.sources import default_sources
from safecontext.sources.dropbox import DROPBOX, DropboxSource, DropboxSyncer, file_to_item, should_download
from safecontext.store import Store
from safecontext.tools import run_tool

ME = "alex@example.org"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data" / "mail.db")
    yield s
    s.close()


def items(store):
    return {r["id"]: dict(r) for r in store._db.execute("SELECT * FROM items WHERE source='dropbox'")}


def dropped(store):
    return dict(store._db.execute("SELECT id, reason FROM dropped WHERE source='dropbox'").fetchall())


def everything(store):
    return json.dumps([dict(r) for r in store._db.execute(
        "SELECT * FROM items WHERE source='dropbox'")], ensure_ascii=False)


def test_file_mapping_text_and_link():
    entry = {".tag": "file", "id": "id:notes", "name": "My notes.txt", "path_display": "/Notes/My notes.txt",
             "size": 11, "client_modified": "2026-01-02T03:04:05Z"}
    it = file_to_item(entry, b"hello there", account=ME)
    assert it.id == "id:notes" and it.kind == "message" and it.thread_id == "id:notes"
    assert it.title == "My notes.txt" and it.text == "hello there" and it.sender == ""
    assert it.date_ms == 1767323045000
    assert it.link == "https://www.dropbox.com/home/Notes/My%20notes.txt"
    assert it.extra["Path"] == "/Notes/My notes.txt" and it.extra["Account"] == ME
    assert it.attachments == []


def test_binary_and_folders_keep_no_bytes():
    pdf = {".tag": "file", "id": "id:pdf", "name": "scan.pdf", "path_display": "/scan.pdf", "size": 40,
           "client_modified": "2026-01-02T03:04:05Z"}
    it = file_to_item(pdf, None, account=ME)
    assert it.text == "" and it.html == ""
    assert it.attachments[0].filename == "scan.pdf" and it.attachments[0].mime_type == "application/pdf"
    assert it.attachments[0].size == 40
    assert file_to_item({".tag": "folder", "id": "id:dir", "name": "Notes"}, None, account=ME) is None
    assert not should_download(pdf)
    assert not should_download({".tag": "file", "name": "huge.txt", "size": 5_000_000, "is_downloadable": True})


def test_nul_bytes_are_not_stored_as_text():
    it = file_to_item({".tag": "file", "id": "id:bin", "name": "odd.txt", "path_display": "/odd.txt", "size": 4,
                       "client_modified": "2026-01-02T03:04:05Z"}, b"a\x00b", account=ME)
    assert it.text == "" and it.attachments[0].filename == "odd.txt"


def test_first_sync_scrubs_and_skips_binaries(store):
    box = FakeDropbox(ME, page_size=2)
    box.add("id:notes", "groceries.txt", "/groceries.txt",
            b"Milk. Card 4111 1111 1111 1111. Routing 021000021 account 123456789012. password is Sup3rSecretPwd.")
    box.add("id:tax", "tax return.txt", "/tax return.txt", b"see attached")
    box.add("id:pdf", "scan.pdf", "/scan.pdf", size=1200)
    box.add("id:dir", "Notes", "/Notes", tag="folder")
    box.add("id:html", "page.html", "/page.html", b"<p>Hello <b>there</b></p>")
    r = DropboxSyncer(store, box, account=ME).run()
    assert r.status == "ok" and r.phase == "backfill"
    got = items(store)
    assert set(got) == {"id:notes", "id:html", "id:pdf"}
    assert dropped(store)["id:tax"].startswith("sensitive:")
    assert "id:pdf" not in box.downloaded
    notes = got["id:notes"]
    blob = everything(store)
    for secret in ("4111 1111 1111 1111", "4111111111111111", "Sup3rSecretPwd", "021000021", "123456789012",
                   "tax return"):
        assert secret not in blob
    assert json.loads(notes["redactions"])
    assert got["id:html"]["body"]  # tags stripped, text kept
    assert "<b>" not in got["id:html"]["body"]
    assert store.state(DROPBOX).get("cursor")
    assert store.state(DROPBOX).get("auth_state") == "ok"


def test_search_finds_the_file(store):
    box = FakeDropbox(ME)
    box.add("id:notes", "groceries.txt", "/groceries.txt", b"buy oat milk from the corner shop")
    DropboxSyncer(store, box, account=ME).run()
    (h,) = store.search("oat milk", source=DROPBOX)
    assert h.title == "groceries.txt" and h.citation.link.startswith("https://www.dropbox.com/home/")
    found = run_tool(store, "search", {"query": "oat milk", "source": "dropbox"})
    assert found["count"] == 1 and found["results"][0]["source"] == "dropbox"


def test_resumed_listing_keeps_files_already_stored(store):
    box = FakeDropbox(ME, page_size=1)
    box.add("id:a", "a.txt", "/a.txt", b"first note about apples")
    box.add("id:b", "b.txt", "/b.txt", b"second note about berries")
    box.fail_list_at = 2  # the second page drops, after the first file is stored
    first = DropboxSyncer(store, box, account=ME).run()
    assert first.status == "retry_later"
    assert set(items(store)) == {"id:a"}
    assert store.state(DROPBOX).get("page_cursor")
    box.fail_list_at = None
    second = DropboxSyncer(store, box, account=ME).run()
    assert second.status == "ok"
    assert set(items(store)) == {"id:a", "id:b"}


def test_incremental_adds_and_removes(store):
    box = FakeDropbox(ME)
    box.add("id:notes", "groceries.txt", "/groceries.txt", b"buy oat milk")
    DropboxSyncer(store, box, account=ME).run()
    lists = box.calls["list"]
    box.add("id:more", "later.txt", "/later.txt", b"a second note about the trip")
    box.remove("id:notes")
    r = DropboxSyncer(store, box, account=ME).run()
    assert r.phase == "incremental" and r.deleted == 1
    assert box.calls["list"] > lists
    got = items(store)
    assert "id:notes" not in got and got["id:more"]["title"] == "later.txt"
    assert "id:notes" not in dropped(store)


def test_expired_cursor_resyncs(store):
    box = FakeDropbox(ME)
    box.add("id:notes", "groceries.txt", "/groceries.txt", b"buy oat milk")
    DropboxSyncer(store, box, account=ME).run()
    box.expire()
    box.add("id:more", "later.txt", "/later.txt", b"still here")
    r = DropboxSyncer(store, box, account=ME).run()
    assert r.status == "ok"
    assert set(items(store)) == {"id:notes", "id:more"}


def test_revoked_and_transient(store):
    box = FakeDropbox(ME)
    box.add("id:notes", "groceries.txt", "/groceries.txt", b"buy oat milk")
    box.revoked = True
    r = DropboxSyncer(store, box, account=ME).run()
    assert r.status == "needs_reconnect"
    box.revoked = False
    box.fail_list_at = 1
    r = DropboxSyncer(store, box, account=ME).run()
    assert r.status == "retry_later"
    assert items(store) == {}


def test_source_connect_disconnect_and_account_switch(store):
    box = FakeDropbox(ME)
    box.add("id:notes", "groceries.txt", "/groceries.txt", b"buy oat milk")
    revoked = []
    src = DropboxSource(store, api_factory=lambda account: box, connect_fn=lambda email: ME,
                        revoke_fn=lambda account, revoke=True: revoked.append((account, revoke)))
    assert src.connect() == ME
    assert src.sync().status == "ok" and items(store)
    src.connect_fn = lambda email: "other@example.org"
    with pytest.raises(auth.NotConfigured):
        src.connect()
    src.disconnect(wipe=True)
    assert revoked == [(ME, True)]
    assert items(store) == {} and not src.connected


def test_disconnect_does_not_touch_the_google_grant(store):
    google, drop = [], []
    s = default_sources(store, revoke_fn=lambda *a, **k: google.append(1),
                        dropbox_revoke=lambda *a, **k: drop.append(1))
    store.state("gmail").set("account", ME)
    store.state("gcal").set("account", ME)
    store.state("dropbox").set("account", ME)
    s.disconnect("dropbox")
    assert drop == [1] and google == []
    assert store.state("gmail").get("account") == ME


def test_registry_lists_dropbox(store):
    by = {s["id"]: s for s in default_sources(store).statuses()}
    assert by["dropbox"]["available"] and by["dropbox"]["state"] == "not_connected"
    assert by["dropbox"]["requirements"][0]["kind"] == "oauth_scope"
    assert "files.content.write" not in by["dropbox"]["requirements"][0]["value"]
    assert "files.content.read" in by["dropbox"]["requirements"][0]["value"]
    s = default_sources(store)
    assert s.link_allowed("https://www.dropbox.com/home/Notes/a.txt")
    assert not s.link_allowed("https://www.dropbox.com/s/evil")
    assert not s.link_allowed("https://evil.dropbox.com/home/a")


class _Resp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_http_client_is_read_only_and_maps_errors():
    src = inspect.getsource(DropboxHTTP)
    auth_src = inspect.getsource(dropbox_auth)
    for banned in ("files/upload", "files/delete", "files/move", "files/copy", "create_shared_link", "sharing"):
        assert banned not in src and banned not in auth_src
    assert dropbox_auth.SCOPES == ("account_info.read", "files.metadata.read", "files.content.read")
    seen = []

    def opener(req, timeout=60):
        seen.append(req.full_url)
        if req.full_url.endswith("/files/list_folder"):
            assert (req.get_header("Authorization") or "").startswith("Bearer ")
            page = {"entries": [], "cursor": "cur", "has_more": False}
            return _Resp(json.dumps(page).encode())
        raise urllib.error.HTTPError(req.full_url, 401, "no", hdrs=None, fp=io.BytesIO(b""))

    client = DropboxHTTP("token", opener=opener)
    page = client.list_folder(None)
    assert page["cursor"] == "cur" and seen == ["https://api.dropboxapi.com/2/files/list_folder"]
    with pytest.raises(auth.NeedsReconnect):
        client.download("id:x")
    assert seen[-1] == "https://content.dropboxapi.com/2/files/download"


def test_connect_stores_a_token_and_only_talks_to_dropbox(monkeypatch, fake_keyring):
    monkeypatch.setenv("SAFECONTEXT_DROPBOX_APP_KEY", "test-app-key")
    seen = []

    def opener(req, timeout=60):
        seen.append(req.full_url)
        if req.full_url == dropbox_auth.TOKEN_URL:
            body = {"access_token": "atk", "refresh_token": "rtk", "expires_in": 3600, "account_id": "dbid:1"}
            return _Resp(json.dumps(body).encode())
        if req.full_url == dropbox_auth.ACCOUNT_URL:
            return _Resp(json.dumps({"email": ME}).encode())
        raise AssertionError(req.full_url)

    monkeypatch.setattr(dropbox_auth, "_browser_roundtrip",
                        lambda client_id, challenge, open_browser: ("http://127.0.0.1:9/", {"code": ["abc"]}))
    account = dropbox_auth.connect(opener=opener, open_browser=False)
    assert account == ME
    assert seen == [dropbox_auth.TOKEN_URL, dropbox_auth.ACCOUNT_URL]
    saved = json.loads(fake_keyring.get_password(dropbox_auth.KEYRING_SERVICE, ME))
    assert saved["refresh_token"] == "rtk" and saved["email"] == ME


def test_missing_app_key_does_not_call_the_network(monkeypatch):
    monkeypatch.delenv("SAFECONTEXT_DROPBOX_APP_KEY", raising=False)
    monkeypatch.delenv("MYCONTEXT_DROPBOX_APP_KEY", raising=False)
    called = []
    monkeypatch.setattr(dropbox_auth, "_browser_roundtrip", lambda *a, **k: called.append(1))
    with pytest.raises(auth.NotConfigured):
        dropbox_auth.connect(opener=lambda *a, **k: called.append(2))
    assert called == []


def test_authorize_url_is_read_only():
    url = dropbox_auth.authorize_url("app", "challenge", "http://127.0.0.1:1/")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["scope"] == ["account_info.read files.metadata.read files.content.read"]
    assert q["code_challenge_method"] == ["S256"] and q["token_access_type"] == ["offline"]
    assert url.startswith("https://www.dropbox.com/oauth2/authorize?")
    assert "write" not in q["scope"][0]


def test_revoke_forgets_the_keychain_item(monkeypatch, fake_keyring):
    dropbox_auth.TokenStore().save(ME, {"access_token": "atk", "refresh_token": "rtk", "expires_at": 0})
    seen = []

    def opener(req, timeout=60):
        seen.append(req.full_url)
        raise urllib.error.URLError("down")

    dropbox_auth.revoke(ME, opener=opener)
    assert seen == [dropbox_auth.REVOKE_URL]
    assert fake_keyring.get_password(dropbox_auth.KEYRING_SERVICE, ME) is None
