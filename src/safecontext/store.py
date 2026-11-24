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
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Union
from urllib.parse import quote

from . import env as _env
from .scrub import SCRUBBER_VERSION, Attachment, Dropped, ScrubbedItem, ScrubbedMessage

SCHEMA_VERSION = 3

# Drop reasons from rules that were removed. Rows with these reasons are
# re-fetched once so the mail they hid can be kept under today's rules.
RETIRED_DROP_REASONS = ("bulk:no_reply_sender",)
_RETIRED = "retired"

# Schema v2: every source's items in one table, tagged by source.
#   source   the connected source ("gmail", "gcal", later "imessage", ...)
#   id       the item's id inside that source; (source, id) is the key
#   kind     "message" (email) | "event" | "chat" | "transcript"
#   date_ms  sent time, or an event's / call's start; end_ms its end, if any
#   sender   author / organizer;  participants: recipients / attendees / members
#   title    subject / event title;  body: the scrubbed text
#   link     deep link back to the original (Gmail, Google Calendar, ...)
#   extra    short labelled fields shown with the item (JSON), e.g. Location
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Per-source sync state: cursor, account, auth state, last sync, heartbeat.
CREATE TABLE IF NOT EXISTS source_state (
    source TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
    PRIMARY KEY (source, key)
);

CREATE TABLE IF NOT EXISTS items (
    source            TEXT NOT NULL,
    id                TEXT NOT NULL,
    account           TEXT NOT NULL DEFAULT '',
    kind              TEXT NOT NULL,
    thread_id         TEXT NOT NULL,
    date_ms           INTEGER NOT NULL,
    end_ms            INTEGER,
    sender            TEXT NOT NULL DEFAULT '',
    sender_email      TEXT NOT NULL DEFAULT '',
    participants      TEXT NOT NULL DEFAULT '',
    title             TEXT NOT NULL DEFAULT '',
    body              TEXT NOT NULL DEFAULT '',
    attachments       TEXT NOT NULL DEFAULT '[]',
    attachment_names  TEXT NOT NULL DEFAULT '',
    link              TEXT NOT NULL DEFAULT '',
    extra             TEXT NOT NULL DEFAULT '{}',
    redactions        TEXT NOT NULL DEFAULT '{}',
    scrubber_version  TEXT NOT NULL,
    PRIMARY KEY (source, id)
);
CREATE INDEX IF NOT EXISTS ix_items_thread ON items(source, thread_id, date_ms);
CREATE INDEX IF NOT EXISTS ix_items_sender ON items(sender_email, date_ms);
CREATE INDEX IF NOT EXISTS ix_items_date   ON items(date_ms);
CREATE INDEX IF NOT EXISTS ix_items_source ON items(source, date_ms);

CREATE TABLE IF NOT EXISTS dropped (
    source           TEXT NOT NULL,
    id               TEXT NOT NULL,
    thread_id        TEXT NOT NULL,
    date_ms          INTEGER NOT NULL DEFAULT 0,
    reason           TEXT NOT NULL,
    scrubber_version TEXT NOT NULL,
    PRIMARY KEY (source, id)
);

-- Ids listed during a full (re)sync; used to prune items deleted at the source. Ids only.
CREATE TABLE IF NOT EXISTS seen (source TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY (source, id));

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, body, sender, participants, attachment_names, extra,
    content='items', content_rowid='rowid',
    tokenize='porter unicode61 remove_diacritics 2'
);
"""
_FTS_COLUMNS = "title, body, sender, participants, attachment_names, extra"
_FTS_TRIGGERS = f"""
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, {_FTS_COLUMNS})
    VALUES (new.rowid, new.title, new.body, new.sender, new.participants, new.attachment_names, new.extra);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, {_FTS_COLUMNS})
    VALUES ('delete', old.rowid, old.title, old.body, old.sender, old.participants, old.attachment_names, old.extra);
END;
"""

# Schema v3 adds only indexes, for browsing (the Mail view): dropped rows by
# date, source, thread and reason. They're created after the v1 migration,
# because a v1 `dropped` table has no source column yet.
_BROWSE_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_dropped_date   ON dropped(date_ms);
CREATE INDEX IF NOT EXISTS ix_dropped_source ON dropped(source, date_ms);
CREATE INDEX IF NOT EXISTS ix_dropped_thread ON dropped(source, thread_id);
CREATE INDEX IF NOT EXISTS ix_dropped_reason ON dropped(reason, date_ms);
"""

GMAIL = "gmail"
# Schema v1 kept Gmail's sync state in `meta`; v2 moves it to source_state('gmail').
_V1_GMAIL_META = {"account_email": "account", "sync_mode": "sync_mode", "auth_state": "auth_state",
                  "history_id": "history_id", "backfill_start_history_id": "backfill_start_history_id",
                  "backfill_page_token": "backfill_page_token", "resync": "resync", "last_sync_ms": "last_sync_ms"}

# …/SafeContext for the packaged app, …/SafeContext-Dev for a source checkout (see env.py).
DEFAULT_DATA_DIR = _env.default_data_dir()


# --- result shapes (what Grok / MCP tools return) -------------------------

@dataclass
class Citation:
    date: str      # ISO 8601, UTC
    sender: str
    link: str      # deep link to the original (Gmail, Google Calendar, ...)
    source: str = GMAIL
    title: str = ""


@dataclass
class Hit:
    id: str
    thread_id: str
    title: str
    snippet: str
    citation: Citation
    source: str = GMAIL
    kind: str = "message"
    end: Optional[str] = None

    @property
    def subject(self) -> str:
        return self.title


@dataclass
class ItemView:
    id: str
    thread_id: str
    title: str
    participants: str
    body: str
    attachments: List[Attachment]
    redactions: Dict[str, int]
    citation: Citation
    source: str = GMAIL
    kind: str = "message"
    end: Optional[str] = None
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def subject(self) -> str:
        return self.title

    @property
    def recipients(self) -> str:
        return self.participants


MessageView = ItemView  # the v1 name


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


def _iso_or_none(ms: Optional[int]) -> Optional[str]:
    return None if ms is None else _iso(ms)


def _handle(sender: str) -> str:
    """Lower-cased address for filtering: the email address, or the handle
    itself (a phone number, a chat name) when there is no address."""
    addr = parseaddr(sender)[1]
    return (addr or sender).strip().lower()


StoreResult = Union[ScrubbedMessage, ScrubbedItem, Dropped]


class SourceState:
    """Key/value sync state for one source (cursor, account, auth state...)."""

    def __init__(self, store: "Store", source: str) -> None:
        self.store, self.source = store, source

    def get(self, key: str) -> Optional[str]:
        return self.store.get_state(self.source, key)

    def set(self, key: str, value: Optional[str]) -> None:
        self.store.set_state(self.source, key, value)


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
        if self._has_table("messages"):
            self._migrate_v1()
        self._db.executescript(_BROWSE_INDEXES)  # v2 -> v3: indexes only, idempotent
        self._requeue_retired_drops()
        version = self.get_meta("schema_version")
        if version is None or (version.isdigit() and int(version) < SCHEMA_VERSION):
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        os.chmod(self.path, 0o600)
        self._account = account

    @property
    def account(self) -> str:
        """The Gmail account, used in Gmail deep links."""
        return self._account or (self.get_state(GMAIL, "account") or "")

    @account.setter
    def account(self, value: str) -> None:
        self._account = value

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

    def _has_table(self, name: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                    (name,)).fetchone() is not None

    # migration ----------------------------------------------------------------

    def _migrate_v1(self) -> None:
        """Schema v1 (Gmail only: `messages`, `dropped`, `sync_seen`, state in
        `meta`) -> v2, in place and in one transaction, so synced mail survives
        without a re-sync. If anything fails, the transaction rolls back and v1
        is left exactly as it was; the next start tries again."""
        with self._tx() as db:
            meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
            account = (meta.get("account_email") or "").lower()
            cur = db.execute("SELECT * FROM messages ORDER BY rowid")
            while True:
                rows = cur.fetchmany(500)
                if not rows:
                    break
                db.executemany(
                    "INSERT OR REPLACE INTO items (source, id, account, kind, thread_id, date_ms, end_ms, sender,"
                    " sender_email, participants, title, body, attachments, attachment_names, link, extra,"
                    " redactions, scrubber_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(GMAIL, r["id"], account, "message", r["thread_id"], r["date_ms"], None, r["sender"],
                      r["sender_email"], r["recipients"], r["subject"], r["body"], r["attachments"],
                      r["attachment_names"], gmail_link(account, r["message_id_header"], r["thread_id"]), "{}",
                      r["redactions"], r["scrubber_version"]) for r in rows])
            cols = [r[1] for r in db.execute("PRAGMA table_info(dropped)")]
            if "source" not in cols:  # v1 dropped table: rebuild with a source column
                db.execute("ALTER TABLE dropped RENAME TO dropped_v1")
                db.execute("""CREATE TABLE dropped (
                    source TEXT NOT NULL, id TEXT NOT NULL, thread_id TEXT NOT NULL,
                    date_ms INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL, scrubber_version TEXT NOT NULL,
                    PRIMARY KEY (source, id))""")
                db.execute("INSERT INTO dropped SELECT ?, id, thread_id, date_ms, reason, scrubber_version "
                           "FROM dropped_v1", (GMAIL,))
                db.execute("DROP TABLE dropped_v1")
            if self._has_table("sync_seen"):
                db.execute("INSERT OR IGNORE INTO seen SELECT ?, id FROM sync_seen", (GMAIL,))
                db.execute("DROP TABLE sync_seen")
            for old, new in _V1_GMAIL_META.items():
                if meta.get(old) is not None:
                    db.execute("INSERT OR REPLACE INTO source_state VALUES (?,?,?)", (GMAIL, new, meta[old]))
            db.execute("DELETE FROM meta WHERE key IN (%s)" % ",".join("?" * (len(_V1_GMAIL_META) + 1)),
                       [*_V1_GMAIL_META, "sync_heartbeat"])
            db.execute("DROP TABLE messages_fts")  # its triggers go with `messages`
            db.execute("DROP TABLE messages")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def _requeue_retired_drops(self) -> None:
        """Mark mail dropped by a rule that no longer exists as stale, so sync
        re-fetches and re-scrubs just those messages instead of the whole
        mailbox. Idempotent: the retired rule never produces new rows."""
        marks = ",".join("?" * len(RETIRED_DROP_REASONS))
        with self._lock:
            self._db.execute(f"UPDATE dropped SET scrubber_version=? WHERE source=? AND reason IN ({marks}) "
                             "AND scrubber_version != ?", [_RETIRED, GMAIL, *RETIRED_DROP_REASONS, _RETIRED])

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

    def get_state(self, source: str, key: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM source_state WHERE source=? AND key=?",
                                   (source, key)).fetchone()
        return row[0] if row else None

    def set_state(self, source: str, key: str, value: Optional[str]) -> None:
        with self._lock:
            if value is None:
                self._db.execute("DELETE FROM source_state WHERE source=? AND key=?", (source, key))
            else:
                self._db.execute("INSERT INTO source_state(source, key, value) VALUES(?, ?, ?) "
                                 "ON CONFLICT(source, key) DO UPDATE SET value=excluded.value",
                                 (source, key, value))

    def state(self, source: str) -> SourceState:
        return SourceState(self, source)

    # writes -----------------------------------------------------------------

    def _row(self, r: Union[ScrubbedMessage, ScrubbedItem], source: str, account: str) -> tuple:
        if isinstance(r, ScrubbedMessage):
            return (source, r.id, account, "message", r.thread_id, r.date_ms, None, r.sender,
                    _sender_email(r.sender), _recipients(r.to, r.cc), r.subject, r.body,
                    json.dumps([dataclasses.asdict(a) for a in r.attachments]),
                    " ".join(a.filename for a in r.attachments),
                    gmail_link(account, r.message_id_header, r.thread_id), "{}",
                    json.dumps(r.redactions), r.scrubber_version)
        return (source, r.id, account, r.kind, r.thread_id, r.date_ms, r.end_ms, r.sender, _handle(r.sender),
                ", ".join(p for p in r.participants if p), r.title, r.body,
                json.dumps([dataclasses.asdict(a) for a in r.attachments]),
                " ".join(a.filename for a in r.attachments), r.link,
                json.dumps(r.extra, ensure_ascii=False), json.dumps(r.redactions), r.scrubber_version)

    def put_many(self, results: Iterable[StoreResult], source: str = GMAIL, account: Optional[str] = None) -> None:
        """Insert or replace one source's results. An item can move between kept
        and dropped (e.g. it was later labelled Promotions); the other row is
        removed. Only scrubbed results are accepted."""
        if account is None:
            account = self.account if source == GMAIL else (self.get_state(source, "account") or "")
        with self._tx() as db:
            for r in results:
                db.execute("DELETE FROM items WHERE source=? AND id=?", (source, r.id))
                db.execute("DELETE FROM dropped WHERE source=? AND id=?", (source, r.id))
                if isinstance(r, Dropped):
                    db.execute("INSERT INTO dropped VALUES (?,?,?,?,?,?)",
                               (source, r.id, r.thread_id, r.date_ms, r.reason, SCRUBBER_VERSION))
                elif isinstance(r, (ScrubbedMessage, ScrubbedItem)):
                    db.execute(
                        "INSERT INTO items (source, id, account, kind, thread_id, date_ms, end_ms, sender,"
                        " sender_email, participants, title, body, attachments, attachment_names, link, extra,"
                        " redactions, scrubber_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        self._row(r, source, account))
                else:  # never let anything else (e.g. a RawMessage / RawItem) reach disk
                    raise TypeError(f"refusing to store {type(r).__name__}")

    def put(self, result: StoreResult, source: str = GMAIL) -> None:
        self.put_many([result], source=source)

    def delete(self, ids: Sequence[str], source: str = GMAIL) -> None:
        with self._tx() as db:
            for i in ids:
                db.execute("DELETE FROM items WHERE source=? AND id=?", (source, i))
                db.execute("DELETE FROM dropped WHERE source=? AND id=?", (source, i))

    def _compact(self) -> None:
        with self._lock:
            self._db.execute("VACUUM")
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def wipe(self) -> None:
        """Remove every source's items and sync state."""
        with self._tx() as db:
            for table in ("items", "dropped", "seen", "source_state"):
                db.execute(f"DELETE FROM {table}")
            # Deleting rows only marks their index entries deleted; the words stay in
            # older FTS segments until merged. Clear the index so no word survives.
            db.execute("INSERT INTO items_fts(items_fts) VALUES('delete-all')")
            db.execute("DELETE FROM meta WHERE key != 'schema_version'")
        self._compact()

    def wipe_source(self, source: str) -> None:
        """Remove one source's items, dropped ids and sync state (used when it's disconnected)."""
        with self._tx() as db:
            for table in ("items", "dropped", "seen", "source_state"):
                db.execute(f"DELETE FROM {table} WHERE source=?", (source,))
            # Merge FTS segments so the deleted source's words leave the index too.
            db.execute("INSERT INTO items_fts(items_fts) VALUES('optimize')")
        self._compact()

    # sync support -----------------------------------------------------------

    def known_ids(self, ids: Sequence[str], version: str = SCRUBBER_VERSION, source: str = GMAIL) -> Set[str]:
        """Ids already processed under this scrubber version (kept or dropped)."""
        found: Set[str] = set()
        ids = list(ids)
        with self._lock:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                for table in ("items", "dropped"):
                    rows = self._db.execute(
                        f"SELECT id FROM {table} WHERE source=? AND scrubber_version=? AND id IN ({marks})",
                        [source, version, *chunk])
                    found.update(r[0] for r in rows)
        return found

    def ids(self, source: str) -> Set[str]:
        """Every id stored for a source, kept or dropped."""
        with self._lock:
            rows = self._db.execute("SELECT id FROM items WHERE source=? UNION SELECT id FROM dropped WHERE source=?",
                                    (source, source))
            return {r[0] for r in rows}

    def mark_seen(self, ids: Sequence[str], source: str = GMAIL) -> None:
        with self._tx() as db:
            db.executemany("INSERT OR IGNORE INTO seen(source, id) VALUES (?, ?)", [(source, i) for i in ids])

    def clear_seen(self, source: str = GMAIL) -> None:
        with self._lock:
            self._db.execute("DELETE FROM seen WHERE source=?", (source,))

    def prune_unseen(self, source: str = GMAIL) -> int:
        """After a complete listing, remove local rows the source no longer lists."""
        with self._tx() as db:
            n = 0
            for table in ("items", "dropped"):
                n += db.execute(f"DELETE FROM {table} WHERE source=? AND id NOT IN "
                                "(SELECT id FROM seen WHERE source=?)", (source, source)).rowcount
            db.execute("DELETE FROM seen WHERE source=?", (source,))
        return n

    def stale_ids(self, version: str = SCRUBBER_VERSION, source: str = GMAIL) -> List[str]:
        """Ids scrubbed under an older version; sync re-fetches these."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM items WHERE source=? AND scrubber_version != ? "
                "UNION SELECT id FROM dropped WHERE source=? AND scrubber_version != ?",
                (source, version, source, version))
            return [r[0] for r in rows]

    @staticmethod
    def _source_where(source: Optional[str]):
        return ("WHERE source=?", [source]) if source else ("", [])

    def stats(self, source: Optional[str] = None) -> Dict[str, object]:
        where, args = self._source_where(source)
        with self._lock:
            kept = self._db.execute(f"SELECT count(*) FROM items {where}", args).fetchone()[0]
            reasons = dict(self._db.execute(
                f"SELECT reason, count(*) FROM dropped {where} GROUP BY reason ORDER BY 2 DESC", args).fetchall())
            redactions: Dict[str, int] = {}
            cond = (where + " AND" if where else "WHERE")
            for (blob,) in self._db.execute(f"SELECT redactions FROM items {cond} redactions != '{{}}'", args):
                for k, v in json.loads(blob).items():
                    redactions[k] = redactions.get(k, 0) + v
        return {"kept": kept, "dropped": sum(reasons.values()), "dropped_by_reason": reasons,
                "redactions": redactions}

    def counts(self, source: Optional[str] = None):
        """(kept, dropped), cheap enough to poll."""
        where, args = self._source_where(source)
        with self._lock:
            return (self._db.execute(f"SELECT count(*) FROM items {where}", args).fetchone()[0],
                    self._db.execute(f"SELECT count(*) FROM dropped {where}", args).fetchone()[0])

    def date_range(self, source: Optional[str] = None):
        """(oldest, newest) date_ms of kept items, or (None, None)."""
        where, args = self._source_where(source)
        with self._lock:
            return tuple(self._db.execute(f"SELECT min(date_ms), max(date_ms) FROM items {where}", args).fetchone())

    def source_ids(self) -> List[str]:
        """Sources that have sync state or items on this Mac."""
        with self._lock:
            rows = self._db.execute("SELECT DISTINCT source FROM source_state UNION "
                                    "SELECT DISTINCT source FROM items ORDER BY 1")
            return [r[0] for r in rows]

    # reads ------------------------------------------------------------------

    @staticmethod
    def _citation(row: sqlite3.Row) -> Citation:
        return Citation(date=_iso(row["date_ms"]), sender=row["sender"], link=row["link"],
                        source=row["source"], title=row["title"])

    def _hit(self, r: sqlite3.Row, snippet: str) -> Hit:
        return Hit(id=r["id"], thread_id=r["thread_id"], title=r["title"], snippet=snippet,
                   citation=self._citation(r), source=r["source"], kind=r["kind"], end=_iso_or_none(r["end_ms"]))

    def _view(self, row: sqlite3.Row) -> ItemView:
        return ItemView(
            id=row["id"], thread_id=row["thread_id"], title=row["title"], participants=row["participants"],
            body=row["body"], attachments=[Attachment(**a) for a in json.loads(row["attachments"])],
            redactions=json.loads(row["redactions"]), citation=self._citation(row), source=row["source"],
            kind=row["kind"], end=_iso_or_none(row["end_ms"]), extra=json.loads(row["extra"] or "{}"))

    @staticmethod
    def _filters(sender: Optional[str], after: DateLike, before: DateLike, alias: str = "m",
                 source: Optional[str] = None, kind: Optional[str] = None):
        where, args = [], []
        if source:
            where.append(f"{alias}.source = ?")
            args.append(source)
        if kind:
            where.append(f"{alias}.kind = ?")
            args.append(kind)
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
        if a is not None:  # an event counts if any part of it falls in the range
            where.append(f"coalesce({alias}.end_ms, {alias}.date_ms) >= ?")
            args.append(a)
        if b is not None:
            where.append(f"{alias}.date_ms < ?")
            args.append(b)
        return where, args

    def search(self, query: str, *, source: Optional[str] = None, kind: Optional[str] = None,
               sender: Optional[str] = None, after: DateLike = None, before: DateLike = None,
               limit: int = 20) -> List[Hit]:
        q = fts_query(query)
        if not q:
            return []
        where, args = self._filters(sender, after, before, source=source, kind=kind)
        sql = (
            "SELECT m.*, snippet(items_fts, 1, '«', '»', ' … ', 24) AS snip "
            "FROM items_fts JOIN items m ON m.rowid = items_fts.rowid "
            "WHERE items_fts MATCH ? " + "".join(f"AND {w} " for w in where) +
            # weights: title, body, sender, participants, attachment_names, extra
            "ORDER BY bm25(items_fts, 5.0, 1.0, 3.0, 1.0, 2.0, 1.0) LIMIT ?")
        with self._lock:
            rows = self._db.execute(sql, [q, *args, max(1, min(limit, 100))]).fetchall()
        return [self._hit(r, r["snip"] or r["body"][:200]) for r in rows]

    def read_thread(self, thread_id: str, source: Optional[str] = None) -> List[ItemView]:
        """Every item in a thread (an email thread, a chat, a recurring event),
        oldest first. Without `source`, thread ids are matched in every source."""
        where, args = self._source_where(source)
        cond = (where + " AND" if where else "WHERE")
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM items {cond} thread_id=? ORDER BY date_ms",
                                    [*args, thread_id]).fetchall()
        return [self._view(r) for r in rows]

    def read_item(self, item_id: str, source: Optional[str] = None) -> Optional[ItemView]:
        where, args = self._source_where(source)
        cond = (where + " AND" if where else "WHERE")
        with self._lock:
            row = self._db.execute(f"SELECT * FROM items {cond} id=? LIMIT 1", [*args, item_id]).fetchone()
        return self._view(row) if row else None

    def read_message(self, message_id: str) -> Optional[ItemView]:
        return self.read_item(message_id, GMAIL)

    def list_items(self, *, source: Optional[str] = None, kind: Optional[str] = None,
                   sender: Optional[str] = None, after: DateLike = None, before: DateLike = None,
                   limit: int = 50, oldest_first: bool = False) -> List[Hit]:
        where, args = self._filters(sender, after, before, source=source, kind=kind)
        sql = ("SELECT m.* FROM items m " + ("WHERE " + " AND ".join(where) + " " if where else "")
               + f"ORDER BY m.date_ms {'ASC' if oldest_first else 'DESC'} LIMIT ?")
        with self._lock:
            rows = self._db.execute(sql, [*args, max(1, min(limit, 200))]).fetchall()
        return [self._hit(r, r["body"][:200]) for r in rows]

    def list_messages(self, *, sender: Optional[str] = None, after: DateLike = None,
                      before: DateLike = None, limit: int = 50) -> List[Hit]:
        """Gmail only (the v1 call)."""
        return self.list_items(source=GMAIL, sender=sender, after=after, before=before, limit=limit)

    # browsing (the Mail view) ---------------------------------------------------
    #
    # Lists are keyset-paged, newest first, so a page costs the same at the top of
    # a 50,000-message mailbox as at the bottom. A page is a list of rows: one per
    # thread for kept items (its newest item stands for it), one per dropped item.
    # Rows are ordered by (date_ms, t, rowid) descending, where t is 1 for kept
    # threads and 0 for dropped items; the cursor is the last row's key.

    _SNIPPET_CHARS = 240
    _THREAD_COLS = (
        "m.rowid AS rid, m.source, m.id, m.thread_id, m.kind, m.date_ms, m.end_ms, m.sender, m.title, "
        "m.redactions, m.attachment_names, "
        "(SELECT count(*) FROM items c WHERE c.source = m.source AND c.thread_id = m.thread_id) AS n_items, "
        "(SELECT count(*) FROM dropped d WHERE d.source = m.source AND d.thread_id = m.thread_id) AS n_dropped, "
        "(SELECT coalesce(sum(j.value), 0) FROM items c, json_each(c.redactions) j "
        " WHERE c.source = m.source AND c.thread_id = m.thread_id) AS n_redacted")

    @staticmethod
    def parse_cursor(cursor: Optional[str]):
        """'date.t.rowid' -> (date_ms, t, rowid), or None for the first page."""
        if not cursor:
            return None
        try:
            d, t, r = (int(x) for x in str(cursor).split("."))
        except ValueError:
            raise ValueError("bad cursor") from None
        return d, (1 if t else 0), r

    @staticmethod
    def _after(alias: str, t: int, cur) -> tuple:
        """SQL condition (and args) for rows of kind `t` that come after `cur` in the order."""
        if cur is None:
            return "", []
        d, ct, r = cur
        if t == ct:  # same table: plain keyset on (date_ms, rowid)
            return f"AND {alias}.date_ms <= ? AND ({alias}.date_ms < ? OR {alias}.rowid < ?) ", [d, d, r]
        if t > ct:  # kept rows sort before dropped rows of the same date: those were all shown
            return f"AND {alias}.date_ms < ? ", [d]
        return f"AND {alias}.date_ms <= ? ", [d]  # dropped after a kept row of the same date: all still to come

    @staticmethod
    def _marks(values: Sequence) -> str:
        return ",".join("?" * len(values))

    def _thread_row(self, r: sqlite3.Row, snippet: Optional[str] = None) -> Dict[str, object]:
        return {"type": "thread", "source": r["source"], "id": r["id"], "thread_id": r["thread_id"],
                "kind": r["kind"], "date": _iso(r["date_ms"]), "end": _iso_or_none(r["end_ms"]),
                "sender": r["sender"], "title": r["title"],
                "snippet": snippet if snippet is not None else r["snip"],
                "count": r["n_items"], "dropped_in_thread": r["n_dropped"], "redacted": r["n_redacted"],
                "has_attachments": bool(r["attachment_names"]),
                "cursor": f"{r['date_ms']}.1.{r['rid']}"}

    def _dropped_row(self, r: sqlite3.Row) -> Dict[str, object]:
        """A dropped item: only what the dropped table keeps (no content), plus the subject
        of its thread when another message of that thread was kept (already scrubbed)."""
        return {"type": "dropped", "source": r["source"], "id": r["id"], "thread_id": r["thread_id"],
                "date": _iso(r["date_ms"]) if r["date_ms"] else None, "reason": r["reason"],
                "thread_title": r["thread_title"] or "",
                "link": gmail_link(self.account, "", r["thread_id"]) if r["source"] == GMAIL else "",
                "cursor": f"{r['date_ms']}.0.{r['rid']}"}

    def _kept_threads(self, sources: Sequence[str], cur, limit: int) -> List[sqlite3.Row]:
        after, args = self._after("m", 1, cur)
        sql = (f"SELECT {self._THREAD_COLS}, substr(m.body, 1, {self._SNIPPET_CHARS}) AS snip FROM items m "
               f"WHERE m.source IN ({self._marks(sources)}) {after}"
               "AND NOT EXISTS (SELECT 1 FROM items x WHERE x.source = m.source AND x.thread_id = m.thread_id "
               "  AND (x.date_ms > m.date_ms OR (x.date_ms = m.date_ms AND x.rowid > m.rowid))) "
               "ORDER BY m.date_ms DESC, m.rowid DESC LIMIT ?")
        with self._lock:
            return self._db.execute(sql, [*sources, *args, limit]).fetchall()

    def _dropped_items(self, sources: Sequence[str], reasons: Optional[Sequence[str]], cur,
                       limit: int) -> List[sqlite3.Row]:
        after, args = self._after("d", 0, cur)
        where = f"d.source IN ({self._marks(sources)}) "
        params: List[object] = list(sources)
        if reasons is not None:
            where += f"AND d.reason IN ({self._marks(reasons)}) "
            params += list(reasons)
        sql = ("SELECT d.rowid AS rid, d.source, d.id, d.thread_id, d.date_ms, d.reason, "
               "(SELECT i.title FROM items i WHERE i.source = d.source AND i.thread_id = d.thread_id "
               " ORDER BY i.date_ms LIMIT 1) AS thread_title "
               f"FROM dropped d WHERE {where}{after}ORDER BY d.date_ms DESC, d.rowid DESC LIMIT ?")
        with self._lock:
            return self._db.execute(sql, [*params, *args, limit]).fetchall()

    def browse(self, sources: Sequence[str], *, view: str = "all", reasons: Optional[Sequence[str]] = None,
               cursor: Optional[str] = None, limit: int = 100) -> Dict[str, object]:
        """One page of the Mail view's list. view: 'all' (kept threads and dropped items,
        interleaved by date), 'synced' (kept threads) or 'not_synced' (dropped items,
        optionally only these reason codes). Returns {"rows": [...], "next": cursor | None}."""
        if view not in ("all", "synced", "not_synced"):
            raise ValueError(f"unknown view {view!r}")
        limit = max(1, min(int(limit), 500))
        sources = list(sources)
        if not sources or (reasons is not None and not reasons):
            return {"rows": [], "next": None}
        cur = self.parse_cursor(cursor)
        merged: List[tuple] = []
        full = False
        if view in ("all", "synced"):
            kept = self._kept_threads(sources, cur, limit)
            full |= len(kept) == limit
            merged += [((r["date_ms"], 1, r["rid"]), self._thread_row(r)) for r in kept]
        if view in ("all", "not_synced"):
            dropped = self._dropped_items(sources, reasons if view == "not_synced" else None, cur, limit)
            full |= len(dropped) == limit
            merged += [((r["date_ms"], 0, r["rid"]), self._dropped_row(r)) for r in dropped]
        merged.sort(key=lambda kv: kv[0], reverse=True)
        more = full or len(merged) > limit
        rows = [row for _, row in merged[:limit]]
        return {"rows": rows, "next": rows[-1]["cursor"] if more and rows else None}

    def search_threads(self, query: str, sources: Sequence[str], *, offset: int = 0,
                       limit: int = 100) -> Dict[str, object]:
        """Threads with an item matching `query` (FTS), newest first; each row shows the
        thread's newest matching item with a highlighted snippet. Offset-paged."""
        q = fts_query(query)
        sources = list(sources)
        limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
        if not q or not sources:
            return {"rows": [], "next": None}
        # A bare column next to max() takes its value from the row holding the max (SQLite).
        sql = ("SELECT m.rowid AS rid, max(m.date_ms) AS last FROM items_fts JOIN items m ON m.rowid = items_fts.rowid "
               f"WHERE items_fts MATCH ? AND m.source IN ({self._marks(sources)}) "
               "GROUP BY m.source, m.thread_id ORDER BY last DESC, rid DESC LIMIT ? OFFSET ?")
        with self._lock:
            rids = [r["rid"] for r in self._db.execute(sql, [q, *sources, limit, offset])]
            if not rids:
                return {"rows": [], "next": None}
            rows = self._db.execute(
                f"SELECT {self._THREAD_COLS}, snippet(items_fts, 1, '«', '»', ' … ', 24) AS snip "
                "FROM items_fts JOIN items m ON m.rowid = items_fts.rowid "
                f"WHERE items_fts MATCH ? AND items_fts.rowid IN ({self._marks(rids)})", [q, *rids]).fetchall()
        order = {rid: n for n, rid in enumerate(rids)}
        out = [self._thread_row(r) for r in sorted(rows, key=lambda r: order[r["rid"]])]
        return {"rows": out, "next": str(offset + limit) if len(rids) == limit else None}

    def thread_detail(self, source: str, thread_id: str) -> Dict[str, object]:
        """A thread for the reader: every kept item in full and every dropped item's
        reason and date, oldest first."""
        with self._lock:
            items = self._db.execute("SELECT * FROM items WHERE source=? AND thread_id=? ORDER BY date_ms, rowid",
                                     (source, thread_id)).fetchall()
            dropped = self._db.execute(
                "SELECT rowid AS rid, source, id, thread_id, date_ms, reason, '' AS thread_title "
                "FROM dropped WHERE source=? AND thread_id=? ORDER BY date_ms, rowid", (source, thread_id)).fetchall()
        entries: List[tuple] = []
        for r in items:
            v = self._view(r)
            entries.append((r["date_ms"], {
                "type": "item", "id": v.id, "kind": v.kind, "source": v.source, "date": v.citation.date,
                "end": v.end, "sender": v.citation.sender, "participants": v.participants, "title": v.title,
                "body": v.body, "attachments": [dataclasses.asdict(a) for a in v.attachments],
                "redactions": v.redactions, "link": v.citation.link, "details": v.extra}))
        for r in dropped:
            entries.append((r["date_ms"], self._dropped_row(r)))
        entries.sort(key=lambda kv: kv[0])
        return {"source": source, "thread_id": thread_id, "entries": [e for _, e in entries]}

    def coverage(self, source: str) -> Dict[str, object]:
        """What's on this Mac for one source: counts, dropped reasons, date range, and how
        many items are queued for re-scrubbing after a rule change."""
        with self._lock:
            db = self._db
            kept = db.execute("SELECT count(*) FROM items WHERE source=?", (source,)).fetchone()[0]
            reasons = dict(db.execute("SELECT reason, count(*) FROM dropped WHERE source=? GROUP BY reason "
                                      "ORDER BY 2 DESC", (source,)).fetchall())
            lo, hi = db.execute(
                "SELECT min(lo), max(hi) FROM ("
                " SELECT min(date_ms) AS lo, max(date_ms) AS hi FROM items WHERE source=?"
                " UNION ALL SELECT min(date_ms), max(date_ms) FROM dropped WHERE source=? AND date_ms > 0)",
                (source, source)).fetchone()
            stale = db.execute(
                "SELECT (SELECT count(*) FROM items WHERE source=? AND scrubber_version != ?)"
                " + (SELECT count(*) FROM dropped WHERE source=? AND scrubber_version != ?)",
                (source, SCRUBBER_VERSION, source, SCRUBBER_VERSION)).fetchone()[0]
        return {"kept": kept, "dropped": sum(reasons.values()), "dropped_by_reason": reasons,
                "oldest_ms": lo, "newest_ms": hi, "stale": stale}

    def dropped_reasons(self, sources: Sequence[str]) -> Dict[str, int]:
        """Dropped counts by reason code across these sources."""
        sources = list(sources)
        if not sources:
            return {}
        with self._lock:
            return dict(self._db.execute(
                f"SELECT reason, count(*) FROM dropped WHERE source IN ({self._marks(sources)}) GROUP BY reason",
                sources).fetchall())
