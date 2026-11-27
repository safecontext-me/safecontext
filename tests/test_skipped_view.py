"""Showing skipped items: fetched live from the source, held in memory only, never stored, never
reachable by an AI app (app/skipped.py and each source's fetch_skipped)."""
import logging
from pathlib import Path

import pytest

from safecontext.app.backend import Backend
from safecontext.app.demo import DEMO_ACCOUNT, demo_backend_kwargs
from safecontext.demo.fake_calendar import event
from safecontext.demo.fake_gmail import FakeGmail
from safecontext.demo.synthetic import msg
from safecontext.tools import TOOLS, run_tool

MARK = "ZQXSKIPPEDMARKER"  # a word that only ever appears in the skipped items' content


@pytest.fixture
def app(tmp_path):
    kw = demo_backend_kwargs(tmp_path / "data")
    kw["auto_sync"] = False
    # The demo's pauses (so a person can watch progress) left out.
    kw["connect_fn"] = lambda mode, email: DEMO_ACCOUNT
    kw["calendar_connect_fn"] = lambda email: DEMO_ACCOUNT
    kw["source_options"]["dropbox"]["connect_fn"] = lambda email: DEMO_ACCOUNT
    kw["gmail_factory"]("", "full").__class__ = FakeGmail
    fakes = {"gmail": kw["gmail_factory"]("", "full"), "gcal": kw["calendar_factory"](""),
             "dropbox": kw["source_options"]["dropbox"]["api_factory"]("")}
    # One item per cloud source that the scrubber skips whole, each carrying MARK in its content.
    fakes["gmail"].add(msg(9001, frm="Chase <alerts@chase.com>", subject="Your statement is ready",
                           text=f"Balance due {MARK}gmail. Card ending 4242."), record=False)
    fakes["gcal"].put(event("evskip", "Lab results review", "2026-12-10T15:00:00Z", "2026-12-10T15:30:00Z",
                            description=f"Bring insurance card {MARK}gcal"))
    fakes["dropbox"].add("id:skip-notes", "notes.txt", "/Taxes/notes.txt", f"AGI 81,200 {MARK}dropbox".encode())
    b = Backend(**kw)
    for sid in ("gmail", "gcal", "dropbox"):
        assert b.connect_source(sid, {})["ok"]
    b.open_source_settings("imessage")  # demo Full Disk Access: turned on by the button
    assert b.connect_source("imessage", {})["ok"]
    for t in list(b._threads.values()):
        t.join()
    b.fakes, b.data_dir = fakes, tmp_path / "data"
    return b


SKIPPED = {"gmail": "m9001", "gcal": "alex@example.org/evskip", "dropbox": "id:skip-notes",
           "imessage": "DEMO-bank-7"}


def stored_bytes(data_dir: Path) -> bytes:
    """Everything SafeContext itself wrote: the database with its WAL, logs, state. The demo's own
    fake sources (demo-*) are left out: they stand in for Gmail, Messages and so on."""
    out = b""
    for p in data_dir.rglob("*"):
        if p.is_file() and not any(part.startswith("demo-") for part in p.relative_to(data_dir).parts):
            out += p.read_bytes()
    return out


@pytest.mark.parametrize("source", list(SKIPPED))
def test_each_source_shows_a_skipped_item_with_its_reason(app, source):
    assert SKIPPED[source] in app._store.dropped_entries(source, [SKIPPED[source]])  # it really was skipped
    out = app.skipped_item(source, SKIPPED[source])
    assert out["ok"] and not out["error"], out
    assert out["skipped"]["reason_info"]["label"] and out["skipped"]["reason_info"]["why"]
    item = out["item"]
    if source == "imessage":
        assert "verification code is 482913" in item["body"]  # read from chat.db on this Mac
    else:
        assert f"{MARK}{source}" in item["body"]


def test_gmail_item_has_headers_and_a_link_back(app):
    item = app.skipped_item("gmail", "m9001")["item"]
    assert item["sender"] == "Chase <alerts@chase.com>" and item["title"] == "Your statement is ready"
    assert item["link"].startswith("https://mail.google.com/mail/")


def test_calendar_item_is_an_event(app):
    item = app.skipped_item("gcal", SKIPPED["gcal"])["item"]
    assert item["kind"] == "event" and item["title"] == "Lab results review" and item["end"]


def test_dropbox_non_text_file_says_open_it_in_dropbox(app):
    assert app._store.dropped_entries("dropbox", ["id:demo-passport"])
    item = app.skipped_item("dropbox", "id:demo-passport")["item"]
    assert item["body"] == "" and "Open it in Dropbox" in item["note"]
    assert item["link"].startswith("https://www.dropbox.com/home/")
    assert "id:demo-passport" not in app.fakes["dropbox"].downloaded  # an image is never downloaded


def test_previews_give_sender_title_and_snippet(app):
    rows = [{"source": s, "id": i} for s, i in SKIPPED.items()]
    downloads = len(app.fakes["dropbox"].downloaded)
    p = app.skipped_previews(rows)["previews"]
    assert p["gmail:m9001"]["title"] == "Your statement is ready"
    assert p["gmail:m9001"]["sender"].startswith("Chase")
    assert p["gcal:alex@example.org/evskip"]["title"] == "Lab results review"
    assert p["dropbox:id:skip-notes"]["title"] == "notes.txt"
    assert "482913" in p["imessage:DEMO-bank-7"]["snippet"]
    assert app.fakes["gmail"].formats["metadata"] >= 1  # a list row asks Gmail for headers, not the whole message
    assert len(app.fakes["dropbox"].downloaded) == downloads  # nor Dropbox for the file


def test_nothing_fetched_reaches_the_disk_or_the_logs(app, caplog):
    caplog.set_level(logging.DEBUG)
    before = stored_bytes(app.data_dir)
    assert MARK.encode() not in before
    assert b"Dinner Saturday" in before  # the scan does read the database (kept mail is in it)
    app.skipped_previews([{"source": s, "id": i} for s, i in SKIPPED.items()])
    for s, i in SKIPPED.items():
        assert app.skipped_item(s, i)["ok"]
    after = stored_bytes(app.data_dir)
    assert MARK.encode() not in after
    assert b"482913" not in after
    assert MARK not in caplog.text and "482913" not in caplog.text
    # the dropped rows are unchanged: still only id, thread, date and reason
    rows = app._store._read("SELECT * FROM dropped WHERE id IN (?,?,?,?)", list(SKIPPED.values()))
    assert len(rows) == 4 and all(set(r.keys()) == {"source", "id", "thread_id", "date_ms", "reason",
                                                    "scrubber_version"} for r in rows)


def test_ai_apps_cannot_reach_a_fetched_skipped_item(app):
    for s, i in SKIPPED.items():
        app.skipped_item(s, i)
    assert not [t for t in TOOLS if "skip" in t["name"]]  # no MCP tool for it
    for query in (MARK, f"{MARK}gmail", "statement", "482913"):
        out = run_tool(app._store, "search", {"query": query})
        assert MARK not in str(out) and "482913" not in str(out)
    for s, i in SKIPPED.items():
        thread = app._store.dropped_entries(s, [i])[i]["thread_id"]
        out = run_tool(app._store, "read", {"thread_id": thread, "source": s})
        assert MARK not in str(out) and "482913" not in str(out)


def test_only_skipped_items_can_be_fetched(app):
    kept = app.mail_list("synced", "gmail")["rows"][0]
    out = app.skipped_item("gmail", kept["id"])
    assert not out["ok"] and "Not synced" in out["error"]
    assert app.skipped_previews([{"source": "gmail", "id": kept["id"]}])["previews"] == {}


def test_forget_drops_everything_held_in_memory(app):
    for s, i in SKIPPED.items():
        app.skipped_item(s, i)
    assert app._skipped.held() > 0
    assert app.skipped_forget()["ok"]
    assert app._skipped.held() == 0


def test_second_open_comes_from_memory_not_the_source(app):
    app.skipped_item("gmail", "m9001")
    n = app.fakes["gmail"].fetched["m9001"]
    app.skipped_item("gmail", "m9001")
    assert app.fakes["gmail"].fetched["m9001"] == n


def test_deleted_at_the_source(app):
    app.fakes["gmail"].delete("m9001", record=False)
    app.fakes["dropbox"].remove("id:skip-notes")
    app.fakes["gcal"].cancel("evskip")
    for s in ("gmail", "dropbox", "gcal"):
        out = app.skipped_item(s, SKIPPED[s])
        assert out["ok"] and out["item"] is None and out["gone"] and "no longer in" in out["error"]


def test_revoked_access_asks_to_reconnect(app):
    for s in ("gmail", "gcal", "dropbox"):
        app.fakes[s].revoked = True
        out = app.skipped_item(s, SKIPPED[s])
        assert out["item"] is None and out["needs_reconnect"] and "Reconnect" in out["error"]
    p = app.skipped_previews([{"source": "gmail", "id": "m9001"}])["previews"]
    assert "Reconnect" in p["gmail:m9001"]["error"]


def test_offline_says_so(app):
    g = app.fakes["gmail"]
    g.fail_get_call = g.calls["get_batch"] + 1
    out = app.skipped_item("gmail", "m9001")
    assert out["item"] is None and out["offline"] and "internet" in out["error"]
    assert app.skipped_item("gmail", "m9001")["item"]  # and works again once back online


def test_disconnecting_a_source_forgets_its_items(app):
    app.skipped_item("dropbox", SKIPPED["dropbox"])
    app.skipped_item("gmail", "m9001")
    app.disconnect_source("dropbox", True)
    assert app._skipped._full.keys() == {("gmail", "m9001")}
