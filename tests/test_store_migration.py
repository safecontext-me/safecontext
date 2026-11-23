"""Schema v1 (Gmail only) -> v2 (source-tagged items), in place: existing users'
synced mail must survive without a re-sync."""
import dataclasses
import json
import os
import sqlite3
import stat

import pytest

from safecontext import store as store_mod
from safecontext.demo.fake_gmail import FakeGmail
from safecontext.demo.synthetic import mailbox
from safecontext.scrub import Dropped, ScrubbedMessage, scrub
from safecontext.store import SCHEMA_VERSION, Store
from safecontext.sync import Syncer

# The v1 schema, verbatim from SafeContext before multi-source support.
V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, date_ms INTEGER NOT NULL,
    sender TEXT NOT NULL DEFAULT '', sender_email TEXT NOT NULL DEFAULT '', recipients TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '', attachments TEXT NOT NULL DEFAULT '[]',
    attachment_names TEXT NOT NULL DEFAULT '', message_id_header TEXT NOT NULL DEFAULT '',
    redactions TEXT NOT NULL DEFAULT '{}', scrubber_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_thread ON messages(thread_id, date_ms);
CREATE INDEX IF NOT EXISTS ix_messages_sender ON messages(sender_email, date_ms);
CREATE INDEX IF NOT EXISTS ix_messages_date   ON messages(date_ms);
CREATE TABLE IF NOT EXISTS dropped (
    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, date_ms INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL, scrubber_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_seen (id TEXT PRIMARY KEY);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, body, sender, recipients, attachment_names,
    content='messages', content_rowid='rowid', tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, subject, body, sender, recipients, attachment_names)
    VALUES (new.rowid, new.subject, new.body, new.sender, new.recipients, new.attachment_names);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, body, sender, recipients, attachment_names)
    VALUES ('delete', old.rowid, old.subject, old.body, old.sender, old.recipients, old.attachment_names);
END;
"""

ACCOUNT = "alex@example.org"


def make_v1(path, gmail_history_id="5000"):
    """A v1 database as SafeContext 0.1 left it after a completed sync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), isolation_level=None)
    db.executescript(V1_SCHEMA)
    kept = dropped = 0
    for m in mailbox():
        r = scrub(m)
        if isinstance(r, Dropped):
            db.execute("INSERT INTO dropped VALUES (?,?,?,?,?)", (r.id, r.thread_id, r.date_ms, r.reason, "1"))
            dropped += 1
        else:
            assert isinstance(r, ScrubbedMessage)
            from email.utils import parseaddr
            db.execute("INSERT INTO messages (id, thread_id, date_ms, sender, sender_email, recipients, subject, body,"
                       " attachments, attachment_names, message_id_header, redactions, scrubber_version)"
                       " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (r.id, r.thread_id, r.date_ms, r.sender, parseaddr(r.sender)[1].lower(), r.to, r.subject,
                        r.body, json.dumps([dataclasses.asdict(a) for a in r.attachments]),
                        " ".join(a.filename for a in r.attachments), r.message_id_header,
                        json.dumps(r.redactions), "1"))
            kept += 1
    for k, v in {"schema_version": "1", "account_email": ACCOUNT, "sync_mode": "full", "auth_state": "ok",
                 "history_id": gmail_history_id, "last_sync_ms": "1764000000000", "grok_model": "grok-x",
                 "sync_heartbeat": json.dumps({"pid": 1})}.items():
        db.execute("INSERT INTO meta VALUES (?,?)", (k, v))
    db.execute("INSERT INTO sync_seen VALUES ('m0001')")
    db.close()
    os.chmod(path, 0o600)
    return kept, dropped


def tables(s):
    return {r[0] for r in s._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.fixture
def v1(tmp_path):
    p = tmp_path / "data" / "mail.db"
    kept, dropped = make_v1(p)
    return p, kept, dropped


def test_rows_survive_and_move_to_gmail_source(v1):
    p, kept, dropped = v1
    s = Store(p)
    assert s.get_meta("schema_version") == str(SCHEMA_VERSION) == "2"
    assert s.counts("gmail") == (kept, dropped) == s.counts()
    assert {"messages", "messages_fts", "sync_seen"} & tables(s) == set()
    row = s._db.execute("SELECT source, kind, account FROM items LIMIT 1").fetchone()
    assert tuple(row) == ("gmail", "message", ACCOUNT)


def test_full_text_search_still_works_with_links(v1):
    s = Store(v1[0])
    (hit,) = s.search("netflix")
    assert hit.source == "gmail" and hit.subject
    assert hit.citation.link.startswith(f"https://mail.google.com/mail/?authuser={ACCOUNT.replace('@', '%40')}")
    assert "rfc822msgid" in hit.citation.link
    view = s.read_message(hit.id)
    assert view.recipients == view.participants and view.subject == view.title


def test_sync_state_moves_to_source_state(v1):
    s = Store(v1[0])
    g = s.state("gmail")
    assert (g.get("account"), g.get("history_id"), g.get("sync_mode"), g.get("auth_state"), g.get("last_sync_ms")) \
        == (ACCOUNT, "5000", "full", "ok", "1764000000000")
    assert s.get_meta("account_email") is None and s.get_meta("history_id") is None
    assert s.get_meta("sync_heartbeat") is None and g.get("sync_heartbeat") is None
    assert s.get_meta("grok_model") == "grok-x"  # app-wide settings stay in meta
    assert s.account == ACCOUNT


def test_dropped_and_seen_tables_carry_source(v1):
    s = Store(v1[0])
    cols = [r[1] for r in s._db.execute("PRAGMA table_info(dropped)")]
    assert cols == ["source", "id", "thread_id", "date_ms", "reason", "scrubber_version"]
    assert [tuple(r) for r in s._db.execute("SELECT source, id FROM seen")] == [("gmail", "m0001")]


def test_next_sync_is_incremental_not_a_refetch(v1):
    p, kept, _ = v1
    s = Store(p)
    gmail = FakeGmail(page_size=4)
    for m in mailbox():
        gmail.add(m, record=False)
    r = Syncer(s, gmail, batch_size=3).run()
    assert r.status == "ok" and r.phase == "incremental"
    assert sum(gmail.fetched.values()) == 0 and s.counts("gmail")[0] == kept


def test_reopen_after_migration_is_a_noop(v1):
    p, kept, dropped = v1
    Store(p).close()
    s = Store(p)
    assert s.counts() == (kept, dropped)
    assert s.search("netflix")


def test_failed_migration_leaves_v1_untouched(v1, monkeypatch):
    p, kept, dropped = v1

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store_mod, "gmail_link", boom)
    with pytest.raises(RuntimeError):
        Store(p)
    db = sqlite3.connect(str(p))
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == kept
    assert db.execute("SELECT value FROM meta WHERE key='account_email'").fetchone()[0] == ACCOUNT
    assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    db.close()
    monkeypatch.undo()
    s = Store(p)  # the next start tries again and succeeds
    assert s.counts() == (kept, dropped)


def test_permissions_and_secure_delete_kept(v1):
    s = Store(v1[0])
    assert stat.S_IMODE(os.stat(v1[0]).st_mode) == 0o600
    assert s._db.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_wipe_leaves_no_word_of_old_mail_in_the_file(v1):
    """secure_delete zeroes the v1 pages when the old tables are dropped, and wipe
    also clears the full-text index (whose terms would otherwise outlive the rows)."""
    p = v1[0]
    s = Store(p)
    s.wipe()  # wipe everything: no message text may remain anywhere in the file
    s.close()
    data = p.read_bytes()
    assert b"etflix" not in data
