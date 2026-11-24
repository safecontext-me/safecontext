import os
import sqlite3
import stat

import pytest

from safecontext.scrub import Dropped, RawMessage, scrub
from safecontext.scrub.models import ScrubbedMessage
from safecontext.store import Store, fts_query, gmail_link, to_ms
from safecontext.demo.synthetic import mailbox, msg


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data" / "mail.db", account="alex@example.org")
    s.put_many(scrub(m) for m in mailbox())
    yield s
    s.close()


def _kept(i, **kw):
    r = scrub(msg(i, **kw))
    assert isinstance(r, ScrubbedMessage)
    return r


# --- writes ---------------------------------------------------------------

def test_counts(store):
    st = store.stats()
    assert st["kept"] == 7 and st["dropped"] == 9
    assert st["redactions"]["API_KEY"] == 6


def test_dropped_rows_hold_no_content(store):
    cols = [r[1] for r in store._db.execute("PRAGMA table_info(dropped)")]
    assert cols == ["source", "id", "thread_id", "date_ms", "reason", "scrubber_version"]


def test_refuses_raw_message(store):
    with pytest.raises(TypeError):
        store.put(RawMessage("x", "t", [], {}))


def test_kept_then_dropped_removes_content(store):
    assert store.search("netflix")
    store.put(Dropped("m0005", "t0005", "spam_trash"))
    assert store.search("netflix") == [] and store.read_message("m0005") is None
    assert store.known_ids(["m0005"]) == {"m0005"}


def test_dropped_then_kept(store):
    store.put(_kept(9, subject="Sale is over", text="actually a note from a friend"))
    assert store.read_message("m0009") is not None
    assert store._db.execute("SELECT count(*) FROM dropped WHERE id='m0009'").fetchone()[0] == 0


def test_upsert_replaces_and_reindexes(store):
    store.put(_kept(6, subject="Notes v2", text="completely new words zephyr"))
    assert store.search("zephyr")[0].id == "m0006"
    assert store.search("standup") == []


def test_delete(store):
    store.delete(["m0001", "m0007"])
    assert store.read_message("m0001") is None and store.known_ids(["m0001", "m0007"]) == set()


def test_known_and_stale_ids_follow_scrubber_version(store):
    assert store.known_ids(["m0001", "m0007", "nope"]) == {"m0001", "m0007"}
    assert store.known_ids(["m0001"], version="999") == set()
    store._db.execute("UPDATE items SET scrubber_version='0' WHERE id='m0002'")
    assert store.stale_ids() == ["m0002"]


def test_retired_drop_reasons_are_requeued_once(tmp_path):
    path = tmp_path / "mail.db"
    s = Store(path)
    s.put_many([Dropped("m0001", "t1", "bulk:no_reply_sender"), Dropped("m0002", "t2", "spam_trash")])
    assert s.stale_ids() == []  # written under the current version
    s.close()
    s = Store(path)  # next start: only the retired-rule drop is queued for re-fetch
    assert s.stale_ids() == ["m0001"] and s.known_ids(["m0001", "m0002"]) == {"m0002"}
    s.put(_kept(1, subject="Your order has shipped", frm="Shop <no-reply@shop.example>"))
    s.close()
    s = Store(path)
    assert s.stale_ids() == [] and s.read_message("m0001") is not None
    s.close()


def test_meta(store):
    assert store.get_state("gmail", "history_id") is None
    store.set_state("gmail", "history_id", "12345")
    assert store.get_state("gmail", "history_id") == "12345"
    store.set_state("gmail", "history_id", None)
    assert store.get_state("gmail", "history_id") is None


def test_reopen_is_idempotent(tmp_path):
    p = tmp_path / "mail.db"
    Store(p).put(_kept(1, text="persist me"))
    s2 = Store(p)
    assert s2.search("persist")[0].id == "m0001"
    assert s2.get_meta("schema_version") == "3"


def test_wipe(store):
    store.set_state("gmail", "history_id", "1")
    store.wipe()
    assert store.stats()["kept"] == 0 and store.get_state("gmail", "history_id") is None


# --- on-disk privacy ------------------------------------------------------

def test_file_permissions(store, tmp_path):
    d = tmp_path / "data"
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    for f in d.iterdir():
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600, f.name


def test_deleted_content_is_zeroed_on_disk(tmp_path):
    marker = "QUOKKA-unique-marker-text"
    p = tmp_path / "mail.db"
    s = Store(p)
    s.put(_kept(1, text=f"before {marker} after"))
    s.put(_kept(2, text="other mail stays"))
    s.delete(["m0001"])
    s.close()  # checkpoints the WAL into the main file
    blob = b"".join(f.read_bytes() for f in tmp_path.iterdir() if f.is_file())
    assert marker.encode() not in blob
    assert b"other mail stays" in blob


def test_secure_delete_enabled(store):
    assert store._db.execute("PRAGMA secure_delete").fetchone()[0] == 1


# --- search ---------------------------------------------------------------

def test_search_with_citation(store):
    (hit,) = store.search("rent transfer")
    assert hit.id == "m0003" and "«rent»" in hit.snippet
    c = hit.citation
    assert c.date == "2025-11-03T18:03:00Z"
    assert c.sender == "Morgan Lee <morgan@example.net>"
    assert c.link == ("https://mail.google.com/mail/?authuser=alex%40example.org"
                      "#search/rfc822msgid%3Asynthetic-3%40example.com")


def test_search_stemming(store):
    assert store.search("ticket")[0].id == "m0001"  # body says "tickets"


def test_search_attachment_names(store):
    assert store.search("boarding")[0].id == "m0005"


def test_search_subject_ranks_above_body(store):
    store.put(_kept(50, subject="Budget", text="see notes"))
    store.put(_kept(51, subject="misc", text="the budget came up briefly"))
    assert [h.id for h in store.search("budget")][0] == "m0050"


@pytest.mark.parametrize("q", ['C++ AND (', '"unbalanced', "NEAR(a b)", "*", "-", "OR", "a:b", "' OR 1=1 --"])
def test_search_never_raises_on_odd_input(store, q):
    store.search(q)


def test_search_empty_query(store):
    assert store.search("   ") == []


def test_search_with_filters(store):
    ids = {h.id for h in store.search("code", sender="jamie@example.com")}
    assert ids == {"m0005"}
    assert store.search("rent", sender="@example.net")[0].id == "m0003"
    assert store.search("rent", sender="@example.com") == []
    assert store.search("rent", after="2025-11-04") == []
    assert store.search("rent", before="2025-11-04")[0].id == "m0003"


def test_fts_query_quotes_everything():
    assert fts_query('C++ AND ("x') == '"C++" "AND" "x"'
    assert fts_query("o'brien's notes") == '"o\'brien\'s" "notes"'


# --- thread / list --------------------------------------------------------

def test_read_thread_in_date_order(store):
    store.put(_kept(60, text="first"))
    later = scrub(msg(61, text="second"))
    later.thread_id = "t0060"
    store.put(later)
    views = store.read_thread("t0060")
    assert [v.body for v in views] == ["first", "second"]
    assert views[0].citation.link.startswith("https://mail.google.com/")


def test_read_message_has_attachment_metadata(store):
    v = store.read_message("m0005")
    assert [a.mime_type for a in v.attachments] == ["application/pdf", "image/png"]
    assert v.redactions == {"ONE_TIME_CODE": 1, "SSN": 1}


def test_list_by_sender(store):
    assert {h.id for h in store.list_messages(sender="jamie")} == {"m0001", "m0005"}
    assert {h.id for h in store.list_messages(sender="Jamie Rivera")} == {"m0001", "m0005"}
    assert [h.id for h in store.list_messages(sender="sam@example.dev")] == ["m0002"]


def test_list_by_date_newest_first(store):
    ids = [h.id for h in store.list_messages(after="2025-11-03T18:02:00Z", before="2025-11-03T18:05:00Z")]
    assert ids == ["m0004", "m0003", "m0002"]


def test_list_limit_clamped(store):
    assert len(store.list_messages(limit=0)) == 1


# --- helpers --------------------------------------------------------------

def test_gmail_link_fallback_to_thread():
    assert gmail_link("", "", "18c2f") == "https://mail.google.com/mail/#all/18c2f"


def test_to_ms():
    assert to_ms("1970-01-02") == 86_400_000
    assert to_ms("1970-01-01T00:00:01Z") == 1000
    assert to_ms(None) is None and to_ms(5) == 5
