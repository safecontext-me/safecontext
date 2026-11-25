"""Switching sources in the sidebar must feel instant, even on a big store and during a sync.

Covers the read path the browse page uses: indexes created in place on open, query plans that
use them, cached counts that follow every write, reads that don't wait for a sync's writes, and a
lenient timing guard on a ~50k-item store. The UI half (one list request per switch, the last
first page shown at once) runs mail.js in Node, as test_mail_ui_refresh does.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from safecontext.scrub import Dropped, ScrubbedMessage
from safecontext.store import SCHEMA_VERSION, Store

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "alex@example.org"
SWITCH_INDEXES = ("ix_items_version", "ix_dropped_version", "ix_dropped_sreason", "ix_items_redacted")


def kept(i, thread, date_ms, redactions=None, version="1", source_id=None):
    return ScrubbedMessage(source_id or f"m{i}", thread, date_ms, "Jamie <jamie@example.com>", ACCOUNT, "", "Hi",
                           "body", f"<{i}@example.com>", [], redactions or {}, version)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "mail.db", account=ACCOUNT)


def plan(store, sql, args):
    return " ".join(r[3] for r in store._db.execute("EXPLAIN QUERY PLAN " + sql, args))


# --- schema: indexes added in place --------------------------------------------------------------

def test_existing_store_gets_switch_indexes_on_open(tmp_path):
    p = tmp_path / "mail.db"
    s = Store(p)
    s.put_many([kept(1, "a", 1), Dropped("d1", "x", "draft", 2)])
    for ix in SWITCH_INDEXES:
        s._db.execute(f"DROP INDEX {ix}")
    s.close()
    s = Store(p)
    names = {r[0] for r in s._db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert set(SWITCH_INDEXES) <= names
    assert s.get_meta("schema_version") == str(SCHEMA_VERSION)  # indexes only: no version bump
    s.close()
    Store(p).close()  # opening again is a no-op
    assert Store(p).counts() == (1, 1)


def test_switch_queries_use_indexes(store):
    p = plan(store, "SELECT 1 FROM items x WHERE x.source=? AND x.thread_id=? AND x.date_ms >= ? "
                    "AND (x.date_ms > ? OR x.rowid > ?)", ["gmail", "a", 1, 1, 1])
    assert "ix_items_thread" in p and "date_ms>" in p  # seeks to newer items, doesn't walk the thread
    p = plan(store, "SELECT count(*) FROM items INDEXED BY ix_items_version WHERE source=? AND scrubber_version != ?",
             ["gmail", "1"])
    assert "COVERING INDEX ix_items_version" in p
    p = plan(store, "SELECT reason, count(*) FROM dropped INDEXED BY ix_dropped_sreason WHERE source IN (?) "
                    "GROUP BY reason", ["gmail"])
    assert "COVERING INDEX ix_dropped_sreason" in p and "TEMP B-TREE" not in p
    p = plan(store, f"SELECT {Store._THREAD_COLS} FROM items m WHERE m.source = ?", ["gmail"])
    assert "ix_items_redacted" in p


# --- thread heads: the probe change keeps the same rows -------------------------------------------

def test_thread_head_is_newest_item_even_with_equal_dates(store):
    # one long chat whose newest items share a timestamp, plus a short thread in between
    store.put_many([kept(i, "chat", 100 + i // 5) for i in range(50)] + [kept(99, "solo", 105)])
    rows = store.browse(["gmail"], view="synced")["rows"]
    assert [(r["thread_id"], r["id"], r["count"]) for r in rows] == [("chat", "m49", 50), ("solo", "m99", 1)]
    old = store.browse(["gmail"], view="synced", order="oldest")["rows"]
    assert [r["id"] for r in old] == ["m99", "m49"]


def test_thread_redaction_total_counts_only_redacted_rows(store):
    store.put_many([kept(1, "a", 1, {"PHONE": 2}), kept(2, "a", 2), kept(3, "a", 3, {"SSN": 1, "EMAIL": 4})])
    assert store.browse(["gmail"], view="synced")["rows"][0]["redacted"] == 7


# --- cached counts follow every write -------------------------------------------------------------

def test_counts_and_coverage_follow_writes(store):
    assert store.counts("gmail") == (0, 0)
    store.put_many([kept(1, "a", 10), Dropped("d1", "x", "draft", 5)])
    assert store.counts("gmail") == (1, 1)
    assert store.coverage("gmail")["kept"] == 1
    store.coverage("gmail")["dropped_by_reason"]["draft"] = 99  # callers get a copy
    assert store.coverage("gmail")["dropped_by_reason"] == {"draft": 1}
    store._db.execute("UPDATE items SET scrubber_version='0'")  # a write outside put_many
    assert store.coverage("gmail")["stale"] == 1
    store.delete(["m1"])
    assert store.counts("gmail") == (0, 1) and store.dropped_reasons(["gmail"]) == {"draft": 1}
    assert store.stats()["kept"] == 0


def test_counts_see_another_connections_writes(tmp_path):
    a, b = Store(tmp_path / "mail.db"), Store(tmp_path / "mail.db")  # e.g. the app and the CLI
    assert a.counts("gmail") == (0, 0)
    b.put_many([kept(1, "a", 10)])
    assert a.counts("gmail") == (1, 0)
    assert a.source_ids() == ["gmail"]


def test_state_read_after_write(store):
    store.set_state("gmail", "cursor", "5")
    assert store.get_state("gmail", "cursor") == "5"
    store.set_state("gmail", "cursor", None)
    assert store.get_state("gmail", "cursor") is None


# --- a sync's writes don't block browsing ---------------------------------------------------------

def test_browse_does_not_wait_for_a_sync_holding_the_writer(store):
    store.put_many([kept(i, f"t{i}", i) for i in range(20)])
    entered, release = threading.Event(), threading.Event()

    def long_sync_batch():
        with store._tx():  # holds the writer lock and an open write transaction
            entered.set()
            release.wait(10)

    t = threading.Thread(target=long_sync_batch)
    t.start()
    try:
        assert entered.wait(5)
        t0 = time.perf_counter()
        rows = store.browse(["gmail"])["rows"]
        assert len(rows) == 20 and store.counts("gmail") == (20, 0) and store.coverage("gmail")["kept"] == 20
        assert store.get_state("gmail", "nothing") is None
        assert time.perf_counter() - t0 < 2  # it would block for the full 10 s if it waited
    finally:
        release.set()
        t.join()


def test_wipe_still_works_with_the_reader_open(store):
    store.put_many([kept(1, "a", 1)])
    assert store.counts() == (1, 0)
    store.wipe_source("gmail")
    assert store.counts() == (0, 0) and store.browse(["gmail"])["rows"] == []


# --- rough timing guard ---------------------------------------------------------------------------

def _fixture_module():
    spec = importlib.util.spec_from_file_location("perf_fixture", ROOT / "scripts" / "perf_fixture.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_browse_is_fast_on_a_50k_store(tmp_path):
    """Lenient on purpose (CI machines vary), so it only catches gross regressions. On this
    fixture an M-series Mac takes under 10 ms per source now; the old thread-head probe took
    ~150 ms for iMessage oldest-first, and ~950 ms on a 40k-message iMessage store."""
    _fixture_module().build(tmp_path, gmail=36_000, imessage=12_000, gcal=3_000, body_words=(5, 20))
    s = Store(tmp_path / "mail.db")
    worst = {}
    for src in ("gmail", "imessage", "gcal"):
        for order in ("newest", "oldest"):
            t0 = time.perf_counter()
            page = s.browse([src], view="all", limit=150, order=order)
            s.browse([src], view="all", limit=150, order=order, cursor=page["next"])
            s.coverage(src)
            s.counts(src)
            worst[(src, order)] = time.perf_counter() - t0
            assert len(page["rows"]) == 150
    assert max(worst.values()) < 0.5, worst


# --- the UI: one request per switch, the last list at once ----------------------------------------

MAIL_JS = ROOT / "src" / "safecontext" / "app" / "ui" / "mail.js"
NODE = shutil.which("node") or (str(Path.home() / ".local/bin/node")
                                if os.access(Path.home() / ".local/bin/node", os.X_OK) else None)

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const [file] = process.argv.slice(2);
const calls = [];
let release = null;
const list = { scrollTop: 0, clientHeight: 600, addEventListener() {} };
const spacer = { style: {}, kids: [], replaceChildren(...k) { this.kids = k; } };
const ctx = {
  console, Promise, Date, Number, Object, JSON, Map, Math, Array, Set,
  setInterval: () => 1, clearInterval() {}, setTimeout, clearTimeout,
  document: { addEventListener() {}, querySelector: () => null }, window: { addEventListener() {} },
  state: { view: "mail", status: { sources: [] } },
  el: (tag, attrs) => ({ tag, cls: (attrs || {}).class || "" }),
  $: (id) => id === "mail-list" ? list : id === "mail-spacer" ? spacer
    : { value: "", hidden: false, addEventListener() {}, replaceChildren() {} },
  api: () => ({
    mail_list: (view, source) => { calls.push(source); return new Promise((r) => { release = () =>
      r({ ok: true, rows: [{ type: "thread", source, thread_id: source + "-fresh" }], next: null }); }); },
    mail_folders: async () => ({ ok: true, mailboxes: {}, sources: [] }),
    mail_coverage: async (s) => ({ ok: true, sources: [{ id: s, kept: 1, dropped: 0, newest_ms: 1, syncing: false }] }),
  }),
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
vm.runInContext(`mailRow = (r) => ({ cls: "mrow", r });
  mailRenderFolders = mailRenderCoverage = mailPlaceholder = mailLoadReasons = mailRenderFilter = () => {};
  mail.inited = true;
  // app.js's openSource and show("mail"), trimmed to what touches the list
  function openSource(id) { if (mail.source !== id) { Object.assign(mail, { source: id, rows: [], next: null, sel: -1,
    openKey: null, query: "", reasons: [], lastCovSig: null }); mailSetView("all"); } mailShow(); }`, ctx);
const tick = () => new Promise((r) => setTimeout(r, 20));
const shown = () => spacer.kids.map((k) => k.r ? k.r.thread_id : k.cls).join(",");
(async () => {
  const out = {};
  vm.runInContext("openSource('imessage')", ctx);
  out.firstVisitImmediately = shown();
  await tick(); release(); await tick();
  out.firstVisitLoaded = shown();
  vm.runInContext("openSource('gcal')", ctx); await tick(); release(); await tick();
  const before = calls.length;
  vm.runInContext("openSource('imessage')", ctx);
  out.backImmediately = shown();
  await tick(); release(); await tick();
  out.backLoaded = shown();
  out.requestsPerSwitch = calls.length - before;
  out.calls = calls;
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_switching_shows_something_at_once_and_asks_once(tmp_path):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS)
    out = subprocess.run([NODE, str(h), str(MAIL_JS)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert set(r["firstVisitImmediately"].split(",")) == {"mrow skel"}  # placeholders, not the old list
    assert r["firstVisitLoaded"] == "imessage-fresh"
    assert r["backImmediately"] == "imessage-fresh"  # the list from last time, before any reply
    assert r["backLoaded"] == "imessage-fresh"
    assert r["requestsPerSwitch"] == 1
    assert r["calls"] == ["imessage", "gcal", "imessage"]
