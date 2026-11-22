"""Local store of scrubbed mail: one SQLite file with FTS5 full-text search.

Only `ScrubbedMessage` content is ever written. Dropped messages are kept as
an id + reason (no content) so sync doesn't fetch them again.

Privacy settings:
  * files are 0600 inside a 0700 directory (WAL/SHM side files included)
  * secure_delete is on, so a message removed because it moved to Trash or
    was re-scrubbed is zeroed on disk, not just unlinked from the index
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Union
from urllib.parse import quote

from .scrub import SCRUBBER_VERSION, Attachment, Dropped, ScrubbedMessage

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS messages (
    id                TEXT PRIMARY KEY,
    thread_id         TEXT NOT NULL,
    date_ms           INTEGER NOT NULL,
    sender            TEXT NOT NULL DEFAULT '',
    sender_email      TEXT NOT NULL DEFAULT '',
    recipients        TEXT NOT NULL DEFAULT '',
    subject           TEXT NOT NULL DEFAULT '',
    body              TEXT NOT NULL DEFAULT '',
    attachments       TEXT NOT NULL DEFAULT '[]',
    attachment_names  TEXT NOT NULL DEFAULT '',
    message_id_header TEXT NOT NULL DEFAULT '',
    redactions        TEXT NOT NULL DEFAULT '{}',
    scrubber_version  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_thread ON messages(thread_id, date_ms);
CREATE INDEX IF NOT EXISTS ix_messages_sender ON messages(sender_email, date_ms);
CREATE INDEX IF NOT EXISTS ix_messages_date   ON messages(date_ms);

CREATE TABLE IF NOT EXISTS dropped (
    id               TEXT PRIMARY KEY,
    thread_id        TEXT NOT NULL,
    date_ms          INTEGER NOT NULL DEFAULT 0,
    reason           TEXT NOT NULL,
    scrubber_version TEXT NOT NULL
);

-- Ids listed during a full (re)sync; used to prune mail deleted in Gmail. Ids only.
CREATE TABLE IF NOT EXISTS sync_seen (id TEXT PRIMARY KEY);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, body, sender, recipients, attachment_names,
    content='messages', content_rowid='rowid',
    tokenize='porter unicode61 remove_diacritics 2'
);
"""
_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, subject, body, sender, recipients, attachment_names)
    VALUES (new.rowid, new.subject, new.body, new.sender, new.recipients, new.attachment_names);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, body, sender, recipients, attachment_names)
    VALUES ('delete', old.rowid, old.subject, old.body, old.sender, old.recipients, old.attachment_names);
END;
"""

DEFAULT_DATA_DIR = Path.home() / "Library" / "Application Support" / "MyContext"


# --- result shapes (what Grok / MCP tools return) -------------------------

@dataclass
class Citation:
    date: str      # ISO 8601, UTC
    sender: str
    link: str      # Gmail deep link


@dataclass
class Hit:
    id: str
    thread_id: str
    subject: str
    snippet: str
    citation: Citation


@dataclass
class MessageView:
    id: str
    thread_id: str
    subject: str
    recipients: str
    body: str
    attachments: List[Attachment]
    redactions: Dict[str, int]
    citation: Citation


# --- helpers --------------------------------------------------------------

def gmail_link(account: str, message_id_header: str, thread_id: str) -> str:
    """Link that opens the message in Gmail. Searching by RFC 822 Message-ID
    keeps working after the message changes labels; thread id is a fallback."""
    base = "https://mail.google.com/mail/"
    if account:
        base += f"?authuser={quote(account)}"
    mid = message_id_header.strip().strip("<>")
    if mid:
        return f"{base}#search/rfc822msgid%3A{quote(mid, safe='')}"
    return f"{base}#all/{thread_id}"


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


DateLike = Union[str, date, datetime, int, None]


def to_ms(value: DateLike) -> Optional[int]:
    """Accept 'YYYY-MM-DD', ISO datetimes, date/datetime, or epoch ms."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    if isinstance(value, date):
        return to_ms(datetime(value.year, value.month, value.day, tzinfo=timezone.utc))
    s = str(value).strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return to_ms(date.fromisoformat(s))
    return to_ms(datetime.fromisoformat(s))


_FTS_TOKEN = re.compile(r"[\w@.'+-]+", re.UNICODE)


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: every word is quoted (so
    operators and punctuation can't cause syntax errors) and all must match."""
    tokens = [t.strip(".'-") for t in _FTS_TOKEN.findall(text)]
    tokens = [t for t in tokens if t]
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _sender_email(sender: str) -> str:
    return parseaddr(sender)[1].lower()


def _recipients(to: str, cc: str) -> str:
    return ", ".join(f"{n} <{a}>" if n else a for n, a in getaddresses([to, cc]) if a)


# --- the store ------------------------------------------------------------

class Store:
    def __init__(self, path: Union[str, Path], account: str = "") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        old_umask = os.umask(0o077)  # also covers -wal / -shm files
        try:
            self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        finally:
            os.umask(old_umask)
        self._lock = threading.RLock()
        self._db.row_factory = sqlite3.Row
        for pragma in ("journal_mode=WAL", "secure_delete=ON", "synchronous=NORMAL", "foreign_keys=ON"):
            self._db.execute(f"PRAGMA {pragma}")
        # executescript manages its own transaction; schema DDL is idempotent.
        self._db.executescript(_SCHEMA + _FTS_TRIGGERS)
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        os.chmod(self.path, 0o600)
        self.account = account or (self.get_meta("account_email") or "")

    @classmethod
    def open_default(cls) -> "Store":
        return cls(DEFAULT_DATA_DIR / "mail.db")

    def close(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    # meta / sync state ------------------------------------------------------

    def get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: Optional[str]) -> None:
        with self._lock:
            if value is None:
                self._db.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                self._db.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # writes -----------------------------------------------------------------

    def put_many(self, results: Iterable[Union[ScrubbedMessage, Dropped]]) -> None:
        """Insert or replace. A message can move between kept and dropped
        (e.g. it was later labelled Promotions); the other row is removed."""
        with self._tx() as db:
            for r in results:
                db.execute("DELETE FROM messages WHERE id=?", (r.id,))
                db.execute("DELETE FROM dropped WHERE id=?", (r.id,))
                if isinstance(r, Dropped):
                    db.execute("INSERT INTO dropped VALUES (?,?,?,?,?)",
                               (r.id, r.thread_id, r.date_ms, r.reason, SCRUBBER_VERSION))
                elif isinstance(r, ScrubbedMessage):
                    db.execute(
                        "INSERT INTO messages (id, thread_id, date_ms, sender, sender_email, recipients,"
                        " subject, body, attachments, attachment_names, message_id_header, redactions,"
                        " scrubber_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (r.id, r.thread_id, r.date_ms, r.sender, _sender_email(r.sender),
                         _recipients(r.to, r.cc), r.subject, r.body,
                         json.dumps([dataclasses.asdict(a) for a in r.attachments]),
                         " ".join(a.filename for a in r.attachments), r.message_id_header, json.dumps(r.redactions), r.scrubber_version))
                else:  # never let anything else (e.g. a RawMessage) reach disk
                    raise TypeError(f"refusing to store {type(r).__name__}")

    def put(self, result: Union[ScrubbedMessage, Dropped]) -> None:
        self.put_many([result])

    def delete(self, ids: Sequence[str]) -> None:
        with self._tx() as db:
            for i in ids:
                db.execute("DELETE FROM messages WHERE id=?", (i,))
                db.execute("DELETE FROM dropped WHERE id=?", (i,))

    def wipe(self) -> None:
        """Remove all mail and sync state (used on disconnect)."""
        with self._tx() as db:
            db.execute("DELETE FROM messages")
            db.execute("DELETE FROM dropped")
            db.execute("DELETE FROM sync_seen")
            db.execute("DELETE FROM meta WHERE key != 'schema_version'")
        with self._lock:
            self._db.execute("VACUUM")
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # sync support -----------------------------------------------------------

    def known_ids(self, ids: Sequence[str], version: str = SCRUBBER_VERSION) -> Set[str]:
        """Ids already processed under this scrubber version (kept or dropped)."""
        found: Set[str] = set()
        ids = list(ids)
        with self._lock:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                for table in ("messages", "dropped"):
                    rows = self._db.execute(
                        f"SELECT id FROM {table} WHERE scrubber_version=? AND id IN ({marks})",
                        [version, *chunk])
                    found.update(r[0] for r in rows)
        return found

    def mark_seen(self, ids: Sequence[str]) -> None:
        with self._tx() as db:
            db.executemany("INSERT OR IGNORE INTO sync_seen(id) VALUES (?)", [(i,) for i in ids])

    def clear_seen(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sync_seen")

    def prune_unseen(self) -> int:
        """After a complete listing, remove local rows Gmail no longer lists."""
        with self._tx() as db:
            n = db.execute("DELETE FROM messages WHERE id NOT IN (SELECT id FROM sync_seen)").rowcount
            n += db.execute("DELETE FROM dropped WHERE id NOT IN (SELECT id FROM sync_seen)").rowcount
            db.execute("DELETE FROM sync_seen")
        return n

    def stale_ids(self, version: str = SCRUBBER_VERSION) -> List[str]:
        """Ids scrubbed under an older version; sync re-fetches these."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM messages WHERE scrubber_version != ? "
                "UNION SELECT id FROM dropped WHERE scrubber_version != ?", (version, version))
            return [r[0] for r in rows]

    def stats(self) -> Dict[str, object]:
        with self._lock:
            kept = self._db.execute("SELECT count(*) FROM messages").fetchone()[0]
            reasons = dict(self._db.execute(
                "SELECT reason, count(*) FROM dropped GROUP BY reason ORDER BY 2 DESC").fetchall())
            redactions: Dict[str, int] = {}
            for (blob,) in self._db.execute("SELECT redactions FROM messages WHERE redactions != '{}'"):
                for k, v in json.loads(blob).items():
                    redactions[k] = redactions.get(k, 0) + v
        return {"kept": kept, "dropped": sum(reasons.values()), "dropped_by_reason": reasons,
                "redactions": redactions}

    def counts(self):
        """(kept, dropped), cheap enough to poll."""
        with self._lock:
            return (self._db.execute("SELECT count(*) FROM messages").fetchone()[0],
                    self._db.execute("SELECT count(*) FROM dropped").fetchone()[0])

    def date_range(self):
        """(oldest, newest) date_ms of kept messages, or (None, None)."""
        with self._lock:
            return tuple(self._db.execute("SELECT min(date_ms), max(date_ms) FROM messages").fetchone())

    # reads ------------------------------------------------------------------

    def _citation(self, row: sqlite3.Row) -> Citation:
        return Citation(date=_iso(row["date_ms"]), sender=row["sender"],
                        link=gmail_link(self.account, row["message_id_header"], row["thread_id"]))

    def _view(self, row: sqlite3.Row) -> MessageView:
        return MessageView(
            id=row["id"], thread_id=row["thread_id"], subject=row["subject"], recipients=row["recipients"],
            body=row["body"], attachments=[Attachment(**a) for a in json.loads(row["attachments"])],
            redactions=json.loads(row["redactions"]), citation=self._citation(row))

    @staticmethod
    def _filters(sender: Optional[str], after: DateLike, before: DateLike, alias: str = "m"):
        where, args = [], []
        if sender:
            s = sender.strip().lower()
            if s.startswith("@"):
                where.append(f"{alias}.sender_email LIKE ?")
                args.append("%" + s)
            elif "@" in s:
                where.append(f"{alias}.sender_email = ?")
                args.append(s)
            else:
                where.append(f"(lower({alias}.sender) LIKE ? OR {alias}.sender_email LIKE ?)")
                args += [f"%{s}%", f"%{s}%"]
        a, b = to_ms(after), to_ms(before)
        if a is not None:
            where.append(f"{alias}.date_ms >= ?")
            args.append(a)
        if b is not None:
            where.append(f"{alias}.date_ms < ?")
            args.append(b)
        return where, args

    def search(self, query: str, *, sender: Optional[str] = None, after: DateLike = None,
               before: DateLike = None, limit: int = 20) -> List[Hit]:
        q = fts_query(query)
        if not q:
            return []
        where, args = self._filters(sender, after, before)
        sql = (
            "SELECT m.*, snippet(messages_fts, 1, '«', '»', ' … ', 24) AS snip "
            "FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid "
            "WHERE messages_fts MATCH ? " + "".join(f"AND {w} " for w in where) +
            # weights: subject, body, sender, recipients, attachment_names
            "ORDER BY bm25(messages_fts, 5.0, 1.0, 3.0, 1.0, 2.0) LIMIT ?")
        with self._lock:
            rows = self._db.execute(sql, [q, *args, max(1, min(limit, 100))]).fetchall()
        return [Hit(id=r["id"], thread_id=r["thread_id"], subject=r["subject"],
                    snippet=r["snip"] or r["body"][:200], citation=self._citation(r)) for r in rows]

    def read_thread(self, thread_id: str) -> List[MessageView]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages WHERE thread_id=? ORDER BY date_ms", (thread_id,)).fetchall()
        return [self._view(r) for r in rows]

    def read_message(self, message_id: str) -> Optional[MessageView]:
        with self._lock:
            row = self._db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return self._view(row) if row else None

    def list_messages(self, *, sender: Optional[str] = None, after: DateLike = None,
                      before: DateLike = None, limit: int = 50) -> List[Hit]:
        where, args = self._filters(sender, after, before)
        sql = ("SELECT m.* FROM messages m " + ("WHERE " + " AND ".join(where) + " " if where else "")
               + "ORDER BY m.date_ms DESC LIMIT ?")
        with self._lock:
            rows = self._db.execute(sql, [*args, max(1, min(limit, 200))]).fetchall()
        return [Hit(id=r["id"], thread_id=r["thread_id"], subject=r["subject"],
                    snippet=r["body"][:200], citation=self._citation(r)) for r in rows]
