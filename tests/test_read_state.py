"""Read / unread: mirrored from the source (Gmail's UNREAD label, Messages' is_read), never written
back. Covers the store migration, Gmail label changes through history (updated in place, no
re-fetch, dropped mail never resurrected), the one-time unread listing for older stores, iMessage
read state that changes without new rows, the Unread list and filters, MCP tools, and the row in JS.

Only fake Gmail, synthetic chat.db files in temp folders and temp stores."""
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from safecontext.demo.fake_gmail import FakeGmail
from safecontext.demo.fake_messages import FakeFullDiskAccess, demo_names, make_messages_db
from safecontext.demo.synthetic import mailbox, msg
from safecontext.gmail_parse import HistoryChanges
from safecontext.scrub import ScrubbedItem, ScrubbedMessage
from safecontext.sources.imessage import IMessageSource
from safecontext.store import _SCHEMA, Store
from safecontext.sync import READ_STATE_KEY, Syncer
from safecontext.tools import run_tool

ACCOUNT = "alex@example.org"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data" / "mail.db", account=ACCOUNT)
    yield s
    s.close()


def unread_ids(store, source="gmail"):
    return {r[0] for r in store._db.execute("SELECT id FROM items WHERE source=? AND unread=1", (source,))}


# --- store migration --------------------------------------------------------------

def test_older_store_gets_the_column_and_indexes_in_place(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    old = _SCHEMA.replace("    unread            INTEGER NOT NULL DEFAULT 0,\n", "")
    assert "unread" not in old
    db.executescript(old)
    db.execute("INSERT INTO meta VALUES ('schema_version', '3')")
    db.execute("INSERT INTO items (source, id, kind, thread_id, date_ms, title, scrubber_version) "
               "VALUES ('gmail', 'm1', 'message', 't1', 1000, 'Hi', '1')")
    db.commit()
    db.close()
    for _ in range(2):  # opening again is a no-op
        s = Store(path)
        cols = {r[1] for r in s._db.execute("PRAGMA table_info(items)")}
        idx = {r[1] for r in s._db.execute("PRAGMA index_list(items)")}
        assert "unread" in cols and {"ix_items_unread_date", "ix_items_unread_thread"} <= idx
        assert s.read_item("m1").unread is False and s.read_item("m1").title == "Hi"
        assert s.unread_count("gmail") == 0
        s.close()


def test_set_unread_never_creates_rows(store):
    from safecontext.scrub import Dropped
    store.put_many([ScrubbedMessage("k", "t", 1, "a@x.com", "", "", "s", "b", "", [], {}, "1"),
                    Dropped("d", "t", "security:subject", 2)])
    assert store.set_unread("gmail", {"k": True, "d": True, "nope": True}) == {"nope"}
    assert unread_ids(store) == {"k"}
    assert store._db.execute("SELECT count(*) FROM items").fetchone()[0] == 1
    assert store.replace_unread("gmail", ["d", "zzz"]) == 1  # k becomes read; d and zzz have no item row
    assert unread_ids(store) == set()


# --- Gmail -------------------------------------------------------------------

def test_history_changes_keeps_the_last_word_and_defers_to_fetches():
    ch = HistoryChanges()
    ch.add_page({"history": [
        {"labelsAdded": [{"message": {"id": "a"}, "labelIds": ["UNREAD"]}]},
        {"labelsRemoved": [{"message": {"id": "a"}, "labelIds": ["UNREAD"]}]},
        {"labelsRemoved": [{"message": {"id": "b"}, "labelIds": ["UNREAD"]}]},
        {"labelsAdded": [{"message": {"id": "c"}, "labelIds": ["UNREAD", "STARRED"]}]},
        {"labelsAdded": [{"message": {"id": "d"}, "labelIds": ["UNREAD"]}]},
        {"messagesAdded": [{"message": {"id": "d"}}]},
        {"labelsAdded": [{"message": {"id": "e"}, "labelIds": ["UNREAD"]}]},
        {"messagesDeleted": [{"message": {"id": "e"}}]},
    ]})
    assert ch.unread_only() == {"a": False, "b": False}
    assert ch.changed == {"c", "d"} and ch.deleted == {"e"}


@pytest.fixture
def gmail():
    g = FakeGmail(page_size=4)
    for m in mailbox():
        if m.id in ("m0002", "m0003"):
            m.label_ids.append("UNREAD")
        g.add(m, record=False)
    return g


def run(store, gmail, **kw):
    return Syncer(store, gmail, batch_size=3, **kw).run()


def test_backfill_stores_read_state(store, gmail):
    assert run(store, gmail).status == "ok"
    assert unread_ids(store) == {"m0002", "m0003"}
    assert store.unread_count("gmail") == 2
    assert store.get_state("gmail", READ_STATE_KEY) == "1"


def test_read_on_gmail_updates_in_place_without_fetching(store, gmail):
    run(store, gmail)
    fetched = sum(gmail.fetched.values())
    body = store.read_item("m0002").body
    gmail.relabel("m0002", remove=["UNREAD"])  # read on the phone
    gmail.relabel("m0001", add=["UNREAD"])     # marked unread in Gmail
    r = run(store, gmail)
    assert r.status == "ok" and r.fetched == 0 and sum(gmail.fetched.values()) == fetched
    assert unread_ids(store) == {"m0001", "m0003"}
    assert store.read_item("m0002").body == body
    assert store.search(body.split()[0], source="gmail")  # FTS untouched by the in-place update


def test_other_label_changes_still_fetch_and_bring_read_state(store, gmail):
    run(store, gmail)
    gmail.relabel("m0003", add=["STARRED"], remove=["UNREAD"])
    before = gmail.fetched["m0003"]
    run(store, gmail)
    assert gmail.fetched["m0003"] == before + 1 and "m0003" not in unread_ids(store)


def test_dropped_mail_is_not_resurrected_by_a_read_change(store, gmail):
    gmail.add(msg(101, frm="Acct <accounts@example-app.com>", subject="Hello",
                  text="593017 is your Example code"), record=False)
    run(store, gmail)
    assert store._db.execute("SELECT 1 FROM dropped WHERE id='m0101'").fetchone()
    before = gmail.fetched["m0101"]
    gmail.relabel("m0101", add=["UNREAD"])
    run(store, gmail)
    assert store.read_item("m0101") is None and gmail.fetched["m0101"] == before


def test_read_change_on_unknown_mail_fetches_it(store, gmail):
    run(store, gmail)
    gmail.add(msg(102, subject="Arrived quietly", text="hello there"), record=False)  # no history record
    gmail.relabel("m0102", add=["UNREAD"])
    run(store, gmail)
    assert store.read_item("m0102").unread is True


def test_older_store_gets_read_state_once_from_the_unread_list(store, gmail):
    run(store, gmail)
    store.replace_unread("gmail", [])            # as synced by a build without read state
    store.set_state("gmail", READ_STATE_KEY, None)
    fetched = sum(gmail.fetched.values())
    run(store, gmail)
    assert unread_ids(store) == {"m0002", "m0003"} and sum(gmail.fetched.values()) == fetched
    assert gmail.calls["list_unread"] == 2  # this run's pages only (the first run listed once too)
    run(store, gmail)
    assert gmail.calls["list_unread"] == 2  # done once per store


def test_sync_never_asks_gmail_to_change_anything(store, gmail):
    run(store, gmail)
    gmail.relabel("m0002", remove=["UNREAD"])
    run(store, gmail)
    store.read_thread(store.read_item("m0003").thread_id)
    assert set(gmail.calls) <= {"profile", "list", "get_batch", "history", "list_unread"}
    assert "UNREAD" in gmail.messages["m0003"]["labelIds"]  # reading it here didn't mark it read


# --- iMessage ----------------------------------------------------------------------

def test_imessage_read_state_follows_messages(tmp_path, store):
    db = make_messages_db(tmp_path / "Messages" / "chat.db", unread=(5, 8))
    access = FakeFullDiskAccess(granted=True)
    src = IMessageSource(store, db_path=db, access_fn=access.check, settings_fn=access.open_settings,
                         names_fn=demo_names)
    src.connect()
    assert src.sync().status == "ok"
    assert unread_ids(store, "imessage") == {"DEMO-climb-5", "DEMO-jamie-8"}
    assert src.status().unread_count == 2
    # Read on the iPhone: is_read flips with no new rows, and the next sync picks it up.
    con = sqlite3.connect(db)
    con.execute("UPDATE message SET is_read=1 WHERE guid='DEMO-jamie-8'")
    con.execute("UPDATE message SET is_read=0 WHERE guid='DEMO-jamie-1'")
    con.commit()
    con.close()
    r = src.sync()
    assert r.status == "ok" and r.fetched == 0
    assert unread_ids(store, "imessage") == {"DEMO-climb-5", "DEMO-jamie-1"}


def test_my_own_messages_are_never_unread(tmp_path, store):
    db = make_messages_db(tmp_path / "chat.db")
    con = sqlite3.connect(db)
    con.execute("UPDATE message SET is_read=0")  # Messages leaves is_read 0 on some sent rows
    con.commit()
    con.close()
    access = FakeFullDiskAccess(granted=True)
    src = IMessageSource(store, db_path=db, access_fn=access.check, names_fn=demo_names)
    src.connect()
    src.sync()
    mine = {r[0] for r in store._db.execute("SELECT id FROM items WHERE sender='Me'")}
    assert mine and not (mine & unread_ids(store, "imessage"))


# --- the Unread list ---------------------------------------------------------------

def _mail(i, thread, date_ms, unread=False, subject="Hello"):
    return ScrubbedMessage(f"m{i}", thread, date_ms, "Jamie <jamie@example.com>", ACCOUNT, "", subject,
                           f"body {i} lunch", f"<{i}@x>", [], {}, "1", unread=unread)


@pytest.fixture
def mixed(store):
    rows = []
    for t in range(40):
        for k in range(3):
            i = t * 3 + k
            rows.append(_mail(i, f"t{t}", 1_000_000 + i * 1000, unread=(t % 4 == 0 and k == 1)))
    store.put_many(rows)
    store.put_many([ScrubbedItem("e1", "event", "e1", 5, None, "x", [], "Standup", "lunch", "", [], {}, {}, "1")],
                   source="gcal")
    return store


def _walk(store, view="unread", page=4, order="newest"):
    out, cur = [], None
    while True:
        p = store.browse(["gmail"], view=view, cursor=cur, limit=page, order=order)
        out += p["rows"]
        if not p["next"]:
            return out
        cur = p["next"]


@pytest.mark.parametrize("order", ["newest", "oldest"])
def test_unread_view_lists_threads_with_any_unread_message(mixed, order):
    driven = _walk(mixed, order=order)
    mixed.UNREAD_DRIVE_MAX = 0  # the other plan: walk the date index
    scanned = _walk(mixed, order=order)
    assert [r["thread_id"] for r in driven] == [r["thread_id"] for r in scanned]
    assert {r["thread_id"] for r in driven} == {f"t{t}" for t in range(0, 40, 4)}
    assert all(r["unread"] == 1 and r["count"] == 3 for r in driven)
    # the thread's newest message stands for it, even though the unread one is older
    assert driven[0 if order == "newest" else -1]["id"] == "m110"


def test_all_view_marks_unread_threads(mixed):
    rows = mixed.browse(["gmail"], view="all", limit=200)["rows"]
    assert {r["thread_id"] for r in rows if r["unread"]} == {f"t{t}" for t in range(0, 40, 4)}
    detail = mixed.thread_detail("gmail", "t0")
    assert [e["unread"] for e in detail["entries"]] == [False, True, False]


def test_search_threads_unread_only(mixed):
    assert len(mixed.search_threads("lunch", ["gmail"], limit=100)["rows"]) == 40
    rows = mixed.search_threads("lunch", ["gmail"], limit=100, unread_only=True)["rows"]
    assert {r["thread_id"] for r in rows} == {f"t{t}" for t in range(0, 40, 4)}


def test_unknown_view_still_rejected(mixed):
    with pytest.raises(ValueError):
        mixed.browse(["gmail"], view="starred")


# --- MCP tools -----------------------------------------------------------------------

def test_tools_filter_and_report_read_state(mixed):
    r = run_tool(mixed, "list_items", {"source": "gmail", "unread": True, "limit": 100})
    assert r["count"] == 10 and all(x["unread"] is True for x in r["results"])
    r = run_tool(mixed, "list_items", {"unread": False, "order": "oldest", "limit": 3})
    assert all(x.get("unread") is not True for x in r["results"])
    event = r["results"][0]
    assert event["source"] == "gcal"
    assert "unread" not in event  # no read state for calendar events
    r = run_tool(mixed, "search", {"query": "lunch", "unread": True, "limit": 50})
    assert r["count"] == 10 and {x["source"] for x in r["results"]} == {"gmail"}
    r = run_tool(mixed, "read", {"thread_id": "t0", "source": "gmail"})
    assert [m["unread"] for m in r["messages"]] == [False, True, False]
    assert run_tool(mixed, "list_messages", {"unread": True})["count"] == 10
    assert "error" in run_tool(mixed, "list_items", {"unread": "yes"})


def test_tool_schemas_offer_unread():
    from safecontext.tools import TOOLS
    by = {t["name"]: t for t in TOOLS}
    for name in ("search", "list_items", "search_mail", "list_messages"):
        assert by[name]["parameters"]["properties"]["unread"]["type"] == "boolean"


# --- the row in the list (JS) ------------------------------------------------------------

UI = Path(__file__).resolve().parents[1] / "src" / "safecontext" / "app" / "ui"
NODE = shutil.which("node") or (str(Path.home() / ".local/bin/node")
                                if os.access(Path.home() / ".local/bin/node", os.X_OK) else None)

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const el = (tag, attrs = {}, ...kids) => ({ tag, cls: attrs.class || "", title: attrs.title,
  kids: kids.flat().filter((k) => k != null && k !== false).map((k) => (typeof k === "object" ? k : String(k))) });
const ctx = {
  console, Date, Number, Object, JSON, el, document: { addEventListener() {} }, window: { addEventListener() {} },
  state: {}, $: () => ({ value: "", addEventListener() {} }),
  richText: (s) => s, senderName: (s) => s, sourceName: (s) => s, n: String,
  fmtDate: () => "DATE",
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
vm.runInContext("mail.folders = null; mail.sel = -1;", ctx);
const row = (r) => vm.runInContext("mailRow", ctx)(r, 0);
const base = { type: "thread", kind: "message", source: "gmail", sender: "a", title: "t", date: "2026-03-04T15:42:00Z" };
const dot = (node) => node.kids.find((k) => typeof k === "object" && k.cls === "mrow-dot") || null;
const unread = row({ ...base, unread: 2 }), read = row({ ...base, unread: 0 });
console.log(JSON.stringify({
  unreadCls: unread.cls, readCls: read.cls,
  dot: dot(unread) && { title: dot(unread).title, label: dot(unread).kids[0].kids },
  readDot: dot(read),
  dropped: row({ type: "dropped", date: base.date, reason_info: { label: "l", why: "w" } }).cls,
}));
"""


@pytest.mark.skipif(not NODE, reason="needs Node")
def test_unread_row_is_bold_with_a_dot(tmp_path):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS)
    out = subprocess.run([NODE, str(h), str(UI / "mail.js")], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["unreadCls"].split() == ["mrow", "unread"] and r["readCls"].split() == ["mrow"]
    assert r["dot"] == {"title": "2 unread", "label": ["Unread"]} and r["readDot"] is None
    assert "unread" not in r["dropped"]


def test_unread_row_styles_use_tokens_only():
    css = (UI / "mail.css").read_text()
    rules = [line for line in css.splitlines() if "unread" in line or "mrow-dot" in line]
    assert rules and not any("#" in line or "rgb" in line for line in rules)
