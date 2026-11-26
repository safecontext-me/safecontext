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


def test_registry_lists_dropbox(store, monkeypatch):
    monkeypatch.setenv("SAFECONTEXT_DROPBOX_APP_KEY", "test-app-key")
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


def _no_key(monkeypatch, tmp_path):
    monkeypatch.delenv("SAFECONTEXT_DROPBOX_APP_KEY", raising=False)
    monkeypatch.delenv("MYCONTEXT_DROPBOX_APP_KEY", raising=False)
    monkeypatch.setattr(dropbox_auth, "BUNDLED_APP_CONFIG", tmp_path / "missing" / "dropbox_app.json")


def test_missing_app_key_does_not_call_the_network(monkeypatch, tmp_path):
    _no_key(monkeypatch, tmp_path)
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


# --- release readiness: bundled key, registered redirect, PKCE without a secret ---

def test_bundled_app_key_and_env_override(monkeypatch, tmp_path):
    _no_key(monkeypatch, tmp_path)
    cfg = tmp_path / "dropbox_app.json"
    monkeypatch.setattr(dropbox_auth, "BUNDLED_APP_CONFIG", cfg)
    assert not dropbox_auth.configured()
    cfg.write_text(json.dumps({"app_key": "bundled-key"}))
    assert dropbox_auth.app_key() == "bundled-key" and dropbox_auth.configured()
    monkeypatch.setenv("SAFECONTEXT_DROPBOX_APP_KEY", "dev-key")
    assert dropbox_auth.app_key() == "dev-key"
    cfg.write_text("not json")
    monkeypatch.delenv("SAFECONTEXT_DROPBOX_APP_KEY")
    assert not dropbox_auth.configured()


def test_card_says_unavailable_without_a_key(store, monkeypatch, tmp_path):
    _no_key(monkeypatch, tmp_path)
    by = {s["id"]: s for s in default_sources(store).statuses()}
    assert by["dropbox"]["state"] == "unavailable"
    assert by["dropbox"]["message"] == dropbox_auth.NOT_IN_BUILD
    # A connected account keeps its normal state (and can still be disconnected).
    store.state("dropbox").set("account", ME)
    store.state("dropbox").set("auth_state", "ok")
    by = {s["id"]: s for s in default_sources(store).statuses()}
    assert by["dropbox"]["state"] == "connected"


def test_no_app_secret_is_sent():
    sent = []

    def opener(req, timeout=60):
        sent.append(urllib.parse.parse_qs(req.data.decode()))
        return _Resp(json.dumps({"access_token": "a", "refresh_token": "r", "expires_in": 60}).encode())

    dropbox_auth.exchange_code("key", "code", "verifier", "http://localhost:47811/", opener=opener)
    dropbox_auth.refresh_token("key", "r", opener=opener)
    for fields in sent:
        assert "client_secret" not in fields and fields["client_id"] == ["key"]
    assert sent[0]["code_verifier"] == ["verifier"] and sent[0]["redirect_uri"] == ["http://localhost:47811/"]
    assert "APP_SECRET" not in inspect.getsource(dropbox_auth)


def test_redirect_uses_a_registered_port_and_skips_a_busy_one():
    import socket
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    taken = busy.getsockname()[1]
    try:
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        spare = free.getsockname()[1]
        free.close()
        port, servers = dropbox_auth._bind_redirect((taken, spare))
        assert port == spare
        for s in servers:
            s.server_close()
        with pytest.raises(auth.NotConfigured):
            dropbox_auth._bind_redirect((taken,))
    finally:
        busy.close()


def test_browser_roundtrip_gets_the_code_on_localhost(monkeypatch):
    import socket
    import threading
    import urllib.request
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(dropbox_auth, "REDIRECT_PORTS", (port,))
    urls = []
    monkeypatch.setattr(dropbox_auth.webbrowser, "open", lambda url: urls.append(url) or threading.Thread(
        target=lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/?code=abc&state=x", timeout=5).read()
    ).start())
    redirect, query = dropbox_auth._browser_roundtrip("key", "challenge", True, timeout=10)
    assert redirect == f"http://localhost:{port}/"
    assert query["code"] == ["abc"]
    q = urllib.parse.parse_qs(urllib.parse.urlparse(urls[0]).query)
    assert q["redirect_uri"] == [redirect] and q["client_id"] == ["key"]


def test_http_client_asks_for_a_fresh_token_each_request():
    tokens = iter(["t1", "t2"])
    seen = []

    def opener(req, timeout=60):
        seen.append(req.get_header("Authorization"))
        return _Resp(json.dumps({"entries": [], "cursor": "c", "has_more": False}).encode())

    client = DropboxHTTP(lambda: next(tokens), opener=opener)
    client.list_folder(None)
    client.list_folder("c")
    assert seen == ["Bearer t1", "Bearer t2"]


# --- scrubbing realistic file names and documents ---

SENSITIVE_FILES = [
    ("/Docs/Chase Statement March.pdf", None),
    ("/bank_statement_2025-03.csv", b"date,amount\n2025-03-01,12.00\n"),
    ("/Taxes/2024/receipt.pdf", None),
    ("/W-2 2025.pdf", None),
    ("/Medical/notes.txt", b"knee feels better"),
    ("/Scans/passport scan.jpg", None),
    ("/drivers_license.png", None),
    ("/Insurance card.jpg", None),
    ("/passwords.txt", b"gmail: hunter2"),
    ("/.ssh/id_rsa", b"-----BEGIN OPENSSH PRIVATE KEY-----"),
    ("/proj/.env", b"TOKEN=abc"),
    ("/payslip march.pdf", None),
]
SENSITIVE_TEXT = [
    ("/Notes/mri.txt", b"Patient: Alex\nFindings: herniated disc L4-L5"),
    ("/Notes/march.csv", b"Statement period: Mar 1 - Mar 31\nEnding balance: $1,203.11"),
    ("/Notes/meds.md", b"Medications: sertraline 50mg"),
]
KEPT_FILES = [
    ("/groceries.txt", b"oat milk, eggs"),
    ("/Trip itinerary.md", b"Flight to Lisbon on May 3"),
    ("/Work/Q3 plan.md", b"Ship the account settings page"),
    ("/Work/Roadmap 2026.key", None),  # Keynote, not a private key
    ("/Photos/beach.jpg", None),
]


def test_sensitive_files_are_dropped_by_name_or_content(store):
    box = FakeDropbox(ME)
    for i, (path, body) in enumerate(SENSITIVE_FILES + SENSITIVE_TEXT + KEPT_FILES):
        name = path.rsplit("/", 1)[1]
        box.add(f"id:{i}", name, path, body, size=None if body is not None else 1000)
    r = DropboxSyncer(store, box, account=ME).run()
    assert r.status == "ok"
    n_name, n_text = len(SENSITIVE_FILES), len(SENSITIVE_TEXT)
    gone = dropped(store)
    for i in range(n_name):
        assert gone[f"id:{i}"] == "sensitive:subject", SENSITIVE_FILES[i]
        assert f"id:{i}" not in box.downloaded  # dropped before its bytes were fetched
    for i in range(n_name, n_name + n_text):
        assert gone[f"id:{i}"] == "sensitive:text", SENSITIVE_TEXT[i - n_name]
    assert set(items(store)) == {f"id:{i}" for i in range(n_name + n_text, n_name + n_text + len(KEPT_FILES))}
    blob = everything(store)
    for needle in ("hunter2", "herniated", "1,203.11", "sertraline", "passport", "Statement"):
        assert needle not in blob


def test_a_download_failure_is_retried_not_stored_as_dropped(store):
    class Flaky(FakeDropbox):
        fail = True

        def download(self, file_id):
            if self.fail:
                from safecontext.errors import TransientError
                raise TransientError("429")
            return super().download(file_id)

    box = Flaky(ME)
    box.add("id:a", "a.txt", "/a.txt", b"apples")
    r = DropboxSyncer(store, box, account=ME).run()
    assert r.status == "retry_later"
    assert items(store) == {} and dropped(store) == {}
    box.fail = False
    assert DropboxSyncer(store, box, account=ME).run().status == "ok"
    assert set(items(store)) == {"id:a"}


def test_documents_keep_dividers_and_blockquotes(store):
    box = FakeDropbox(ME)
    box.add("id:md", "plan.md", "/plan.md", b"# Plan\n--\nPart two: budget\n> a quoted idea\nThanks,\nAlex\nShip date May 3")
    DropboxSyncer(store, box, account=ME).run()
    body = items(store)["id:md"]["body"]
    assert "Part two" in body and "a quoted idea" in body and "Ship date May 3" in body
