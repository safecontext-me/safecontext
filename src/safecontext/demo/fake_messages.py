"""A synthetic Messages database (same tables and columns as ~/Library/Messages/chat.db)
for demo mode and tests, plus a stand-in for the Full Disk Access check.

Nothing here reads the real Messages database: the file is built from the
made-up conversations below, in a folder the caller chooses.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..readers.imessage import AccessState, AccessStatus, check_access

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT UNIQUE, id TEXT NOT NULL, service TEXT NOT NULL);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, style INTEGER,
  chat_identifier TEXT, service_name TEXT, display_name TEXT);
CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, text TEXT,
  attributedBody BLOB, handle_id INTEGER DEFAULT 0, service TEXT, date INTEGER, is_from_me INTEGER DEFAULT 0,
  cache_has_attachments INTEGER DEFAULT 0, item_type INTEGER DEFAULT 0, associated_message_guid TEXT,
  associated_message_type INTEGER DEFAULT 0, associated_message_emoji TEXT, thread_originator_guid TEXT,
  date_edited INTEGER DEFAULT 0, date_retracted INTEGER DEFAULT 0, is_read INTEGER DEFAULT 0);
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, filename TEXT,
  transfer_name TEXT, mime_type TEXT, uti TEXT);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER, UNIQUE(chat_id, handle_id));
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER, PRIMARY KEY (chat_id, message_id));
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER, UNIQUE(message_id, attachment_id));
"""

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# handle -> name, for the demo's stand-in Contacts
DEMO_CONTACTS: Dict[str, str] = {
    "+14155550134": "Jamie Rivera",
    "taylor@example.org": "Taylor Wu",
    "+14155550188": "Sam Patel",
}

# (chat key, sender handle or None for me, text, attachment file name or None)
Msg = Tuple[str, Optional[str], str, Optional[str]]

DEMO_CHATS: Dict[str, Tuple[str, Sequence[str], Optional[str]]] = {
    # key: (service, participants, group name)
    "jamie": ("iMessage", ("+14155550134",), None),
    "climb": ("iMessage", ("+14155550134", "taylor@example.org", "+14155550188"), "Saturday climbing"),
    "bank": ("SMS", ("72975",), None),
}

DEMO_MESSAGES: List[Msg] = [
    ("jamie", "+14155550134", "Dinner Saturday? I can book Nopa for 7.", None),
    ("jamie", None, "Yes! Book it for 4, I'll bring Taylor and Sam.", None),
    ("jamie", "+14155550134", "Done, under Rivera. Venmo me after, my card is 4111 1111 1111 1111 lol", None),
    ("climb", "taylor@example.org", "Who's in for Mission Cliffs on Saturday morning?", None),
    ("climb", "+14155550188", "Me. Leaving at 9 from the Mission.", None),
    ("climb", None, "In. Here's the route list", "routes.pdf"),
    ("bank", "72975", "Your verification code is 482913. Don't share it with anyone.", None),
    ("jamie", "+14155550134", "Running 10 min late, grab a table?", None),
]
# Positions (1-based) in DEMO_MESSAGES that Messages still shows as unread.
DEMO_UNREAD: Tuple[int, ...] = (5, 8)


def apple_ns(dt: datetime) -> int:
    return int((dt - APPLE_EPOCH).total_seconds()) * 1_000_000_000


def make_messages_db(path: Path, messages: Sequence[Msg] = DEMO_MESSAGES,
                     chats: Dict[str, Tuple[str, Sequence[str], Optional[str]]] = DEMO_CHATS,
                     start: Optional[datetime] = None, unread: Sequence[int] = ()) -> Path:
    """Write a Messages-style database at `path` (replacing any file there). One minute between messages.
    Incoming messages are read except those at the 1-based positions in `unread`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    t0 = start or (datetime.now(timezone.utc) - timedelta(days=2))
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        handles: Dict[str, int] = {}
        chat_ids: Dict[str, int] = {}
        for key, (service, people, group) in chats.items():
            for h in people:
                if h not in handles:
                    handles[h] = conn.execute("INSERT INTO handle (id, service) VALUES (?, ?)", (h, service)).lastrowid
            is_group = len(people) > 1
            ident = f"chat-{key}" if is_group else people[0]
            chat_ids[key] = conn.execute(
                "INSERT INTO chat (guid, style, chat_identifier, service_name, display_name) VALUES (?,?,?,?,?)",
                (f"{service};{'+' if is_group else '-'};{ident}", 43 if is_group else 45, ident, service,
                 group or "")).lastrowid
            for h in people:
                conn.execute("INSERT INTO chat_handle_join VALUES (?, ?)", (chat_ids[key], handles[h]))
        for i, (key, sender, text, attachment) in enumerate(messages, start=1):
            service = chats[key][0]
            rid = conn.execute(
                "INSERT INTO message (guid, text, handle_id, service, date, is_from_me, cache_has_attachments, "
                "is_read) VALUES (?,?,?,?,?,?,?,?)",
                (f"DEMO-{key}-{i}", text, handles.get(sender or "", 0), service,
                 apple_ns(t0 + timedelta(minutes=i)), 0 if sender else 1, 1 if attachment else 0,
                 0 if sender and i in unread else 1)).lastrowid
            conn.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat_ids[key], rid))
            if attachment:
                aid = conn.execute(
                    "INSERT INTO attachment (guid, filename, transfer_name, mime_type, uti) VALUES (?,?,?,?,?)",
                    (f"ATT-{i}", f"~/Library/Messages/Attachments/{attachment}", attachment,
                     "application/pdf", "com.adobe.pdf")).lastrowid
                conn.execute("INSERT INTO message_attachment_join VALUES (?, ?)", (rid, aid))
        conn.commit()
    finally:
        conn.close()
    return path


def add_message(path: Path, chat_identifier: str, sender: Optional[str], text: str) -> None:
    """Append one message to a database made by make_messages_db (for incremental-sync checks).
    `chat_identifier` is the other person's handle, or "chat-<key>" for a group."""
    conn = sqlite3.connect(path)
    try:
        chat_rowid = conn.execute("SELECT ROWID FROM chat WHERE chat_identifier=?", (chat_identifier,)).fetchone()
        handle = conn.execute("SELECT ROWID FROM handle WHERE id=?", (sender,)).fetchone() if sender else None
        n = conn.execute("SELECT count(*) FROM message").fetchone()[0] + 1
        rid = conn.execute(
            "INSERT INTO message (guid, text, handle_id, service, date, is_from_me) VALUES (?,?,?,?,?,?)",
            (f"DEMO-added-{n}", text, handle[0] if handle else 0, "iMessage",
             apple_ns(datetime.now(timezone.utc)), 0 if sender else 1)).lastrowid
        conn.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat_rowid[0], rid))
        conn.commit()
    finally:
        conn.close()


class FakeFullDiskAccess:
    """Stands in for macOS Full Disk Access in demo mode and tests: starts off;
    `open_settings` (the button) turns it on, as if the user had flipped the switch."""

    def __init__(self, granted: bool = False) -> None:
        self.granted = granted
        self.settings_opened = 0

    def check(self, db_path=None) -> AccessStatus:
        if not self.granted:
            return AccessStatus(AccessState.NO_PERMISSION, "No Full Disk Access (demo).", str(db_path or ""))
        return check_access(db_path)

    def open_settings(self) -> bool:
        self.settings_opened += 1
        self.granted = True
        return True


def demo_names() -> Callable[[str], Optional[str]]:
    return DEMO_CONTACTS.get


def demo_imessage_options(folder: Path, access: Optional[FakeFullDiskAccess] = None) -> Dict[str, object]:
    """IMessageSource options for demo mode: a synthetic chat.db under `folder`, fake access and contacts."""
    db = make_messages_db(Path(folder) / "chat.db", unread=DEMO_UNREAD)
    access = access or FakeFullDiskAccess()
    return {"db_path": db, "access_fn": access.check, "settings_fn": access.open_settings,
            "names_fn": demo_names}
