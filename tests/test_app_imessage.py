"""iMessage on the Sources screen: connect, the missing Full Disk Access path, sync into the
shared store (scrubbed, searchable through the same tools as mail) and disconnect.

Only fixture databases built in a temp folder: conftest points HOME at a temporary
directory, and every Backend here is given an explicit db_path, so nothing can reach the
real ~/Library/Messages.
"""
import time

import pytest

from safecontext.app.backend import Backend
from safecontext.app.demo import DEMO_ACCOUNT, demo_backend_kwargs
from safecontext.demo.fake_messages import FakeFullDiskAccess, add_message, demo_names, make_messages_db
from safecontext.readers.imessage import AccessState, AccessStatus, default_db_path
from safecontext.tools import run_tool


def wait_idle(b, source="imessage", timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        src = next(x for x in b.sources()["sources"] if x["id"] == source)
        if not src["syncing"]:
            return src
        time.sleep(0.05)
    raise AssertionError("sync did not finish")


def card(b, source="imessage"):
    return next(x for x in b.status()["sources"] if x["id"] == source)


@pytest.fixture
def chat_db(tmp_path):
    return make_messages_db(tmp_path / "Messages" / "chat.db")


@pytest.fixture
def access():
    return FakeFullDiskAccess(granted=True)


@pytest.fixture
def opened():
    return []


@pytest.fixture
def backend(tmp_path, chat_db, access, opened):
    def settings():
        opened.append("fda")
        return access.open_settings()

    b = Backend(tmp_path / "data", demo=True, gmail_factory=lambda a, m: None,
                connect_fn=lambda mode, email: DEMO_ACCOUNT, auto_sync=False,
                imessage_options={"db_path": chat_db, "access_fn": access.check, "settings_fn": settings,
                                  "names_fn": demo_names})
    yield b
    b._shutdown()


def test_imessage_is_a_connectable_source(backend):
    c = card(backend)
    assert c["available"] and c["state"] == "not_connected" and c["name"] == "iMessage"
    assert c["kinds"] == ["chat"] and c["permission"] is None
    assert [r["label"] for r in c["requirements"]] == ["Full Disk Access"]
    assert "imessage" not in [x["id"] for x in backend.status()["sources"] if x["state"] == "coming_soon"]


def test_connect_syncs_scrubs_and_counts(backend):
    r = backend.connect_source("imessage")
    assert r == {"ok": True, "account": "This Mac"}
    c = wait_idle(backend)
    # 8 demo messages: the bank's verification code is dropped, the rest kept
    assert c["state"] == "connected" and c["item_count"] == 7 and c["dropped_count"] == 1
    assert c["last_result"]["status"] == "ok" and c["first_sync_done"]
    assert backend.status()["any_connected"]


def test_synced_messages_show_up_in_search_and_tools(backend):
    backend.connect_source("imessage")
    wait_idle(backend)
    r = backend.search("Nopa", source="imessage")
    assert r["ok"] and r["results"], r
    hit = r["results"][0]
    assert hit["source"] == "imessage" and hit["kind"] == "chat"
    # names come from contacts; the card number is redacted like in mail
    t = backend.read_thread(hit["thread_id"], "imessage")
    bodies = " ".join(m["body"] for m in t["messages"])
    assert "4111" not in bodies and "[REDACTED:CREDIT_CARD]" in bodies
    assert {m["sender"] for m in t["messages"]} >= {"Jamie Rivera", "Me"}
    # the MCP tools read the same store
    out = run_tool(backend._store, "list_items", {"source": "imessage", "kind": "chat", "limit": 50})
    assert out["count"] == 7
    assert not any("482913" in (x.get("snippet") or "") for x in out["results"])
    names = {x["source"]: x["name"] for x in run_tool(backend._store, "list_sources", {})["sources"]}
    assert names.get("imessage") == "iMessage"
    group = run_tool(backend._store, "search", {"query": "Mission Cliffs", "source": "imessage"})
    assert group["results"][0]["title"] == "Saturday climbing"


def test_incremental_sync_picks_up_new_messages(backend, chat_db):
    backend.connect_source("imessage")
    wait_idle(backend)
    add_message(chat_db, "+14155550134", "+14155550134", "Parking is on Valencia, see you there")
    assert backend.sync_source("imessage") == {"ok": True, "started": True}
    c = wait_idle(backend)
    assert c["item_count"] == 8 and c["last_result"]["status"] == "ok"
    assert backend.search("Valencia", source="imessage")["results"]


def test_missing_full_disk_access_explains_and_opens_settings(backend, access, opened):
    access.granted = False
    r = backend.connect_source("imessage")
    assert r["ok"] is False and r["needs_permission"] and r["requirement"] == "full_disk_access"
    assert "Full Disk Access" in r["error"]
    c = card(backend)
    assert c["state"] == "needs_permission" and not c["account"] and c["item_count"] == 0
    help_ = c["permission"]
    assert help_["title"] == "Full Disk Access" and help_["button"] == "Open System Settings"
    assert len(help_["steps"]) == 3 and "Quit and reopen" in help_["steps"][2]
    assert not opened  # nothing is opened or asked for until the user clicks
    assert backend.open_source_settings("imessage") == {"ok": True, "opened": True}
    assert opened == ["fda"]
    # "Check again" is another connect; now access works
    assert backend.connect_source("imessage")["ok"]
    c = wait_idle(backend)
    assert c["state"] == "connected" and c["permission"] is None and c["item_count"] == 7


def test_access_lost_after_connecting(backend, access):
    backend.connect_source("imessage")
    wait_idle(backend)
    access.granted = False
    backend.sync_source("imessage")
    c = wait_idle(backend)
    assert c["state"] == "needs_permission" and c["permission"]["title"] == "Full Disk Access"
    assert c["item_count"] == 7  # what was synced stays searchable
    assert backend.search("Nopa", source="imessage")["results"]


def test_no_messages_data_is_an_error_not_a_permission(tmp_path):
    missing = tmp_path / "nothing-here" / "chat.db"
    b = Backend(tmp_path / "data", demo=True, auto_sync=False,
                imessage_options={"db_path": missing, "names_fn": demo_names})
    try:
        r = b.connect_source("imessage")
        assert r["ok"] is False and "needs_permission" not in r and "No Messages data" in r["error"]
        assert card(b)["state"] == "not_connected"
    finally:
        b._shutdown()


def test_unreadable_folder_is_reported_as_missing_permission(tmp_path):
    """The real check (no fake access): like macOS without Full Disk Access, the folder can be
    stat'ed but not listed or read, which the reader reports as missing permission."""
    locked = tmp_path / "Messages"
    locked.mkdir()
    locked.chmod(0o100)
    b = Backend(tmp_path / "data", demo=True, auto_sync=False,
                imessage_options={"db_path": locked / "chat.db", "settings_fn": lambda: True,
                                  "names_fn": demo_names})
    try:
        r = b.connect_source("imessage")
        assert r["ok"] is False and r.get("needs_permission"), r
        assert card(b)["state"] == "needs_permission"
    finally:
        locked.chmod(0o700)
        b._shutdown()


def test_disconnect_deletes_imessage_data_only(backend):
    backend.connect_source("imessage")
    wait_idle(backend)
    assert backend.disconnect_source("imessage", True) == {"ok": True, "wiped": True}
    c = card(backend)
    assert c["state"] == "not_connected" and c["item_count"] == 0 and c["dropped_count"] == 0
    assert backend._store.counts("imessage") == (0, 0)
    assert backend.search("Nopa", source="imessage")["results"] == []
    # and it can be connected again from scratch
    assert backend.connect_source("imessage")["ok"]
    assert wait_idle(backend)["item_count"] == 7


def test_disconnect_clears_a_pending_permission(backend, access):
    access.granted = False
    backend.connect_source("imessage")
    assert card(backend)["state"] == "needs_permission"
    assert backend.disconnect_source("imessage", True)["ok"]
    c = card(backend)
    assert c["state"] == "not_connected" and c["permission"] is None


def test_open_settings_for_a_source_without_mac_permissions(backend):
    r = backend.open_source_settings("gmail")
    assert r["ok"] is False and "doesn't need" in r["error"]


def test_default_reader_path_is_never_used_by_tests():
    # conftest moves HOME into a temp folder, so even the default path isn't the real one
    import os
    assert str(default_db_path()).startswith(os.environ["HOME"])


def test_demo_mode_uses_a_synthetic_messages_db(tmp_path):
    kw = demo_backend_kwargs(tmp_path / "demo")
    db = kw["imessage_options"]["db_path"]
    assert str(db).startswith(str(tmp_path)) and db.exists()
    b = Backend(**kw, auto_sync=False)
    try:
        assert b.connect_source("imessage")["needs_permission"]
        assert b.open_source_settings("imessage")["opened"]
        assert b.connect_source("imessage")["ok"]
        assert wait_idle(b)["item_count"] == 7
    finally:
        b._shutdown()


def test_access_status_shape_is_what_the_source_expects():
    st = AccessStatus(AccessState.NO_PERMISSION, "x", "/nope")
    assert not st.ok and st.settings_url.startswith("x-apple.systempreferences:")
