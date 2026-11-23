"""The Mail view: keyset-paged browsing of kept threads and dropped items,
FTS search by thread, the reader's thread, per-source coverage, and the
v2 -> v3 migration that adds its indexes."""
import json
import sqlite3
import time

import pytest

from safecontext.app.backend import Backend
from safecontext.app.demo import DEMO_ACCOUNT
from safecontext.app.main import build_html
from safecontext.app.mail import REASON_SHORT
from safecontext.demo.fake_gmail import FakeGmail
from safecontext.demo.synthetic import demo_mailbox, mailbox, msg
from safecontext.scrub import Dropped, ScrubbedMessage, scrub
from safecontext.store import SCHEMA_VERSION, Store
from safecontext.sync import Syncer

ACCOUNT = "alex@example.org"


def kept(i, thread, date_ms, subject="Hello", body="plain body", redactions=None):
    return ScrubbedMessage(f"m{i}", thread, date_ms, "Jamie <jamie@example.com>", ACCOUNT, "", subject, body,
                           f"<{i}@example.com>", [], redactions or {}, "1")


def dropped(i, thread, date_ms, reason="security:subject"):
    return Dropped(f"d{i}", thread, reason, date_ms)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "mail.db", account=ACCOUNT)


def walk(store, page=3, **kw):
    rows, cur = [], None
    while True:
        out = store.browse(["gmail"], cursor=cur, limit=page, **kw)
        rows += out["rows"]
        cur = out["next"]
        if not cur:
            return rows


# --- browse -------------------------------------------------------------------------------------

def test_threads_newest_first_one_row_per_thread(store):
    store.put_many([kept(1, "a", 100), kept(2, "a", 300, subject="Re: Hello"), kept(3, "b", 200),
                    kept(4, "c", 50, redactions={"PHONE": 2}), kept(5, "c", 60, redactions={"SSN": 1})])
    rows = store.browse(["gmail"], view="synced")["rows"]
    assert [(r["thread_id"], r["id"]) for r in rows] == [("a", "m2"), ("b", "m3"), ("c", "m5")]
    a, _, c = rows
    assert a["count"] == 2 and a["title"] == "Re: Hello" and a["type"] == "thread"
    assert c["redacted"] == 3  # summed over the whole thread


def test_all_interleaves_kept_and_dropped_by_date(store):
    store.put_many([kept(1, "a", 100), kept(2, "b", 300), dropped(1, "x", 200), dropped(2, "a", 50)])
    rows = store.browse(["gmail"], view="all")["rows"]
    assert [(r["type"], r["id"]) for r in rows] == [("thread", "m2"), ("dropped", "d1"), ("thread", "m1"),
                                                    ("dropped", "d2")]
    assert rows[2]["dropped_in_thread"] == 1


@pytest.mark.parametrize("view", ["all", "synced", "not_synced"])
@pytest.mark.parametrize("page", [1, 2, 3, 7, 100])
def test_paging_visits_every_row_once_even_with_equal_dates(store, view, page):
    items = [kept(i, f"t{i % 9}", 1000 + (i // 4)) for i in range(40)]  # many equal dates
    drops = [dropped(i, f"x{i}", 1000 + (i // 3)) for i in range(25)]
    store.put_many(items + drops)
    full = store.browse(["gmail"], view=view, limit=500)["rows"]
    rows = walk(store, page=page, view=view)
    assert [r["cursor"] for r in rows] == [r["cursor"] for r in full]
    expected = {"all": 9 + 25, "synced": 9, "not_synced": 25}[view]
    assert len({(r["type"], r["id"]) for r in rows}) == len(rows) == expected
    keys = [tuple(int(x) for x in r["cursor"].split(".")) for r in rows]
    assert keys == sorted(keys, reverse=True)


def test_reason_filter_and_source_scope(store):
    store.put_many([dropped(1, "x", 10, "security:body"), dropped(2, "y", 20, "bulk:list_unsubscribe")])
    store.put_many([dropped(3, "z", 30, "security:body")], source="gcal")
    rows = store.browse(["gmail"], view="not_synced", reasons=["security:body"])["rows"]
    assert [r["id"] for r in rows] == ["d1"]
    assert store.browse(["gmail"], view="not_synced", reasons=[])["rows"] == []
    assert len(store.browse(["gmail", "gcal"], view="not_synced")["rows"]) == 3


def test_dropped_rows_carry_no_content(store):
    """Only what the dropped table keeps, plus a link back and the subject of a kept message
    in the same thread (already scrubbed)."""
    store.put_many([kept(1, "a", 10, subject="Apartment"), dropped(1, "a", 20), dropped(2, "lonely", 30)])
    rows = store.browse(["gmail"], view="not_synced")["rows"]
    assert set(rows[0]) == {"type", "source", "id", "thread_id", "date", "reason", "thread_title", "link", "cursor"}
    assert rows[0]["thread_title"] == "" and rows[1]["thread_title"] == "Apartment"
    assert rows[0]["link"].startswith("https://mail.google.com/mail/") and rows[0]["link"].endswith("#all/lonely")
    cols = {r[1] for r in store._db.execute("PRAGMA table_info(dropped)")}
    assert cols == {"source", "id", "thread_id", "date_ms", "reason", "scrubber_version"}


def test_bad_view_or_cursor(store):
    with pytest.raises(ValueError):
        store.browse(["gmail"], view="inbox")
    with pytest.raises(ValueError):
        store.browse(["gmail"], cursor="nope")


# --- search, thread, coverage ------------------------------------------------------------------

def test_search_groups_by_thread_newest_first(store):
    store.put_many([kept(1, "a", 100, body="the hike is on"), kept(2, "a", 200, body="hike moved to 9"),
                    kept(3, "b", 300, body="no match here"), kept(4, "c", 150, subject="Hike photos")])
    out = store.search_threads("hike", ["gmail"])
    assert [(r["thread_id"], r["id"]) for r in out["rows"]] == [("a", "m2"), ("c", "m4")]
    assert "«hike»" in out["rows"][0]["snippet"] and out["rows"][0]["count"] == 2
    page = store.search_threads("hike", ["gmail"], limit=1)
    assert len(page["rows"]) == 1 and page["next"] == "1"
    assert store.search_threads("hike", ["gmail"], offset=1, limit=1)["rows"][0]["thread_id"] == "c"
    assert store.search_threads("  ", ["gmail"])["rows"] == []


def test_thread_detail_merges_dropped_in_order(store):
    store.put_many([kept(1, "a", 100, body="hi [REDACTED:PHONE]", redactions={"PHONE": 1}),
                    dropped(1, "a", 150, "security:body"), kept(2, "a", 200)])
    out = store.thread_detail("gmail", "a")
    assert [(e["type"], e["id"]) for e in out["entries"]] == [("item", "m1"), ("dropped", "d1"), ("item", "m2")]
    assert out["entries"][0]["redactions"] == {"PHONE": 1} and "[REDACTED:PHONE]" in out["entries"][0]["body"]


def test_coverage_counts_range_and_stale(store):
    store.put_many([kept(1, "a", 100), kept(2, "b", 900), dropped(1, "x", 50, "bulk:precedence"),
                    dropped(2, "y", 0, "error:ValueError")])
    store._db.execute("UPDATE items SET scrubber_version='0' WHERE id='m1'")
    c = store.coverage("gmail")
    assert (c["kept"], c["dropped"], c["oldest_ms"], c["newest_ms"], c["stale"]) == (2, 2, 50, 900, 1)
    assert c["dropped_by_reason"] == {"bulk:precedence": 1, "error:ValueError": 1}


# --- migration ------------------------------------------------------------------------------------

def test_v2_database_gets_browse_indexes(tmp_path):
    p = tmp_path / "mail.db"
    s = Store(p)
    s.put_many([kept(1, "a", 1), dropped(1, "x", 2)])
    for ix in ("ix_dropped_date", "ix_dropped_source", "ix_dropped_thread", "ix_dropped_reason"):
        s._db.execute(f"DROP INDEX {ix}")
    s.set_meta("schema_version", "2")
    s.close()
    s = Store(p)
    names = {r[0] for r in s._db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"ix_dropped_date", "ix_dropped_source", "ix_dropped_thread", "ix_dropped_reason"} <= names
    assert s.get_meta("schema_version") == str(SCHEMA_VERSION) == "3"
    assert s.counts() == (1, 1)


def test_newer_schema_version_is_not_downgraded(tmp_path):
    p = tmp_path / "mail.db"
    Store(p).set_meta("schema_version", "9")
    assert Store(p).get_meta("schema_version") == "9"


def test_queries_use_indexes(store):
    plan = " ".join(r[3] for r in store._db.execute(
        "EXPLAIN QUERY PLAN SELECT d.rowid FROM dropped d WHERE d.source IN (?) AND d.date_ms <= ? "
        "ORDER BY d.date_ms DESC, d.rowid DESC LIMIT 10", ["gmail", 5]))
    assert "ix_dropped_source" in plan and "TEMP B-TREE" not in plan
    plan = " ".join(r[3] for r in store._db.execute(
        "EXPLAIN QUERY PLAN SELECT count(*) FROM dropped WHERE source=? AND thread_id=?", ["gmail", "a"]))
    assert "ix_dropped_thread" in plan


# --- sync records Gmail's total for "still to fetch" --------------------------------------------

def test_backfill_records_mailbox_total(store):
    g = FakeGmail(page_size=3)
    for m in mailbox():
        g.add(m, record=False)
    Syncer(store, g).run()
    assert store.get_state("gmail", "mailbox_total") == str(len(mailbox()))


# --- the window's calls -----------------------------------------------------------------------------

@pytest.fixture
def backend(tmp_path):
    g = FakeGmail(page_size=50, email=DEMO_ACCOUNT)
    for m in mailbox() + demo_mailbox(threads=60, now_ms=1_790_000_000_000):
        g.add(m, record=False)
    b = Backend(tmp_path / "data", demo=True, gmail_factory=lambda a, m: g,
                connect_fn=lambda mode, email: DEMO_ACCOUNT, auto_sync=False)
    assert b.connect("full")["ok"]
    end = time.time() + 20
    while b._is_syncing("gmail") and time.time() < end:
        time.sleep(0.05)
    yield b
    b._shutdown()


def test_backend_folders_list_and_thread(backend):
    f = backend.mail_folders()
    kept_n, dropped_n = backend._store.counts("gmail")
    assert f["ok"] and f["mailboxes"] == {"all": kept_n + dropped_n, "synced": kept_n, "not_synced": dropped_n}
    assert [s["id"] for s in f["sources"]] == ["gmail"] and f["sources"][0]["account"] == DEMO_ACCOUNT

    page = backend.mail_list("all", limit=20)
    assert page["ok"] and len(page["rows"]) == 20 and page["next"]
    nxt = backend.mail_list("all", cursor=page["next"], limit=20)
    assert not {r["cursor"] for r in page["rows"]} & {r["cursor"] for r in nxt["rows"]}

    drops = backend.mail_list("not_synced", limit=500)["rows"]
    assert len(drops) == dropped_n and all(r["reason_info"]["label"] for r in drops)
    t = next(r for r in backend.mail_list("synced", limit=500)["rows"] if r["dropped_in_thread"])
    th = backend.mail_thread("gmail", t["thread_id"])
    assert th["ok"] and any(e["type"] == "dropped" for e in th["entries"])
    assert any(e["type"] == "item" for e in th["entries"])


def test_backend_reason_filter_uses_group_names(backend):
    reasons = backend.mail_reasons()["reasons"]
    groups = {r["group"]: r["count"] for r in reasons}
    assert "Security & 2FA codes" in groups and "Banks, tax & health senders" in groups
    rows = backend.mail_list("not_synced", reasons=["Security & 2FA codes"], limit=500)["rows"]
    assert len(rows) == groups["Security & 2FA codes"]
    assert {r["reason"] for r in rows} <= {"security:subject", "security:body"}


def test_backend_search_and_not_synced_search(backend):
    out = backend.mail_list("all", query="hike")
    assert out["ok"] and out["searched"] and out["rows"] and all(r["type"] == "thread" for r in out["rows"])
    assert backend.mail_list("not_synced", query="hike")["rows"] == []


def test_backend_coverage(backend):
    cov = backend.mail_coverage()["sources"]
    assert len(cov) == 1
    c = cov[0]
    kept_n, dropped_n = backend._store.counts("gmail")
    assert (c["kept"], c["dropped"], c["pending"]) == (kept_n, dropped_n, 0)
    assert sum(c["dropped_by_group"].values()) == dropped_n
    assert c["first_sync_done"] and c["last_sync_ms"] and c["oldest_ms"] < c["newest_ms"]
    assert any("Promotions" in n for n in c["notes"])


def test_coverage_during_first_sync(backend):
    st = backend._store.state("gmail")
    st.set("history_id", None)
    st.set("mailbox_total", "100000")
    c = backend.mail_coverage("gmail")["sources"][0]
    assert not c["first_sync_done"] and c["remaining_estimate"] == 100000 - c["kept"] - c["dropped"]
    assert any("older mail" in n or "paused" in n for n in c["notes"])


def test_backend_rejects_unknown_source(backend):
    assert backend.mail_list("all", source="nope")["ok"] is False
    assert backend.mail_thread("nope", "t")["ok"] is False


def test_every_reason_code_has_a_label():
    from safecontext.app.backend import REASON_LABELS
    assert set(REASON_LABELS) == set(REASON_SHORT)


def test_mail_ui_is_inlined():
    html = build_html()
    assert 'id="view-mail"' in html and "function mailInit" in html and ".mail-shell" in html
    assert "<script src=" not in html and '<link rel="stylesheet"' not in html
