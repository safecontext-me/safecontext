"""Gmail labels (mirrored from Gmail) and the user's own tags (on this Mac only): storage, sync,
the Mail view's filter, and the MCP tools."""
import sqlite3

import pytest

from safecontext.demo.fake_gmail import FakeGmail
from safecontext.demo.synthetic import msg
from safecontext.gmail_parse import HistoryChanges
from safecontext.scrub import Dropped, ScrubbedMessage
from safecontext.store import Store
from safecontext.sync import Syncer
from safecontext.tools import run_tool

ACCOUNT = "alex@example.org"


def kept(i, thread, date_ms, labels=(), subject="Hello", body="plain body"):
    return ScrubbedMessage(f"m{i}", thread, date_ms, "Jamie <jamie@example.com>", ACCOUNT, "", subject, body,
                           f"<{i}@example.com>", [], {}, "1", labels=list(labels))


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "mail.db", account=ACCOUNT)
    yield s
    s.close()


def names(rows):
    return [l["name"] for l in rows]


# --- store ------------------------------------------------------------------

def test_labels_are_stored_with_messages_and_named_by_the_label_list(store):
    store.replace_labels("gmail", [{"id": "Label_1", "name": "Receipts", "color": "#fb4c2f"},
                                   {"id": "Label_2", "name": "Work/Clients"},
                                   {"id": "INBOX", "name": "INBOX", "type": "system"},
                                   {"id": "UNREAD", "name": "UNREAD", "type": "system"},
                                   {"id": "CATEGORY_UPDATES", "name": "CATEGORY_UPDATES", "type": "system"}])
    store.put_many([kept(1, "t1", 1000, ["INBOX", "Label_1", "UNREAD", "CATEGORY_UPDATES"]),
                    kept(2, "t1", 2000, ["Label_2"]), kept(3, "t3", 3000)])
    # Gmail's own first, then user labels A to Z; UNREAD and categories aren't kept
    assert [(l["name"], l["type"], l["count"]) for l in store.labels()] == [
        ("Inbox", "system", 1), ("Receipts", "user", 1), ("Work/Clients", "user", 1)]
    assert store.labels()[1]["color"] == "#fb4c2f"
    assert names(store.thread_labels("gmail", "t1")) == ["Inbox", "Receipts", "Work/Clients"]
    assert store.thread_labels("gmail", "t3") == []


def test_label_names_are_scrubbed_before_storing(store):
    store.replace_labels("gmail", [{"id": "Label_1", "name": "Card 4111 1111 1111 1111"}])
    assert names(store.labels()) == ["Card [REDACTED:CREDIT_CARD]"]
    raw = store._db.execute("SELECT group_concat(name) FROM labels").fetchone()[0]
    assert "4111" not in raw


def test_rename_and_delete_follow_gmail(store):
    store.replace_labels("gmail", [{"id": "Label_1", "name": "Receipts"}, {"id": "Label_2", "name": "Old"}])
    store.put_many([kept(1, "t1", 1000, ["Label_1", "Label_2"])])
    store.replace_labels("gmail", [{"id": "Label_1", "name": "Purchases"}])  # renamed one, deleted the other
    assert names(store.thread_labels("gmail", "t1")) == ["Purchases"]
    assert store._db.execute("SELECT count(*) FROM item_labels").fetchone()[0] == 1


def test_unknown_label_waits_for_its_name(store):
    store.put_many([kept(1, "t1", 1000, ["Label_9"])])
    assert store.thread_labels("gmail", "t1") == []  # nameless until labels.list names it
    store.replace_labels("gmail", [{"id": "Label_9", "name": "New one"}])
    assert names(store.thread_labels("gmail", "t1")) == ["New one"]


def test_refetch_replaces_labels_and_dropping_removes_them(store):
    store.replace_labels("gmail", [{"id": "Label_1", "name": "A"}, {"id": "Label_2", "name": "B"}])
    store.put_many([kept(1, "t1", 1000, ["Label_1"])])
    store.put_many([kept(1, "t1", 1000, ["Label_2"])])
    assert names(store.thread_labels("gmail", "t1")) == ["B"]
    store.put_many([Dropped("m1", "t1", "spam_trash", 1000)])
    assert store._db.execute("SELECT count(*) FROM item_labels").fetchone()[0] == 0


def test_set_item_labels_in_place(store):
    store.replace_labels("gmail", [{"id": "Label_1", "name": "A"}])
    store.put_many([kept(1, "t1", 1000), Dropped("m2", "t2", "spam_trash", 2000)])
    rowid = store._db.execute("SELECT rowid FROM items WHERE id='m1'").fetchone()[0]
    unknown = store.set_item_labels("gmail", {"m1": ["Label_1", "INBOX"], "m2": ["Label_1"], "m9": ["Label_1"]})
    assert unknown == {"m9"}  # neither kept nor dropped: the caller fetches it
    assert names(store.thread_labels("gmail", "t1")) == ["Inbox", "A"]
    assert store.thread_labels("gmail", "t2") == []  # dropped mail gets no labels
    assert store._db.execute("SELECT rowid FROM items WHERE id='m1'").fetchone()[0] == rowid  # not rewritten


def test_tags(store):
    store.put_many([kept(1, "t1", 1000), kept(2, "t2", 2000)])
    a = store.create_tag("Taxes 2025")
    b = store.create_tag("Trip")
    with pytest.raises(ValueError):
        store.create_tag("taxes 2025")  # names are unique, ignoring case
    with pytest.raises(ValueError):
        store.create_tag("  ")
    with pytest.raises(ValueError):
        store.tag_thread(a["id"], "gmail", "nope")  # only synced threads
    store.tag_thread(a["id"], "gmail", "t1")
    store.tag_thread(a["id"], "gmail", "t1")  # idempotent
    store.tag_thread(b["id"], "gmail", "t1")
    assert names(store.thread_labels("gmail", "t1")) == ["Taxes 2025", "Trip"]
    store.update_tag(a["id"], name="Taxes", color="green")
    assert [(l["name"], l["color"], l["count"]) for l in store.labels()] == [("Taxes", "green", 1), ("Trip", b["color"], 1)]
    with pytest.raises(ValueError):
        store.update_tag(a["id"], name="trip")
    store.tag_thread(b["id"], "gmail", "t1", on=False)
    store.delete_tag(a["id"])
    assert store.thread_labels("gmail", "t1") == []
    # a re-fetch of the thread's messages keeps its tags
    store.tag_thread(b["id"], "gmail", "t2")
    store.put_many([kept(2, "t2", 2000, subject="again")])
    assert names(store.thread_labels("gmail", "t2")) == ["Trip"]


def test_tags_survive_disconnecting_a_source_but_its_threads_dont(store):
    store.replace_labels("gmail", [{"id": "Label_1", "name": "A"}])
    store.put_many([kept(1, "t1", 1000, ["Label_1"])])
    t = store.create_tag("Keep")
    store.tag_thread(t["id"], "gmail", "t1")
    store.wipe_source("gmail")
    assert names(store.labels()) == ["Keep"]  # the tag itself stays; Gmail's labels go
    assert store._db.execute("SELECT count(*) FROM thread_tags").fetchone()[0] == 0
    store.wipe()
    assert store.labels() == []


def test_older_store_gains_label_tables(tmp_path):
    path = tmp_path / "mail.db"
    Store(path).close()
    db = sqlite3.connect(path)
    for t in ("item_labels", "thread_tags", "labels"):
        db.execute(f"DROP TABLE {t}")
    db.commit()
    db.close()
    s = Store(path)  # opening adds them again, and twice is fine
    s.close()
    s = Store(path)
    assert s.labels() == []
    s.close()


# --- the Mail view's filter ---------------------------------------------------

def _labelled_store(store):
    store.replace_labels("gmail", [{"id": "Label_1", "name": "Receipts"}, {"id": "Label_2", "name": "Work"},
                                   {"id": "Label_3", "name": "Work/Clients"}])
    store.put_many([kept(1, "t1", 1000, ["Label_1"]), kept(2, "t1", 5000),  # newest item has no label
                    kept(3, "t3", 3000, ["Label_3"]), kept(4, "t4", 4000),
                    Dropped("d1", "t9", "spam_trash", 4500)])
    tag = store.create_tag("Later")
    store.tag_thread(tag["id"], "gmail", "t4")
    return {l["name"]: l["id"] for l in store.labels()}


@pytest.mark.parametrize("drive", [True, False])
def test_browse_by_label(store, drive):
    ids = _labelled_store(store)
    store.LABEL_DRIVE_MAX = 10 ** 9 if drive else 0  # both query plans
    rows = store.browse(["gmail"], labels=[ids["Receipts"]])["rows"]
    assert [r["thread_id"] for r in rows] == ["t1"] and rows[0]["id"] == "m2"  # the thread, by its newest item
    assert names(rows[0]["labels"]) == ["Receipts"]
    rows = store.browse(["gmail"], labels=[ids["Receipts"], ids["Later"]])["rows"]
    assert [r["thread_id"] for r in rows] == ["t1", "t4"]  # any of them; dropped items never match
    assert store.browse(["gmail"], labels=[ids["Later"]], view="not_synced")["rows"] == []
    assert store.browse(["gmail"], labels=[])["rows"] == []
    # paging
    page = store.browse(["gmail"], labels=[ids["Receipts"], ids["Later"]], limit=1)
    nxt = store.browse(["gmail"], labels=[ids["Receipts"], ids["Later"]], limit=1, cursor=page["next"])
    assert [r["thread_id"] for r in page["rows"] + nxt["rows"]] == ["t1", "t4"]


def test_unfiltered_rows_carry_labels(store):
    _labelled_store(store)
    rows = {r.get("thread_id"): r for r in store.browse(["gmail"])["rows"]}
    assert names(rows["t3"]["labels"]) == ["Work/Clients"]
    assert names(rows["t4"]["labels"]) == ["Later"]


def test_search_threads_by_label(store):
    ids = _labelled_store(store)
    assert [r["thread_id"] for r in store.search_threads("plain", ["gmail"], labels=[ids["Work/Clients"]])["rows"]] == ["t3"]
    assert store.find_labels("work") == [ids["Work"], ids["Work/Clients"]]  # a parent finds its children
    assert store.find_labels("RECEIPTS") == [ids["Receipts"]]
    assert store.find_labels("nothing") == []


# --- sync ---------------------------------------------------------------------

@pytest.fixture
def gmail():
    g = FakeGmail(page_size=4)
    g.label_defs = {"Label_1": {"name": "Receipts", "color": "#16a766"}, "Label_2": {"name": "Family"}}
    g.add(msg(1, subject="Your order", text="thanks", labels=("INBOX", "Label_1")), record=False)
    g.add(msg(2, subject="Dinner", text="see you", labels=("INBOX",)), record=False)
    return g


def run(store, gmail):
    return Syncer(store, gmail, batch_size=3).run()


def test_sync_stores_labels(store, gmail):
    assert run(store, gmail).status == "ok"
    assert gmail.calls["labels"] == 1
    assert names(store.thread_labels("gmail", "t0001")) == ["Inbox", "Receipts"]


def test_label_changes_apply_in_place_without_refetching(store, gmail):
    run(store, gmail)
    before = dict(gmail.fetched)
    gmail.relabel("m0002", add=["Label_2", "STARRED"])
    gmail.relabel("m0001", remove=["Label_1"])
    gmail.relabel("m0001", add=["UNREAD"])
    assert run(store, gmail).status == "ok"
    assert dict(gmail.fetched) == before  # nothing re-downloaded or re-scrubbed
    assert names(store.thread_labels("gmail", "t0002")) == ["Inbox", "Starred", "Family"]
    assert names(store.thread_labels("gmail", "t0001")) == ["Inbox"]


def test_scrub_labels_still_refetch(store, gmail):
    run(store, gmail)
    gmail.relabel("m0002", add=["TRASH"])
    run(store, gmail)
    assert store.read_message("m0002") is None and store.thread_labels("gmail", "t0002") == []
    gmail.relabel("m0002", remove=["TRASH"])
    run(store, gmail)
    assert names(store.thread_labels("gmail", "t0002")) == ["Inbox"]


def test_renamed_and_deleted_labels_follow_on_next_sync(store, gmail):
    run(store, gmail)
    gmail.label_defs = {"Label_1": {"name": "Purchases"}}
    run(store, gmail)
    assert names(store.thread_labels("gmail", "t0001")) == ["Inbox", "Purchases"]
    assert "Family" not in names(store.labels())


def test_history_changes_sorting():
    ch = HistoryChanges()
    ch.add_page({"history": [
        {"messagesAdded": [{"message": {"id": "a"}}]},
        {"labelsAdded": [{"message": {"id": "a", "labelIds": ["Label_1"]}, "labelIds": ["Label_1"]}]},
        {"labelsAdded": [{"message": {"id": "b", "labelIds": ["INBOX", "Label_1"]}, "labelIds": ["Label_1"]}]},
        {"labelsRemoved": [{"message": {"id": "b", "labelIds": ["INBOX"]}, "labelIds": ["Label_1"]}]},
        {"labelsAdded": [{"message": {"id": "c", "labelIds": ["SPAM"]}, "labelIds": ["SPAM"]}]},
        {"labelsAdded": [{"message": {"id": "d"}, "labelIds": ["Label_1"]}]},  # no labels in the record: fetch
        {"labelsAdded": [{"message": {"id": "e", "labelIds": ["Label_1"]}, "labelIds": ["Label_1"]}]},
        {"messagesDeleted": [{"message": {"id": "e"}}]},
    ]})
    assert ch.changed == {"a", "c", "d"} and ch.deleted == {"e"}
    assert ch.label_only() == {"b": ["INBOX"]}


# --- MCP ------------------------------------------------------------------------

def test_tools_filter_and_show_labels(store):
    ids = _labelled_store(store)
    out = run_tool(store, "search", {"query": "plain", "label": "receipts"})
    assert [r["id"] for r in out["results"]] == ["m1"]  # per message: m2 in the same thread has no label
    assert out["results"][0]["labels"] == ["Receipts"]
    out = run_tool(store, "list_items", {"label": "Work"})
    assert [r["id"] for r in out["results"]] == ["m3"] and out["results"][0]["labels"] == ["Work/Clients"]
    out = run_tool(store, "list_items", {"label": "Later"})
    assert [r["id"] for r in out["results"]] == ["m4"]  # a tag covers its whole thread
    out = run_tool(store, "list_items", {"label": "nope"})
    assert out["results"] == [] and "no label" in out["note"].lower()
    plain = run_tool(store, "list_items", {})
    assert "labels" not in next(r for r in plain["results"] if r["id"] == "m2")
    labels = run_tool(store, "list_labels", {})["labels"]
    assert {"name": "Receipts", "type": "gmail_label", "source": "gmail", "count": 1} in labels
    assert {"name": "Later", "type": "tag", "source": "safecontext", "count": 1} in labels
    assert ids


@pytest.mark.parametrize("labels", [None, "drive", "exists"])
def test_browse_plans_never_read_every_label(store, labels):
    """On a 128k-message store the wrong join order took 12 s a page; the right one takes milliseconds.
    Guard the plans: labels are only ever looked up per item or per label, never by source alone."""
    ids = _labelled_store(store)
    lids = None if labels is None else [ids["Receipts"]]
    store.LABEL_DRIVE_MAX = 10 ** 9 if labels == "drive" else 0
    plans = []
    store._read = lambda sql, args=(): plans.extend(
        r[3] for r in store._rdb.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall()) or []
    store._kept_threads(["gmail"], None, 150, labels=lids)
    bad = [p for p in plans if p.endswith("ix_item_labels_item (source=?)") or p.endswith("ix_items_thread (source=?)")
           or p.startswith("SCAN il") or p.startswith("SCAN c")]
    assert plans and not bad, plans
