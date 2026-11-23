"""WhatsApp reader: the native Mac app's local database, plus "Export chat" files.

Read-only and on-device. Nothing here talks to the network or to WhatsApp's
servers; unofficial web-client libraries (which log in as the user and risk
account bans) are deliberately not used.

Research notes (checked 2026-11-23 on macOS 26 / Darwin 25.6, WhatsApp for Mac
26.45.75, bundle id ``net.whatsapp.WhatsApp``; the native Catalyst app, not the
old Electron "WhatsApp Desktop", whose IndexedDB store is not supported)
---------------------------------------------------------------------------

Where
    ``~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite``
    (+ ``-wal``/``-shm``). Same Core Data model as WhatsApp for iPhone. Siblings
    in that folder: ``ContactsV2.sqlite`` (address book: ``ZLID`` <-> ``ZWHATSAPPID``
    <-> ``ZFULLNAME``), ``LID.sqlite`` (``ZWAPHONENUMBERLIDPAIR``, often empty),
    ``Message/Media/<jid>/…`` (downloaded media; not read here).

Encryption
    None at rest beyond FileVault. Plain SQLite in WAL mode, readable while the
    app runs. ``Z_METADATA.Z_VERSION`` = 1.

Permission
    Another app's Group Container is covered by macOS "App Data" protection.
    A process without Full Disk Access gets ``PermissionError`` (or the one-off
    "would like to access data from other apps" prompt). Grant via
    System Settings > Privacy & Security > Full Disk Access
    (``x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles``).
    When running from source, the grant belongs to the host binary (Terminal,
    the Python interpreter, or the bundled SafeContext.app).

Schema subset used (Core Data: tables ``Z<ENTITY>``, ``Z_PK`` row ids,
timestamps are seconds since 2001-01-01 UTC, i.e. add 978307200):
    ``ZWACHATSESSION``  one row per chat. ``ZCONTACTJID``, ``ZPARTNERNAME`` (chat
        title / contact name), ``ZSESSIONTYPE`` (0 direct, 1 group, 2 broadcast
        list, 3 status, 4 community/announcement group), ``ZARCHIVED``,
        ``ZREMOVED``, ``ZHIDDEN``, ``ZLASTMESSAGEDATE``, ``ZGROUPINFO``.
    ``ZWAMESSAGE``  ``ZCHATSESSION``, ``ZISFROMME``, ``ZFROMJID`` (chat jid for
        groups, contact jid for 1:1 incoming, NULL for outgoing), ``ZTOJID``,
        ``ZGROUPMEMBER`` (-> sender in groups), ``ZMESSAGEDATE``, ``ZSENTDATE``,
        ``ZTEXT``, ``ZMESSAGETYPE`` (see ``_MESSAGE_KINDS``), ``ZGROUPEVENTTYPE``,
        ``ZSTANZAID`` (WhatsApp message id), ``ZSTARRED``, ``ZMEDIAITEM``.
        NOTE: on the Mac ``ZPUSHNAME`` is a base64 blob, not a display name;
        push names live in ``ZWAPROFILEPUSHNAME``.
    ``ZWAMEDIAITEM``  one per message on the Mac (not only media). ``ZTITLE``
        (caption, document file name, link title), ``ZMEDIALOCALPATH`` (only
        when downloaded), ``ZFILESIZE``, ``ZVCARDNAME``, ``ZMETADATA`` (protobuf).
        Replies: ``ZMETADATA`` field 5 = quoted message's stanza id, field 6 =
        quoted sender jid (6,480 replies seen; 99.6% resolve to a local message).
    ``ZWAGROUPMEMBER``  ``ZCHATSESSION``, ``ZMEMBERJID``, ``ZCONTACTNAME``,
        ``ZFIRSTNAME``, ``ZISADMIN``, ``ZISACTIVE``.
    ``ZWAPROFILEPUSHNAME``  ``ZJID`` -> ``ZPUSHNAME`` (the name a user set).
    JIDs: ``<phone>@s.whatsapp.net`` (user), ``<n>@lid`` (privacy "linked id",
    no phone; most group members and newer chats), ``<id>@g.us`` (group),
    ``status@broadcast``. A ``:<device>`` suffix may follow the user part.

History completeness
    The Mac app is a linked device. It only holds what WhatsApp synced at link
    time plus everything since. On the machine checked: 89k messages, 2018-2026,
    but only ~550 before 2025; the bulk is from the months since linking. Older
    history lives on the phone; use the export fallback below for it.

Fallback: "Export chat" files (``.txt`` or ``.zip`` with ``_chat.txt``), from
iOS (``[date, time] Name: text``) and Android (``date, time - Name: text``),
12/24-hour clocks, D/M/Y, M/D/Y and Y/M/D dates with ``/ . -`` separators,
multi-line messages, system lines and attachment markers. Export timestamps
are phone-local with no zone; pass ``tz`` (defaults to this Mac's zone).

Risks: the Core Data schema changes without notice (columns are probed, not
assumed, where it matters); message-type numbers are reverse-engineered; the
``@lid`` migration hides phone numbers; edits and deletions rewrite rows in
place, so a rowid cursor will not re-deliver an edited message.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import sqlite3
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path, PurePosixPath

__all__ = [
    "APPLE_EPOCH",
    "AccessStatus",
    "Attachment",
    "Conversation",
    "Member",
    "Message",
    "WhatsAppDesktopReader",
    "ExportChat",
    "check_access",
    "parse_export",
    "parse_export_text",
    "default_container",
]

APPLE_EPOCH = 978307200  # 2001-01-01T00:00:00Z as a Unix timestamp
GROUP_CONTAINER = "group.net.whatsapp.WhatsApp.shared"
APP_PATHS = ("/Applications/WhatsApp.app", "~/Applications/WhatsApp.app")
FULL_DISK_ACCESS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
)
FULL_DISK_ACCESS_PATH = "System Settings > Privacy & Security > Full Disk Access"

# ZMESSAGETYPE -> kind. Reverse-engineered; unknown values map to "other".
_MESSAGE_KINDS = {
    0: "text",
    1: "image",
    2: "video",
    3: "audio",
    4: "contact",
    5: "location",
    6: "system",
    7: "link",
    8: "document",
    10: "system",
    11: "gif",
    14: "deleted",
    15: "sticker",
    46: "poll",
}
_ATTACHMENT_KINDS = {"image", "video", "audio", "contact", "location", "document", "gif", "sticker"}
_SESSION_KINDS = {0: "direct", 1: "group", 2: "broadcast", 3: "status", 4: "group"}


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Attachment:
    kind: str  # image, video, audio, document, sticker, gif, contact, location, media
    name: str | None = None  # file name / document title / vCard name; never contents
    size: int | None = None


@dataclass(frozen=True)
class Member:
    jid: str
    phone: str | None
    name: str | None
    is_admin: bool = False


@dataclass(frozen=True)
class Conversation:
    id: str  # chat JID, or "export:<name>" for export files
    name: str | None
    kind: str  # direct, group, broadcast, status, other
    archived: bool = False
    last_message_at: datetime | None = None
    phone: str | None = None  # for direct chats when known
    members: tuple[Member, ...] = ()
    source: str = "whatsapp-desktop"


@dataclass(frozen=True)
class Message:
    id: str  # WhatsApp stanza id, or a stable hash for export lines
    rowid: int  # desktop: ZWAMESSAGE.Z_PK (the cursor); export: message index
    conversation_id: str
    sent_at: datetime  # timezone-aware UTC
    is_from_me: bool
    sender_jid: str | None
    sender_phone: str | None
    sender_name: str | None  # best display name (address book, then push name)
    sender_push_name: str | None
    text: str | None
    kind: str  # see _MESSAGE_KINDS, plus "media" for exports
    reply_to_id: str | None = None
    reply_to_sender_jid: str | None = None
    attachment: Attachment | None = None
    starred: bool = False
    raw_type: int | None = None
    source: str = "whatsapp-desktop"


@dataclass(frozen=True)
class AccessStatus:
    state: str  # "ok", "not_installed", "no_permission", "no_data"
    message: str
    db_path: Path | None = None
    settings_path: str | None = None
    settings_url: str | None = None

    @property
    def ok(self) -> bool:
        return self.state == "ok"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def default_container() -> Path:
    return Path.home() / "Library" / "Group Containers" / GROUP_CONTAINER


def apple_to_utc(value: float | int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value) + APPLE_EPOCH, tz=timezone.utc)


def jid_user(jid: str | None) -> str | None:
    if not jid or "@" not in jid:
        return None
    return jid.split("@", 1)[0].split(":", 1)[0]


def jid_to_phone(jid: str | None) -> str | None:
    """'15551234567@s.whatsapp.net' -> '+15551234567'. LIDs and groups -> None."""
    if not jid or not jid.endswith("@s.whatsapp.net"):
        return None
    user = jid_user(jid)
    return f"+{user}" if user and user.isdigit() else None


def _normalize_jid(jid: str | None) -> str | None:
    """Drop the ':<device>' suffix so lookups match."""
    if not jid or "@" not in jid:
        return jid
    user, server = jid.split("@", 1)
    return f"{user.split(':', 1)[0]}@{server}"


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if b < 0x80:
            return result, i
        if shift > 63:
            raise ValueError("varint too long")


def _quoted_from_metadata(blob: bytes | None) -> tuple[str | None, str | None]:
    """Return (quoted stanza id, quoted sender jid) from ZWAMEDIAITEM.ZMETADATA.

    Top-level protobuf fields 5 and 6. Anything malformed yields (None, None).
    """
    if not blob:
        return None, None
    stanza = sender = None
    i = 0
    try:
        while i < len(blob):
            key, i = _varint(blob, i)
            field_no, wire = key >> 3, key & 7
            if wire == 0:
                _, i = _varint(blob, i)
            elif wire == 2:
                length, i = _varint(blob, i)
                value = blob[i : i + length]
                i += length
                if field_no == 5:
                    stanza = value.decode("utf-8", "replace") or None
                elif field_no == 6:
                    sender = value.decode("utf-8", "replace") or None
            elif wire == 1:
                i += 8
            elif wire == 5:
                i += 4
            else:
                break
    except ValueError:
        return None, None
    return stanza, sender


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


# --------------------------------------------------------------------------
# Access check
# --------------------------------------------------------------------------


def check_access(container: Path | str | None = None) -> AccessStatus:
    """Tell "not installed", "no permission" and "no data" apart. Reads no messages."""
    root = Path(container).expanduser() if container else default_container()
    db = root / "ChatStorage.sqlite"
    app_installed = any(Path(p).expanduser().exists() for p in APP_PATHS)

    try:
        root_exists = root.is_dir()
    except PermissionError:
        root_exists = True
    if not root_exists:
        if app_installed:
            return AccessStatus(
                "no_data",
                "WhatsApp is installed but has not been opened or linked to a phone yet.",
                db,
            )
        return AccessStatus(
            "not_installed",
            "WhatsApp for Mac is not installed. Install it from the Mac App Store, "
            "or import an exported chat (.txt/.zip) instead.",
            db,
        )

    try:
        os.listdir(root)
        with open(db, "rb") as fh:
            fh.read(16)
    except FileNotFoundError:
        return AccessStatus(
            "no_data", "WhatsApp is installed but has no chat database yet. Open it and link your phone.", db
        )
    except PermissionError:
        return AccessStatus(
            "no_permission",
            f"SafeContext needs Full Disk Access to read WhatsApp's local database. "
            f"Open {FULL_DISK_ACCESS_PATH}, turn on SafeContext, then try again.",
            db,
            FULL_DISK_ACCESS_PATH,
            FULL_DISK_ACCESS_URL,
        )

    try:
        conn = _open_readonly(db)
        try:
            (count,) = conn.execute("SELECT COUNT(*) FROM ZWAMESSAGE").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        if "authoriz" in str(exc).lower() or "permission" in str(exc).lower():
            return AccessStatus(
                "no_permission", f"Permission denied opening the database ({exc}).", db,
                FULL_DISK_ACCESS_PATH, FULL_DISK_ACCESS_URL,
            )
        return AccessStatus("no_data", f"The WhatsApp database could not be read ({exc}).", db)
    if not count:
        return AccessStatus("no_data", "WhatsApp is set up but has no messages on this Mac yet.", db)
    return AccessStatus("ok", f"WhatsApp database readable ({count} messages).", db)


def _open_readonly(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)


# --------------------------------------------------------------------------
# Desktop reader
# --------------------------------------------------------------------------


class WhatsAppDesktopReader:
    """Reads the native WhatsApp for Mac database, read-only.

    By default the database is copied into memory with SQLite's backup API
    (a consistent snapshot; nothing written to disk, no lock held on
    WhatsApp's file while iterating). Pass ``snapshot=False`` to read live.

    Incremental sync: ``messages(after_rowid=n)`` yields rows with
    ``ZWAMESSAGE.Z_PK > n`` in Z_PK order; store the last ``Message.rowid``.
    Z_PK grows on insert, so history that syncs in late (old dates) is still
    picked up. Edits/deletions update rows in place and are not re-delivered.
    """

    def __init__(self, container: Path | str | None = None, *, snapshot: bool = True):
        self.container = Path(container).expanduser() if container else default_container()
        self.db_path = self.container / "ChatStorage.sqlite"
        self.snapshot = snapshot
        self._conn: sqlite3.Connection | None = None
        self._names: dict[str, str] | None = None
        self._push: dict[str, str] | None = None
        self._lid_phone: dict[str, str] | None = None

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> WhatsAppDesktopReader:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> sqlite3.Connection:
        if self._conn is None:
            src = _open_readonly(self.db_path)
            if self.snapshot:
                mem = sqlite3.connect(":memory:")
                try:
                    src.backup(mem)
                finally:
                    src.close()
                self._conn = mem
            else:
                self._conn = src
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        return self.open()

    # -- schema probing --------------------------------------------------

    def _columns(self, table: str) -> set[str]:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _col(self, cols: set[str], table_alias: str, name: str) -> str:
        return f"{table_alias}.{name}" if name in cols else "NULL"

    # -- name / phone lookups ---------------------------------------------

    def _load_lookups(self) -> None:
        if self._push is not None:
            return
        self._push = {}
        if "ZWAPROFILEPUSHNAME" in self._tables():
            for jid, name in self.conn.execute("SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME"):
                if jid and _clean(name):
                    self._push[_normalize_jid(jid)] = _clean(name)
        self._names, self._lid_phone = {}, {}
        contacts = self.container / "ContactsV2.sqlite"
        if contacts.exists():
            try:
                c = _open_readonly(contacts)
                try:
                    cols = {r[1] for r in c.execute("PRAGMA table_info(ZWAADDRESSBOOKCONTACT)")}
                    if {"ZFULLNAME", "ZWHATSAPPID"} <= cols:
                        lid = "ZLID" if "ZLID" in cols else "NULL"
                        for full, wa, l in c.execute(
                            f"SELECT ZFULLNAME, ZWHATSAPPID, {lid} FROM ZWAADDRESSBOOKCONTACT"
                        ):
                            name = _clean(full)
                            for j in (wa, l):
                                if j and name:
                                    self._names[_normalize_jid(j)] = name
                            if l and jid_to_phone(_normalize_jid(wa)):
                                self._lid_phone[_normalize_jid(l)] = jid_to_phone(_normalize_jid(wa))
                finally:
                    c.close()
            except sqlite3.DatabaseError:
                pass
        lid_db = self.container / "LID.sqlite"
        if lid_db.exists():
            try:
                c = _open_readonly(lid_db)
                try:
                    for l, phone in c.execute("SELECT ZLID, ZPHONENUMBER FROM ZWAPHONENUMBERLIDPAIR"):
                        if l and phone:
                            p = phone if "@" not in phone else jid_to_phone(_normalize_jid(phone))
                            if p:
                                self._lid_phone.setdefault(
                                    _normalize_jid(l if "@" in l else f"{l}@lid"),
                                    p if p.startswith("+") else f"+{p}",
                                )
                finally:
                    c.close()
            except sqlite3.DatabaseError:
                pass

    def _tables(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def phone_for(self, jid: str | None) -> str | None:
        jid = _normalize_jid(jid)
        self._load_lookups()
        return jid_to_phone(jid) or (self._lid_phone or {}).get(jid or "")

    def push_name_for(self, jid: str | None) -> str | None:
        self._load_lookups()
        return (self._push or {}).get(_normalize_jid(jid) or "")

    def contact_name_for(self, jid: str | None) -> str | None:
        self._load_lookups()
        return (self._names or {}).get(_normalize_jid(jid) or "")

    # -- conversations -----------------------------------------------------

    def conversations(self, *, include_hidden: bool = False) -> list[Conversation]:
        self._load_lookups()
        cols = self._columns("ZWACHATSESSION")
        where = []
        if not include_hidden:
            if "ZREMOVED" in cols:
                where.append("COALESCE(s.ZREMOVED, 0) = 0")
            if "ZHIDDEN" in cols:
                where.append("COALESCE(s.ZHIDDEN, 0) = 0")
        sql = (
            f"SELECT s.Z_PK, s.ZCONTACTJID, {self._col(cols, 's', 'ZPARTNERNAME')} AS name, "
            f"{self._col(cols, 's', 'ZSESSIONTYPE')} AS stype, "
            f"{self._col(cols, 's', 'ZARCHIVED')} AS archived, "
            f"{self._col(cols, 's', 'ZLASTMESSAGEDATE')} AS last "
            f"FROM ZWACHATSESSION s"
            + (f" WHERE {' AND '.join(where)}" if where else "")
            + " ORDER BY s.Z_PK"
        )
        members = self._members_by_session()
        out = []
        for r in self.conn.execute(sql):
            jid = r["ZCONTACTJID"]
            kind = _SESSION_KINDS.get(r["stype"], "other")
            if jid and jid.endswith("@g.us"):
                kind = "group"
            out.append(
                Conversation(
                    id=jid or f"session:{r['Z_PK']}",
                    name=_clean(r["name"]) or self.contact_name_for(jid) or self.push_name_for(jid),
                    kind=kind,
                    archived=bool(r["archived"]),
                    last_message_at=apple_to_utc(r["last"]),
                    phone=self.phone_for(jid) if kind == "direct" else None,
                    members=tuple(members.get(r["Z_PK"], ())),
                )
            )
        return out

    def _members_by_session(self) -> dict[int, list[Member]]:
        if "ZWAGROUPMEMBER" not in self._tables():
            return {}
        cols = self._columns("ZWAGROUPMEMBER")
        active = "WHERE COALESCE(g.ZISACTIVE, 1) = 1" if "ZISACTIVE" in cols else ""
        sql = (
            f"SELECT g.ZCHATSESSION, g.ZMEMBERJID, {self._col(cols, 'g', 'ZCONTACTNAME')} AS cname, "
            f"{self._col(cols, 'g', 'ZFIRSTNAME')} AS fname, {self._col(cols, 'g', 'ZISADMIN')} AS admin "
            f"FROM ZWAGROUPMEMBER g {active} ORDER BY g.Z_PK"
        )
        out: dict[int, list[Member]] = {}
        for sess, jid, cname, fname, admin in self.conn.execute(sql):
            if not jid:
                continue
            out.setdefault(sess, []).append(
                Member(
                    jid=jid,
                    phone=self.phone_for(jid),
                    name=self.contact_name_for(jid) or _clean(cname) or _clean(fname) or self.push_name_for(jid),
                    is_admin=bool(admin),
                )
            )
        return out

    # -- messages ------------------------------------------------------------

    def messages(
        self,
        *,
        after_rowid: int = 0,
        conversation_id: str | None = None,
        limit: int | None = None,
        include_system: bool = True,
    ) -> Iterator[Message]:
        """Yield messages with Z_PK > after_rowid, oldest insert first."""
        self._load_lookups()
        mcols = self._columns("ZWAMESSAGE")
        icols = self._columns("ZWAMEDIAITEM") if "ZWAMEDIAITEM" in self._tables() else set()
        join_members = "ZWAGROUPMEMBER" in self._tables() and "ZGROUPMEMBER" in mcols
        join_media = bool(icols) and "ZMEDIAITEM" in mcols
        c = lambda name: self._col(mcols, "m", name)  # noqa: E731
        i = lambda name: self._col(icols, "i", name) if join_media else "NULL"  # noqa: E731
        sql = f"""
            SELECT m.Z_PK, m.ZSTANZAID, m.ZTEXT, m.ZMESSAGEDATE, {c('ZSENTDATE')} AS sentdate,
                   m.ZISFROMME, m.ZFROMJID, {c('ZMESSAGETYPE')} AS mtype,
                   {c('ZSTARRED')} AS starred,
                   s.ZCONTACTJID AS chat_jid,
                   {'g.ZMEMBERJID' if join_members else 'NULL'} AS member_jid,
                   {'g.ZCONTACTNAME' if join_members else 'NULL'} AS member_name,
                   {i('ZTITLE')} AS title, {i('ZMEDIALOCALPATH')} AS localpath,
                   {i('ZFILESIZE')} AS filesize, {i('ZVCARDNAME')} AS vcardname,
                   {i('ZMETADATA')} AS metadata
            FROM ZWAMESSAGE m
            LEFT JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION
            {'LEFT JOIN ZWAGROUPMEMBER g ON g.Z_PK = m.ZGROUPMEMBER' if join_members else ''}
            {'LEFT JOIN ZWAMEDIAITEM i ON i.Z_PK = m.ZMEDIAITEM' if join_media else ''}
            WHERE m.Z_PK > ?
        """
        params: list = [after_rowid]
        if conversation_id:
            sql += " AND s.ZCONTACTJID = ?"
            params.append(conversation_id)
        sql += " ORDER BY m.Z_PK"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        for r in self.conn.execute(sql, params):
            msg = self._row_to_message(r)
            if msg is None or (not include_system and msg.kind == "system"):
                continue
            yield msg

    def latest_rowid(self) -> int:
        (n,) = self.conn.execute("SELECT COALESCE(MAX(Z_PK), 0) FROM ZWAMESSAGE").fetchone()
        return int(n)

    def _row_to_message(self, r: sqlite3.Row) -> Message | None:
        sent = apple_to_utc(r["ZMESSAGEDATE"]) or apple_to_utc(r["sentdate"])
        if sent is None:
            return None
        raw_type = r["mtype"]
        kind = _MESSAGE_KINDS.get(raw_type, "other") if raw_type is not None else "text"
        chat_jid = r["chat_jid"] or r["ZFROMJID"]
        from_me = bool(r["ZISFROMME"])
        if from_me:
            sender = None
        elif r["member_jid"]:
            sender = r["member_jid"]
        elif chat_jid and chat_jid.endswith("@g.us"):
            sender = None  # group system line with no member
        else:
            sender = r["ZFROMJID"] or chat_jid
        push = self.push_name_for(sender) if sender else None
        name = (
            (self.contact_name_for(sender) or _clean(r["member_name"]) or push) if sender else None
        )

        text = r["ZTEXT"]
        title = _clean(r["title"])
        attachment = None
        if kind in _ATTACHMENT_KINDS:
            if kind == "document":
                att_name = title or _basename(r["localpath"])
            elif kind == "contact":
                att_name = _clean(r["vcardname"])
            elif kind == "location":
                att_name = None  # coordinates deliberately not exported
            else:
                att_name = _basename(r["localpath"])
                if title and not text:
                    text = title  # caption
            size = r["filesize"] if isinstance(r["filesize"], int) and r["filesize"] > 0 else None
            attachment = Attachment(kind=kind, name=att_name, size=size)
        if kind == "deleted":
            text = None

        reply_id, reply_sender = _quoted_from_metadata(r["metadata"])
        return Message(
            id=r["ZSTANZAID"] or f"pk:{r['Z_PK']}",
            rowid=r["Z_PK"],
            conversation_id=chat_jid or "",
            sent_at=sent,
            is_from_me=from_me,
            sender_jid=sender,
            sender_phone=self.phone_for(sender) if sender else None,
            sender_name=name,
            sender_push_name=push,
            text=text if text else None,
            kind=kind,
            reply_to_id=reply_id,
            reply_to_sender_jid=reply_sender,
            attachment=attachment,
            starred=bool(r["starred"]),
            raw_type=raw_type,
        )


def _basename(path: str | None) -> str | None:
    if not path:
        return None
    return PurePosixPath(path).name or None


# --------------------------------------------------------------------------
# Export-file fallback
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportChat:
    conversation: Conversation
    messages: list[Message] = field(default_factory=list)


_LRM = "‎‏‪‫‬‭‮﻿"
_SPACE = r"[   ]"
_DATE = r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\.?"
_TIME = (
    r"\d{1,2}[:.]\d{2}(?:[:.]\d{2})?"
    rf"(?:{_SPACE}?(?:[AaPp]\.?{_SPACE}?[Mm]\.?|[AaPp]))?"
)
_IOS_HEADER = re.compile(rf"^\[(?P<date>{_DATE}),?{_SPACE}+(?P<time>{_TIME})\]{_SPACE}?(?P<rest>.*)$")
_ANDROID_HEADER = re.compile(
    rf"^(?P<date>{_DATE}),?{_SPACE}+(?P<time>{_TIME}){_SPACE}+[-–]{_SPACE}(?P<rest>.*)$"
)
_IOS_ATTACHED = re.compile(r"<attached:\s*(?P<name>[^>]+)>")
_ANDROID_ATTACHED = re.compile(r"^(?P<name>\S.*?\.\w{1,5}) \((?:file attached|archivo adjunto|Datei angehängt|fichier joint|arquivo anexado|file allegato)\)")
_MEDIA_OMITTED = re.compile(r"^<?(?:Media omitted|Medien ausgeschlossen|Multimedia omitido|Médias omis|Mídia oculta|Media omessi)>?$", re.I)
_IOS_OMITTED = {
    "image omitted": "image",
    "video omitted": "video",
    "audio omitted": "audio",
    "sticker omitted": "sticker",
    "gif omitted": "gif",
    "document omitted": "document",
    "contact card omitted": "contact",
}
_DELETED = {
    "this message was deleted",
    "you deleted this message",
    "this message was deleted.",
    "you deleted this message.",
}
_EDITED_SUFFIX = re.compile(r"\s*<This message was edited>\s*$")
_PHONE_LIKE = re.compile(r"^\+?[\d\s().-]{7,}$")


def _kind_for_filename(name: str) -> str:
    upper = name.upper()
    for token, kind in (("-PHOTO-", "image"), ("-VIDEO-", "video"), ("-AUDIO-", "audio"),
                        ("-STICKER-", "sticker"), ("-GIF-", "gif"), ("IMG-", "image"),
                        ("VID-", "video"), ("PTT-", "audio"), ("AUD-", "audio"), ("STK-", "sticker")):
        if token in upper:
            return kind
    ext = upper.rsplit(".", 1)[-1] if "." in upper else ""
    if ext in {"JPG", "JPEG", "PNG", "HEIC", "WEBP"}:
        return "sticker" if ext == "WEBP" else "image"
    if ext in {"MP4", "MOV", "3GP"}:
        return "video"
    if ext in {"OPUS", "M4A", "MP3", "AAC", "OGG"}:
        return "audio"
    if ext == "VCF":
        return "contact"
    return "document"


def _split_date(date: str) -> tuple[int, int, int]:
    parts = [int(p) for p in re.split(r"[./-]", date.rstrip(".")) if p]
    if len(parts) != 3:
        raise ValueError(date)
    return parts[0], parts[1], parts[2]


def _detect_date_order(dates: Iterable[str], twelve_hour: bool) -> str:
    """Return 'YMD', 'DMY' or 'MDY' for the whole file."""
    dmy_votes = mdy_votes = 0
    for d in dates:
        a, b, _c = _split_date(d)
        if a > 31:
            return "YMD"
        if a > 12:
            dmy_votes += 1
        elif b > 12:
            mdy_votes += 1
    if dmy_votes and not mdy_votes:
        return "DMY"
    if mdy_votes and not dmy_votes:
        return "MDY"
    return "MDY" if twelve_hour else "DMY"


def _parse_time(text: str) -> tuple[int, int, int]:
    t = text.replace(" ", " ").replace(" ", " ").strip()
    m = re.match(r"(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?\s*(.*)$", t)
    if not m:
        raise ValueError(text)
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    suffix = m.group(4).replace(".", "").replace(" ", "").lower()
    if suffix in ("pm", "p"):
        h = h % 12 + 12
    elif suffix in ("am", "a"):
        h = h % 12
    return h, mi, s


def _to_datetime(date: str, time_: str, order: str, tz: tzinfo) -> datetime:
    a, b, c = _split_date(date)
    if order == "YMD":
        y, mo, d = a, b, c
    elif order == "MDY":
        mo, d, y = a, b, c
    else:
        d, mo, y = a, b, c
    if y < 100:
        y += 2000
    h, mi, s = _parse_time(time_)
    local = datetime(y, mo, d, h, mi, s, tzinfo=tz)
    return local.astimezone(timezone.utc)


def _local_tz() -> tzinfo:
    return datetime.now().astimezone().tzinfo or timezone.utc


def _chat_name_from_filename(path: Path) -> str:
    stem = path.stem
    for prefix in ("WhatsApp Chat with ", "WhatsApp Chat - ", "WhatsApp-Chat mit ", "Chat de WhatsApp con "):
        if stem.startswith(prefix):
            return stem[len(prefix):].strip() or stem
    return stem


def parse_export(
    path: Path | str,
    *,
    tz: tzinfo | None = None,
    me: Iterable[str] = (),
    date_order: str | None = None,
    name: str | None = None,
) -> ExportChat:
    """Parse a WhatsApp "Export chat" .txt or .zip. Media in a zip is never extracted."""
    path = Path(path).expanduser()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            txts = [n for n in zf.namelist() if n.lower().endswith(".txt") and not n.startswith("__MACOSX")]
            if not txts:
                raise ValueError(f"No chat .txt inside {path.name}")
            member = "_chat.txt" if "_chat.txt" in txts else sorted(txts, key=len)[0]
            info = zf.getinfo(member)
            if info.file_size > 512 * 1024 * 1024:
                raise ValueError("chat text larger than 512 MB; refusing")
            raw = zf.read(member)
        default_name = _chat_name_from_filename(path)
        if member != "_chat.txt":
            default_name = _chat_name_from_filename(Path(PurePosixPath(member).name))
    else:
        raw = path.read_bytes()
        default_name = _chat_name_from_filename(path)
    text = raw.decode("utf-8-sig", errors="replace")
    return parse_export_text(text, name=name or default_name, tz=tz, me=me, date_order=date_order)


@dataclass
class _Raw:
    date: str
    time: str
    rest: str
    style: str  # "ios" | "android"
    extra: list[str] = field(default_factory=list)


def parse_export_text(
    text: str,
    *,
    name: str,
    tz: tzinfo | None = None,
    me: Iterable[str] = (),
    date_order: str | None = None,
) -> ExportChat:
    tz = tz or _local_tz()
    me_names = {m.strip().casefold() for m in me if m.strip()}
    raws: list[_Raw] = []
    for line in io.StringIO(text):
        line = line.rstrip("\r\n")
        stripped = line.lstrip(_LRM)
        m = _IOS_HEADER.match(stripped)
        style = "ios"
        if not m:
            m = _ANDROID_HEADER.match(stripped)
            style = "android"
        if m:
            try:
                _split_date(m.group("date"))
                _parse_time(m.group("time"))
            except ValueError:
                m = None
        if m:
            raws.append(_Raw(m.group("date"), m.group("time"), m.group("rest"), style))
        elif raws:
            raws[-1].extra.append(line)
        # lines before the first header are ignored

    twelve = any(re.search(r"[AaPp]\.?\s?[Mm]?\.?$", r.time.replace(" ", " ").strip()) for r in raws)
    order = (date_order or _detect_date_order((r.date for r in raws), twelve)).upper()
    conv_id = f"export:{name}"
    senders: set[str] = set()
    messages: list[Message] = []
    last_dt: datetime | None = None
    for idx, raw in enumerate(raws):
        try:
            sent = _to_datetime(raw.date, raw.time, order, tz)
        except ValueError:
            continue  # impossible date under the detected order (e.g. 31/02)
        sender, body = _split_sender(raw.rest)
        body = "\n".join([body, *raw.extra]) if raw.extra else body
        msg_kind, attachment, body_text, system = _classify_export_body(body, raw.style, sender)
        if sender is None:
            system = True
        if system:
            msg_kind = "system"
        is_me = bool(sender) and (sender.casefold() in me_names or sender.casefold() == "you")
        if sender and not system:
            senders.add(sender)
        phone = None
        if sender and _PHONE_LIKE.match(sender):
            digits = re.sub(r"\D", "", sender)
            phone = f"+{digits}" if digits else None
        digest = hashlib.sha256(
            f"{conv_id}\x00{sent.isoformat()}\x00{sender}\x00{body}\x00{idx}".encode()
        ).hexdigest()[:24]
        messages.append(
            Message(
                id=f"export:{digest}",
                rowid=idx + 1,
                conversation_id=conv_id,
                sent_at=sent,
                is_from_me=is_me,
                sender_jid=None,
                sender_phone=phone,
                sender_name=None if system else sender,
                sender_push_name=None,
                text=body_text,
                kind=msg_kind,
                attachment=attachment,
                source="whatsapp-export",
            )
        )
        last_dt = sent
    others = {s for s in senders if s.casefold() not in me_names and s.casefold() != "you"}
    direct = len(others) <= 1 or (len(senders) == 2 and name in senders)
    kind = "direct" if direct else "group"
    conversation = Conversation(
        id=conv_id,
        name=name,
        kind=kind,
        last_message_at=last_dt,
        members=tuple(Member(jid="", phone=None, name=s) for s in sorted(senders)),
        source="whatsapp-export",
    )
    return ExportChat(conversation=conversation, messages=messages)


def _split_sender(rest: str) -> tuple[str | None, str]:
    """'Name: text' -> ('Name', 'text'). System lines with no sender -> (None, rest)."""
    idx = rest.find(": ")
    if idx <= 0:
        if rest.endswith(":"):
            return rest[:-1].strip(_LRM + " ") or None, ""
        return None, rest
    sender = rest[:idx].strip(_LRM + " ")
    if not sender or len(sender) > 80:
        return None, rest
    return sender, rest[idx + 2 :]


def _classify_export_body(
    body: str, style: str, sender: str | None
) -> tuple[str, Attachment | None, str | None, bool]:
    """Return (kind, attachment, text, is_system)."""
    system = body.startswith(tuple(_LRM)) and style == "ios"
    clean = body.lstrip(_LRM)
    clean = _EDITED_SUFFIX.sub("", clean)
    first, _, remainder = clean.partition("\n")
    first_stripped = first.strip(_LRM + " ")

    m = _IOS_ATTACHED.search(first_stripped)
    if m:
        fname = m.group("name").strip()
        kind = _kind_for_filename(fname)
        caption = remainder.strip() or None
        return kind, Attachment(kind=kind, name=fname), caption, False
    m = _ANDROID_ATTACHED.match(first_stripped)
    if m:
        fname = m.group("name").strip()
        kind = _kind_for_filename(fname)
        caption = remainder.strip() or None
        return kind, Attachment(kind=kind, name=fname), caption, False
    if _MEDIA_OMITTED.match(first_stripped):
        return "media", Attachment(kind="media"), remainder.strip() or None, False
    omitted = _IOS_OMITTED.get(first_stripped.casefold())
    if omitted:
        return omitted, Attachment(kind=omitted), remainder.strip() or None, False
    if first_stripped.casefold().endswith("document omitted"):
        # iOS: "‎report.pdf • ‎3 pages ‎document omitted"
        fname = first_stripped.split(" • ", 1)[0].strip(_LRM + " ") if " • " in first_stripped else None
        return "document", Attachment(kind="document", name=fname or None), None, False
    if clean.strip().casefold() in _DELETED:
        return "deleted", None, None, False
    if first_stripped.casefold().startswith("location: "):
        return "location", Attachment(kind="location"), None, False
    if system:
        return "system", None, clean.strip() or None, True
    return "text", None, clean if clean else None, False
