"""Read-only reader for the macOS Messages database (iMessage, SMS, RCS).

Nothing here writes to, locks or copies out of the Mac: the live database is
opened with SQLite's ``mode=ro`` URI flag plus ``PRAGMA query_only``, and only
when that fails is it copied to a private temporary folder (0700, deleted on
close). Attachment *contents* are never opened; only their names and types are
read from the database.

What this module relies on
--------------------------
Written and checked against macOS 14 Sonoma, 15 Sequoia and 26 Tahoe; the
core tables have been stable since macOS 10.13 High Sierra. Columns that newer
releases added are optional: when a column is missing its value is treated as
NULL, so older databases still read.

``~/Library/Messages/chat.db`` (WAL mode; ``chat.db-wal`` / ``-shm`` beside it)

* ``message``: ``ROWID`` (incremental cursor), ``guid``, ``text``,
  ``attributedBody`` (typedstream-archived NSAttributedString; since macOS 13
  Ventura this is often the *only* copy of the text), ``handle_id``,
  ``service`` ("iMessage", "SMS", "RCS" since macOS 15), ``date``
  (nanoseconds since 2001-01-01 UTC since 10.13; seconds before),
  ``is_from_me``, ``cache_has_attachments``.
  Optional: ``is_read`` (1 once read on any of the user's devices; only
  meaningful for incoming messages), ``item_type`` (non-zero = group event such as a rename),
  ``thread_originator_guid`` (inline reply, macOS 11+),
  ``associated_message_guid`` / ``associated_message_type`` (tapbacks:
  2000-2007 add, 3000-3007 remove), ``associated_message_emoji`` (custom
  emoji tapback, macOS 14+), ``date_edited`` / ``date_retracted`` (macOS 13+).
* ``handle``: ``ROWID``, ``id`` (E.164 phone number, email, or a short code /
  alphanumeric sender for SMS).
* ``chat``: ``ROWID``, ``guid``, ``chat_identifier``, ``service_name``,
  ``display_name``, ``style`` (43 = group, 45 = one-to-one).
* ``chat_handle_join`` (``chat_id``, ``handle_id``),
  ``chat_message_join`` (``chat_id``, ``message_id``).
* ``attachment`` (``ROWID``, ``filename``, ``transfer_name``, ``mime_type``,
  ``uti``) and ``message_attachment_join`` (``message_id``, ``attachment_id``).

Permission
----------
Reading needs Full Disk Access for the *responsible* app: SafeContext.app when
packaged, the terminal when run from a shell, or the app that launched the MCP
server. See :func:`check_access` and :func:`open_settings`.
"""
from __future__ import annotations

import enum
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote

__all__ = [
    "AccessState",
    "AccessStatus",
    "Attachment",
    "Conversation",
    "Message",
    "MessagesAccessError",
    "MessagesReader",
    "Reaction",
    "SETTINGS_PATH",
    "SETTINGS_URL",
    "check_access",
    "decode_attributed_body",
    "default_db_path",
    "open_settings",
]

SETTINGS_PATH = "System Settings → Privacy & Security → Full Disk Access"
SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
CHAT_STYLE_GROUP = 43

# associated_message_type → tapback name. 3000-range values remove the 2000 one.
_TAPBACKS = {
    0: "love",
    1: "like",
    2: "dislike",
    3: "laugh",
    4: "emphasize",
    5: "question",
    6: "emoji",
    7: "sticker",
}
_TAPBACK_EMOJI = {
    "love": "❤️",
    "like": "👍",
    "dislike": "👎",
    "laugh": "😂",
    "emphasize": "‼️",
    "question": "❓",
}
# Link previews and app bubbles store their payload as an "attachment"; it is
# not something the user sent, so it is not reported.
_HIDDEN_ATTACHMENT_SUFFIX = ".pluginPayloadAttachment"
_OBJECT_REPLACEMENT = "￼"  # stands in for an inline attachment in the text


def default_db_path() -> Path:
    return Path.home() / "Library" / "Messages" / "chat.db"


# --------------------------------------------------------------------------
# Data classes


@dataclass(frozen=True)
class Conversation:
    rowid: int
    chat_id: str  # chat.guid, e.g. "iMessage;-;+15551234567" or "any;+;chat123…"
    identifier: str  # chat.chat_identifier: the other handle, or a group id
    display_name: Optional[str]  # set for named group chats only
    service: Optional[str]
    is_group: bool
    participants: Tuple[str, ...]  # handle ids (phone numbers / emails), not including me


@dataclass(frozen=True)
class Attachment:
    name: Optional[str]  # file name only; never a path, never the contents
    mime_type: Optional[str]
    uti: Optional[str]


@dataclass(frozen=True)
class Reaction:
    kind: str  # love, like, dislike, laugh, emphasize, question, emoji, sticker
    emoji: Optional[str]
    removed: bool  # True when this event takes an earlier tapback away
    target_guid: Optional[str]  # guid of the message reacted to


@dataclass(frozen=True)
class Message:
    rowid: int  # incremental cursor: larger means newer in this database
    guid: str
    chat_id: Optional[str]  # Conversation.chat_id
    chat_rowid: Optional[int]
    sender: Optional[str]  # handle id; None when is_from_me
    is_from_me: bool
    timestamp: Optional[datetime]  # UTC
    text: str
    service: Optional[str]  # "iMessage", "SMS", "RCS", or what the database says
    reply_to: Optional[str]  # guid of the message this replies to (inline reply)
    has_attachments: bool
    attachments: Tuple[Attachment, ...] = ()
    edited: bool = False
    reaction: Optional[Reaction] = None  # set only with include_reactions=True
    is_read: bool = True  # Messages' own read state; always True for my own messages


# --------------------------------------------------------------------------
# Permission


class AccessState(str, enum.Enum):
    OK = "ok"
    NO_PERMISSION = "no_permission"  # Full Disk Access not granted
    NO_DATA = "no_data"  # Messages has never been used on this Mac / account
    ERROR = "error"  # readable but not a Messages database, or another OS error


@dataclass(frozen=True)
class AccessStatus:
    state: AccessState
    message: str
    path: str
    settings_path: str = SETTINGS_PATH
    settings_url: str = SETTINGS_URL

    @property
    def ok(self) -> bool:
        return self.state is AccessState.OK


class MessagesAccessError(RuntimeError):
    def __init__(self, status: AccessStatus):
        super().__init__(status.message)
        self.status = status


def check_access(db_path: Optional[os.PathLike] = None) -> AccessStatus:
    """Tell "no permission" apart from "no Messages data", reading 16 bytes at most.

    Without Full Disk Access macOS still answers ``stat`` for files under
    ~/Library/Messages but refuses ``open`` with EPERM, so existence and
    readability can be checked separately.
    """
    path = Path(db_path) if db_path is not None else default_db_path()
    p = str(path)
    if not path.exists():
        if path.parent.exists():
            try:
                os.listdir(path.parent)
            except PermissionError:
                return AccessStatus(AccessState.NO_PERMISSION, _no_permission_msg(), p)
        return AccessStatus(
            AccessState.NO_DATA,
            "No Messages data on this Mac. Sign in to Messages (and turn on "
            "Messages in iCloud to bring in older history), then try again.",
            p,
        )
    try:
        with open(path, "rb") as f:
            header = f.read(16)
    except PermissionError:
        return AccessStatus(AccessState.NO_PERMISSION, _no_permission_msg(), p)
    except OSError as e:
        return AccessStatus(AccessState.ERROR, f"Could not read the Messages database: {e.strerror}", p)
    if header != b"SQLite format 3\x00":
        return AccessStatus(AccessState.ERROR, "The Messages database is not in the expected format.", p)
    return AccessStatus(AccessState.OK, "Messages can be read.", p)


def _no_permission_msg() -> str:
    return (
        "SafeContext needs Full Disk Access to read Messages. Open "
        f"{SETTINGS_PATH}, turn on SafeContext (add it with + if it is not "
        "listed), then quit and reopen SafeContext."
    )


def open_settings() -> bool:
    """Open the Full Disk Access pane of System Settings. Returns False on failure."""
    try:
        return subprocess.run(["open", SETTINGS_URL], check=False, capture_output=True).returncode == 0
    except OSError:
        return False


# --------------------------------------------------------------------------
# attributedBody decoding


_STRING_CLASS = re.compile(rb"NS(?:Mutable)?String")


def decode_attributed_body(blob: Optional[bytes]) -> Optional[str]:
    """Return the plain text of an archived NSAttributedString, or None.

    Messages stores it as a NeXTSTEP *typedstream* (``\\x04\\x0bstreamtyped``).
    The first object is the NSAttributedString; its first field is the
    NSString, archived as the class name, then the type code ``+`` (written as
    ``\\x84\\x01+`` the first time it appears), then the UTF-8 byte length, then
    the bytes. Lengths use typedstream integer tags: a byte below 0x80 is the
    value, 0x81 precedes a little-endian int16, 0x82 an int32.

    NSKeyedArchiver binary plists are accepted too, for robustness.
    """
    if not blob:
        return None
    blob = bytes(blob)
    try:
        if blob.startswith(b"bplist"):
            return _decode_keyed_archive(blob)
        return _decode_typedstream(blob)
    except Exception:  # malformed blob: never let one message stop a sync
        return None


def _decode_typedstream(blob: bytes) -> Optional[str]:
    if b"streamtyped" not in blob[:16]:
        return None
    m = _STRING_CLASS.search(blob)
    if not m:
        return None
    marker = blob.find(b"\x84\x01+", m.end(), m.end() + 64)
    if marker < 0:
        return None
    i = marker + 3
    tag = blob[i]
    if tag < 0x80:
        length, i = tag, i + 1
    elif tag == 0x81:
        length, i = int.from_bytes(blob[i + 1 : i + 3], "little"), i + 3
    elif tag == 0x82:
        length, i = int.from_bytes(blob[i + 1 : i + 5], "little"), i + 5
    else:
        return None
    if i + length > len(blob):
        return None
    return blob[i : i + length].decode("utf-8", errors="replace")


def _decode_keyed_archive(blob: bytes) -> Optional[str]:
    archive = plistlib.loads(blob)
    objects = archive.get("$objects") or []

    def resolve(v):
        if isinstance(v, plistlib.UID):
            v = objects[v.data]
        if isinstance(v, dict) and "NS.string" in v:
            v = resolve(v["NS.string"])
        return v if isinstance(v, str) else None

    for obj in objects:
        if isinstance(obj, dict) and "NSString" in obj:
            return resolve(obj["NSString"])
    return None


def _clean_text(text: Optional[str]) -> str:
    if not text:
        return ""
    return text.replace(_OBJECT_REPLACEMENT, "").strip()


# --------------------------------------------------------------------------
# Reader


def _apple_time(value) -> Optional[datetime]:
    if not value:
        return None
    v = int(value)
    # Nanoseconds since macOS 10.13; seconds before. 1e11 s is ~3170 AD.
    seconds = v / 1_000_000_000 if abs(v) > 100_000_000_000 else float(v)
    return APPLE_EPOCH + timedelta(seconds=seconds)


def _strip_assoc_prefix(guid: Optional[str]) -> Optional[str]:
    # "p:0/GUID" (part 0 of a message) or "bp:GUID" (balloon) → "GUID"
    if not guid:
        return None
    if "/" in guid and guid.split("/", 1)[0].startswith("p:"):
        return guid.split("/", 1)[1]
    if guid.startswith("bp:"):
        return guid[3:]
    return guid


def _reaction(atype: int, emoji: Optional[str], target: Optional[str]) -> Optional[Reaction]:
    if 2000 <= atype <= 2007:
        removed = False
    elif 3000 <= atype <= 3007:
        removed = True
    else:
        return None
    kind = _TAPBACKS[atype % 1000]
    return Reaction(
        kind=kind,
        emoji=emoji if kind == "emoji" else _TAPBACK_EMOJI.get(kind),
        removed=removed,
        target_guid=_strip_assoc_prefix(target),
    )


class MessagesReader:
    """Read conversations and messages from chat.db without changing it.

    Usage::

        status = check_access()
        if not status.ok: ...            # show status.message, offer open_settings()
        with MessagesReader() as r:
            end = r.max_rowid()
            for msg in r.messages(since_rowid=cursor, until_rowid=end):
                ...
            cursor = end                 # next time, start after this
    """

    BATCH = 500

    def __init__(self, db_path: Optional[os.PathLike] = None, *, snapshot: bool = False):
        self.path = Path(db_path) if db_path is not None else default_db_path()
        status = check_access(self.path)
        if not status.ok:
            raise MessagesAccessError(status)
        self._tmpdir: Optional[str] = None
        self.conn = self._open_snapshot() if snapshot else self._open_live()
        self.conn.row_factory = sqlite3.Row
        self._cols = {t: self._columns(t) for t in ("message", "chat", "handle", "attachment")}
        self._chats: Optional[Dict[int, str]] = None

    # -- connection -------------------------------------------------------

    def _open_live(self) -> sqlite3.Connection:
        uri = f"file:{quote(str(self.path))}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            conn.execute("PRAGMA query_only = 1")
            conn.execute("SELECT 1 FROM message LIMIT 1").fetchall()
            return conn
        except sqlite3.OperationalError:
            # e.g. the WAL index can't be opened read-only: fall back to a copy.
            return self._open_snapshot()

    def _open_snapshot(self) -> sqlite3.Connection:
        self._tmpdir = tempfile.mkdtemp(prefix="safecontext-msgs-")  # 0700
        dest = Path(self._tmpdir) / "chat.db"
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self.path) + suffix)
            if src.exists():
                shutil.copyfile(src, str(dest) + suffix)
                os.chmod(str(dest) + suffix, 0o600)
        conn = sqlite3.connect(str(dest), check_same_thread=False)
        conn.execute("PRAGMA query_only = 1")
        return conn

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            if self._tmpdir:
                shutil.rmtree(self._tmpdir, ignore_errors=True)
                self._tmpdir = None

    def __enter__(self) -> "MessagesReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _columns(self, table: str) -> set:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _col(self, table: str, alias: str, column: str, default: str = "NULL") -> str:
        if column in self._cols[table]:
            return f"{alias}.{column} AS {column}"
        return f"{default} AS {column}"

    # -- queries ----------------------------------------------------------

    def max_rowid(self) -> int:
        return self.conn.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]

    def unread_guids(self) -> List[str]:
        """guids of incoming messages Messages still shows as unread (read state syncs across the
        user's devices). Only reads; nothing here can mark anything read."""
        if "is_read" not in self._cols["message"]:
            return []
        return [r[0] for r in self.conn.execute(
            "SELECT guid FROM message WHERE is_read = 0 AND is_from_me = 0")]

    def conversations(self) -> List[Conversation]:
        c = lambda col: self._col("chat", "c", col)  # noqa: E731
        rows = self.conn.execute(
            f"""SELECT c.ROWID AS rowid, c.guid AS guid, {c('chat_identifier')},
                       {c('display_name')}, {c('service_name')}, {c('style')}
                FROM chat c ORDER BY c.ROWID"""
        ).fetchall()
        members: Dict[int, List[str]] = {}
        for chat_id, handle in self.conn.execute(
            """SELECT chj.chat_id, h.id FROM chat_handle_join chj
               JOIN handle h ON h.ROWID = chj.handle_id ORDER BY chj.chat_id, h.ROWID"""
        ):
            members.setdefault(chat_id, []).append(handle)
        out = []
        for r in rows:
            people = tuple(members.get(r["rowid"], ()))
            style = r["style"]
            out.append(
                Conversation(
                    rowid=r["rowid"],
                    chat_id=r["guid"],
                    identifier=r["chat_identifier"] or "",
                    display_name=r["display_name"] or None,
                    service=r["service_name"],
                    is_group=(style == CHAT_STYLE_GROUP) if style is not None else len(people) > 1,
                    participants=people,
                )
            )
        return out

    def _chat_guids(self) -> Dict[int, str]:
        if self._chats is None:
            self._chats = dict(self.conn.execute("SELECT ROWID, guid FROM chat"))
        return self._chats

    def messages(
        self,
        since_rowid: int = 0,
        *,
        until_rowid: Optional[int] = None,
        include_reactions: bool = False,
        limit: Optional[int] = None,
    ) -> Iterator[Message]:
        """Yield messages with ``since_rowid < ROWID <= until_rowid``, oldest first.

        Skipped: group events (renames, joins), messages with no text and no
        attachments (e.g. unsent ones), and tapbacks unless
        ``include_reactions`` is set, in which case each is a ``Message`` with
        ``reaction`` filled in and empty ``text``.
        """
        m = lambda col, d="NULL": self._col("message", "m", col, d)  # noqa: E731
        sql = f"""
            SELECT m.ROWID AS rowid, m.guid AS guid, {m('text')}, {m('attributedBody')},
                   {m('service')}, m.date AS date, m.is_from_me AS is_from_me, {m('is_read', '1')},
                   {m('item_type', '0')}, {m('cache_has_attachments', '0')},
                   {m('thread_originator_guid')}, {m('associated_message_guid')},
                   {m('associated_message_type', '0')}, {m('associated_message_emoji')},
                   {m('date_edited', '0')}, {m('date_retracted', '0')},
                   h.id AS handle,
                   (SELECT MIN(cmj.chat_id) FROM chat_message_join cmj
                     WHERE cmj.message_id = m.ROWID) AS chat_rowid
            FROM message m LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE m.ROWID > ? AND m.ROWID <= ?
            ORDER BY m.ROWID
        """
        upper = until_rowid if until_rowid is not None else self.max_rowid()
        chats = self._chat_guids()
        yielded = 0
        cur = self.conn.execute(sql, (since_rowid, upper))
        while True:
            rows = cur.fetchmany(self.BATCH)
            if not rows:
                return
            attachments = self._attachments([r["rowid"] for r in rows if r["cache_has_attachments"]])
            for r in rows:
                msg = self._to_message(r, chats, attachments, include_reactions)
                if msg is None:
                    continue
                yield msg
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

    def _to_message(self, r, chats, attachments, include_reactions) -> Optional[Message]:
        if r["item_type"]:
            return None
        reaction = _reaction(r["associated_message_type"] or 0, r["associated_message_emoji"], r["associated_message_guid"])
        if reaction is None and (r["associated_message_type"] or 0) >= 1000:
            return None  # other associated items (e.g. sticker placements): not text
        if reaction is not None and not include_reactions:
            return None
        files = tuple(attachments.get(r["rowid"], ()))
        text = "" if reaction else _clean_text(r["text"] or decode_attributed_body(r["attributedBody"]))
        if reaction is None and not text and not files:
            return None
        chat_rowid = r["chat_rowid"]
        from_me = bool(r["is_from_me"])
        return Message(
            rowid=r["rowid"],
            guid=r["guid"],
            chat_id=chats.get(chat_rowid) if chat_rowid is not None else None,
            chat_rowid=chat_rowid,
            sender=None if from_me else r["handle"],
            is_from_me=from_me,
            timestamp=_apple_time(r["date"]),
            text=text,
            service=r["service"],
            reply_to=r["thread_originator_guid"] or None,
            has_attachments=bool(files),
            attachments=files,
            edited=bool(r["date_edited"]),
            reaction=reaction,
            is_read=from_me or bool(r["is_read"]),
        )

    def _attachments(self, rowids: Sequence[int]) -> Dict[int, List[Attachment]]:
        out: Dict[int, List[Attachment]] = {}
        if not rowids:
            return out
        a = lambda col: self._col("attachment", "a", col)  # noqa: E731
        marks = ",".join("?" * len(rowids))
        for r in self.conn.execute(
            f"""SELECT maj.message_id AS message_id, {a('transfer_name')}, {a('filename')},
                       {a('mime_type')}, {a('uti')}
                FROM message_attachment_join maj JOIN attachment a ON a.ROWID = maj.attachment_id
                WHERE maj.message_id IN ({marks}) ORDER BY maj.message_id, a.ROWID""",
            list(rowids),
        ):
            name = r["transfer_name"] or (os.path.basename(r["filename"]) if r["filename"] else None)
            if name and name.endswith(_HIDDEN_ATTACHMENT_SUFFIX):
                continue
            out.setdefault(r["message_id"], []).append(Attachment(name, r["mime_type"], r["uti"]))
        return out
