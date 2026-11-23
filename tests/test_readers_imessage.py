"""Messages and Contacts readers against fixture databases built in a temp dir.

The schemas below copy the real tables/columns the readers use (plus a few
neighbours, so SELECTs that name only some columns are exercised). Nothing
here touches the real ~/Library/Messages or AddressBook: conftest points HOME
at a temporary folder, and every path is passed explicitly anyway.
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import sqlite3
from datetime import datetime, timezone

import pytest

from safecontext.readers import contacts, imessage
from safecontext.readers.imessage import (
    AccessState,
    MessagesAccessError,
    MessagesReader,
    decode_attributed_body,
)

# --------------------------------------------------------------------------
# Fixture builders

CHAT_SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT UNIQUE, id TEXT NOT NULL,
  country TEXT, service TEXT NOT NULL, uncanonicalized_id TEXT, person_centric_id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
  style INTEGER, state INTEGER, account_id TEXT, properties BLOB, chat_identifier TEXT,
  service_name TEXT, room_name TEXT, account_login TEXT, is_archived INTEGER DEFAULT 0,
  last_addressed_handle TEXT, display_name TEXT, group_id TEXT, is_filtered INTEGER DEFAULT 0);
CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
  text TEXT, replace INTEGER DEFAULT 0, service_center TEXT, handle_id INTEGER DEFAULT 0,
  subject TEXT, country TEXT, attributedBody BLOB, version INTEGER DEFAULT 0,
  type INTEGER DEFAULT 0, service TEXT, account TEXT, account_guid TEXT, error INTEGER DEFAULT 0,
  date INTEGER, date_read INTEGER, date_delivered INTEGER, is_delivered INTEGER DEFAULT 0,
  is_finished INTEGER DEFAULT 0, is_emote INTEGER DEFAULT 0, is_from_me INTEGER DEFAULT 0,
  is_empty INTEGER DEFAULT 0, is_read INTEGER DEFAULT 0, is_audio_message INTEGER DEFAULT 0,
  cache_has_attachments INTEGER DEFAULT 0, item_type INTEGER DEFAULT 0, other_handle INTEGER DEFAULT 0,
  group_title TEXT, group_action_type INTEGER DEFAULT 0, associated_message_guid TEXT,
  associated_message_type INTEGER DEFAULT 0, balloon_bundle_id TEXT, payload_data BLOB,
  date_retracted INTEGER DEFAULT 0, date_edited INTEGER DEFAULT 0,
  thread_originator_guid TEXT, thread_originator_part TEXT, destination_caller_id TEXT,
  associated_message_emoji TEXT, message_summary_info BLOB);
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
  created_date INTEGER DEFAULT 0, filename TEXT, uti TEXT, mime_type TEXT,
  transfer_state INTEGER DEFAULT 0, is_outgoing INTEGER DEFAULT 0, transfer_name TEXT,
  total_bytes INTEGER DEFAULT 0, hide_attachment INTEGER DEFAULT 0);
CREATE TABLE chat_handle_join (chat_id INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE,
  handle_id INTEGER REFERENCES handle (ROWID) ON DELETE CASCADE, UNIQUE(chat_id, handle_id));
CREATE TABLE chat_message_join (chat_id INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE,
  message_id INTEGER REFERENCES message (ROWID) ON DELETE CASCADE, message_date INTEGER DEFAULT 0,
  PRIMARY KEY (chat_id, message_id));
CREATE TABLE message_attachment_join (message_id INTEGER REFERENCES message (ROWID) ON DELETE CASCADE,
  attachment_id INTEGER REFERENCES attachment (ROWID) ON DELETE CASCADE, UNIQUE(message_id, attachment_id));
"""


def typedstream(text: str) -> bytes:
    """An attributedBody blob laid out the way Messages writes it."""
    raw = text.encode("utf-8")
    if len(raw) < 0x80:
        length = bytes([len(raw)])
    elif len(raw) < 0x10000:
        length = b"\x81" + len(raw).to_bytes(2, "little")
    else:
        length = b"\x82" + len(raw).to_bytes(4, "little")
    return (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00"
        b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
        + length
        + raw
        + b"\x86\x84\x02iI\x01" + bytes([min(len(text), 0x7F)])
        + b"\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i\x01\x92\x84\x96\x96\x1d"
        b"__kIMMessagePartAttributeName\x86\x92\x84\x84\x84\x08NSNumber\x00\x84\x84\x07NSValue"
        b"\x00\x94\x84\x01*\x84\x99\x99\x00\x86\x86\x86"
    )


def apple_ns(dt: datetime) -> int:
    return int((dt - datetime(2001, 1, 1, tzinfo=timezone.utc)).total_seconds()) * 1_000_000_000


T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
ALICE, BOB, CAROL, SHORT = "+15551230001", "bob@example.com", "+447700900123", "12345"


def make_chat_db(path, *, drop_columns=()):
    conn = sqlite3.connect(path)
    schema = CHAT_SCHEMA
    for col in drop_columns:  # simulate an older macOS without these columns
        schema = schema.replace(f", {col} TEXT", "").replace(f", {col} INTEGER DEFAULT 0", "")
    conn.executescript(schema)
    conn.execute("PRAGMA journal_mode=WAL")
    handles = {}
    for hid, service in ((ALICE, "iMessage"), (BOB, "iMessage"), (CAROL, "iMessage"), (SHORT, "SMS")):
        handles[hid] = conn.execute("INSERT INTO handle (id, service) VALUES (?, ?)", (hid, service)).lastrowid

    def chat(guid, ident, service, style, members, name=None):
        cid = conn.execute(
            "INSERT INTO chat (guid, style, chat_identifier, service_name, display_name) VALUES (?,?,?,?,?)",
            (guid, style, ident, service, name or ""),
        ).lastrowid
        for h in members:
            conn.execute("INSERT INTO chat_handle_join VALUES (?, ?)", (cid, handles[h]))
        return cid

    c_alice = chat(f"iMessage;-;{ALICE}", ALICE, "iMessage", 45, [ALICE])
    c_group = chat("iMessage;+;chat123456", "chat123456", "iMessage", 43, [ALICE, BOB, CAROL], "Climbing")
    c_sms = chat(f"SMS;-;{SHORT}", SHORT, "SMS", 45, [SHORT])

    n = [0]

    def msg(chat_id, sender, text=None, body=None, service="iMessage", from_me=0, **extra):
        n[0] += 1
        cols = dict(
            guid=f"GUID-{n[0]}", text=text, attributedBody=body, service=service, is_from_me=from_me,
            handle_id=handles[sender] if sender else 0, date=apple_ns(T0) + n[0] * 60_000_000_000,
        )
        cols.update({k: v for k, v in extra.items() if k not in drop_columns})
        keys = ",".join(cols)
        rid = conn.execute(
            f"INSERT INTO message ({keys}) VALUES ({','.join('?' * len(cols))})", list(cols.values())
        ).lastrowid
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)", (chat_id, rid))
        return rid

    msg(c_alice, ALICE, text="Hey, dinner Friday?")                                    # 1 plain text
    msg(c_alice, ALICE, body=typedstream("Only in attributedBody 🎉"))                  # 2 body only
    msg(c_alice, ALICE, from_me=1, text="Sounds good", thread_originator_guid="GUID-1")  # 3 reply, from me
    msg(c_alice, ALICE, associated_message_type=2000, associated_message_guid="p:0/GUID-3",
        text='Loved "Sounds good"')                                                      # 4 tapback
    msg(c_group, BOB, text="Who's in for Saturday?")                                     # 5 group
    msg(c_group, CAROL, body=typedstream("Me! " + "x" * 300))                            # 6 long body
    rid = msg(c_group, ALICE, text="￼", cache_has_attachments=1)                    # 7 attachment only
    for guid, name, mime, uti in (
        ("A1", "IMG_0001.HEIC", "image/heic", "public.heic"),
        ("A2", "x.pluginPayloadAttachment", None, "com.apple.messages.plugin"),
    ):
        aid = conn.execute(
            "INSERT INTO attachment (guid, filename, transfer_name, mime_type, uti) VALUES (?,?,?,?,?)",
            (guid, f"~/Library/Messages/Attachments/ab/12/{guid}/{name}", name, mime, uti),
        ).lastrowid
        conn.execute("INSERT INTO message_attachment_join VALUES (?, ?)", (rid, aid))
    msg(c_group, None, item_type=2, group_title="Climbing")                              # 8 group rename
    msg(c_sms, SHORT, service="SMS", text="Your verification code is 482913. Do not share it.")  # 9 OTP
    msg(c_alice, ALICE, service="RCS", text="", date_retracted=apple_ns(T0))             # 10 unsent: skipped
    msg(c_alice, ALICE, associated_message_type=3001, associated_message_guid="p:0/GUID-1")  # 11 remove like
    msg(c_alice, ALICE, service="RCS", text="edited text", date_edited=apple_ns(T0))     # 12 RCS + edited
    conn.commit()
    conn.close()


# AddressBook (Core Data) schema: only the tables/columns that matter plus neighbours.
AB_SCHEMA = """
CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
  ZFIRSTNAME VARCHAR, ZLASTNAME VARCHAR, ZMIDDLENAME VARCHAR, ZNICKNAME VARCHAR,
  ZORGANIZATION VARCHAR, ZNAME VARCHAR, ZDISPLAYFLAGS INTEGER, ZUNIQUEID VARCHAR);
CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
  ZOWNER INTEGER, ZFULLNUMBER VARCHAR, ZLABEL VARCHAR, ZAREACODE VARCHAR, ZCOUNTRYCODE VARCHAR);
CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
  ZOWNER INTEGER, ZADDRESS VARCHAR, ZADDRESSNORMALIZED VARCHAR, ZLABEL VARCHAR);
"""


def make_addressbook(root, people_by_source):
    for source, people in people_by_source.items():
        folder = root if source is None else root / "Sources" / source
        folder.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(folder / "AddressBook-v22.abcddb")
        conn.executescript(AB_SCHEMA)
        for pk, (first, last, org, phones, emails) in enumerate(people, start=1):
            conn.execute(
                "INSERT INTO ZABCDRECORD (Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION) VALUES (?,?,?,?)",
                (pk, first, last, org),
            )
            for p in phones:
                conn.execute("INSERT INTO ZABCDPHONENUMBER (ZOWNER, ZFULLNUMBER) VALUES (?, ?)", (pk, p))
            for e in emails:
                conn.execute("INSERT INTO ZABCDEMAILADDRESS (ZOWNER, ZADDRESS) VALUES (?, ?)", (pk, e))
        # a group record: name but no handles
        conn.execute("INSERT INTO ZABCDRECORD (Z_PK, ZNAME) VALUES (999, 'Family')")
        conn.commit()
        conn.close()


@pytest.fixture
def chat_db(tmp_path):
    path = tmp_path / "Messages" / "chat.db"
    path.parent.mkdir()
    make_chat_db(path)
    return path


def digest(path):
    h = hashlib.sha256()
    for suffix in ("", "-wal"):
        p = str(path) + suffix
        if os.path.exists(p):
            h.update(open(p, "rb").read())
    return h.hexdigest()


# --------------------------------------------------------------------------
# attributedBody


def test_decode_typedstream_short_and_long():
    assert decode_attributed_body(typedstream("hi")) == "hi"
    long = "é" * 200  # 400 bytes: 0x81 int16 length
    assert decode_attributed_body(typedstream(long)) == long
    huge = "a" * 70_000  # 0x82 int32 length
    assert decode_attributed_body(typedstream(huge)) == huge


def test_decode_mutable_string_and_keyed_archive():
    blob = typedstream("mutable").replace(b"\x08NSString", b"\x0fNSMutableString")
    assert decode_attributed_body(blob) == "mutable"
    archive = plistlib.dumps(
        {
            "$archiver": "NSKeyedArchiver",
            "$objects": ["$null", {"NSString": plistlib.UID(2)}, {"NS.string": "keyed"}],
            "$top": {"root": plistlib.UID(1)},
            "$version": 100000,
        },
        fmt=plistlib.FMT_BINARY,
    )
    assert decode_attributed_body(archive) == "keyed"


@pytest.mark.parametrize("blob", [None, b"", b"garbage", typedstream("hello")[:60], b"\x04\x0bstreamtyped"])
def test_decode_malformed_returns_none(blob):
    assert decode_attributed_body(blob) is None


# --------------------------------------------------------------------------
# Messages reader


def test_conversations(chat_db):
    with MessagesReader(chat_db) as r:
        convs = {c.identifier: c for c in r.conversations()}
    group = convs["chat123456"]
    assert group.is_group and group.display_name == "Climbing"
    assert set(group.participants) == {ALICE, BOB, CAROL}
    one = convs[ALICE]
    assert not one.is_group and one.display_name is None and one.participants == (ALICE,)
    assert convs[SHORT].service == "SMS"


def test_messages(chat_db):
    with MessagesReader(chat_db) as r:
        msgs = {m.guid: m for m in r.messages()}
    # skipped: tapback (4), group rename (8), unsent (10), tapback removal (11)
    assert sorted(msgs) == sorted(f"GUID-{i}" for i in (1, 2, 3, 5, 6, 7, 9, 12))

    assert msgs["GUID-1"].text == "Hey, dinner Friday?"
    assert msgs["GUID-1"].sender == ALICE and not msgs["GUID-1"].is_from_me
    assert msgs["GUID-1"].timestamp == datetime(2026, 11, 1, 12, 1, tzinfo=timezone.utc)
    assert msgs["GUID-1"].chat_id == f"iMessage;-;{ALICE}"

    assert msgs["GUID-2"].text == "Only in attributedBody 🎉"

    reply = msgs["GUID-3"]
    assert reply.is_from_me and reply.sender is None and reply.reply_to == "GUID-1"

    assert msgs["GUID-5"].chat_id == "iMessage;+;chat123456" and msgs["GUID-5"].sender == BOB
    assert msgs["GUID-6"].text == "Me! " + "x" * 300

    att = msgs["GUID-7"]
    assert att.text == "" and att.has_attachments
    assert [(a.name, a.mime_type, a.uti) for a in att.attachments] == [("IMG_0001.HEIC", "image/heic", "public.heic")]

    otp = msgs["GUID-9"]
    assert otp.service == "SMS" and otp.sender == SHORT and "482913" in otp.text

    assert msgs["GUID-12"].service == "RCS" and msgs["GUID-12"].edited


def test_attachment_names_only_no_paths(chat_db):
    with MessagesReader(chat_db) as r:
        for m in r.messages():
            for a in m.attachments:
                assert "/" not in a.name


def test_reactions_compact(chat_db):
    with MessagesReader(chat_db) as r:
        reactions = [m for m in r.messages(include_reactions=True) if m.reaction]
    assert [(m.reaction.kind, m.reaction.emoji, m.reaction.removed, m.reaction.target_guid, m.text)
            for m in reactions] == [
        ("love", "❤️", False, "GUID-3", ""),
        ("like", "👍", True, "GUID-1", ""),
    ]


def test_incremental_cursor(chat_db):
    with MessagesReader(chat_db) as r:
        end = r.max_rowid()
        first = [m.rowid for m in r.messages(until_rowid=5)]
        rest = [m.rowid for m in r.messages(since_rowid=5, until_rowid=end)]
        assert first == [1, 2, 3, 5] and rest == [6, 7, 9, 12]
        assert list(r.messages(since_rowid=end)) == []
        assert len(list(r.messages(limit=2))) == 2


def test_live_db_not_modified_and_snapshot_cleaned_up(chat_db):
    before = digest(chat_db)
    with MessagesReader(chat_db) as r:
        list(r.messages())
        with pytest.raises(sqlite3.OperationalError):
            r.conn.execute("DELETE FROM message")
    snap = MessagesReader(chat_db, snapshot=True)
    tmp = snap._tmpdir
    assert len(list(snap.messages())) == 8
    snap.close()
    assert not os.path.exists(tmp)
    assert digest(chat_db) == before


def test_older_schema_without_optional_columns(tmp_path):
    path = tmp_path / "chat.db"
    make_chat_db(path, drop_columns=("associated_message_emoji", "thread_originator_guid", "date_edited", "date_retracted"))
    with MessagesReader(path) as r:
        msgs = {m.guid: m for m in r.messages()}
    assert msgs["GUID-3"].reply_to is None
    assert msgs["GUID-2"].text == "Only in attributedBody 🎉"


# --------------------------------------------------------------------------
# Access checks


def test_check_access_states(tmp_path, chat_db, monkeypatch):
    assert imessage.check_access(chat_db).state is AccessState.OK
    assert imessage.check_access(tmp_path / "nope" / "chat.db").state is AccessState.NO_DATA
    not_db = tmp_path / "bad.db"
    not_db.write_bytes(b"not sqlite at all")
    assert imessage.check_access(not_db).state is AccessState.ERROR

    real_open = open

    def denied(path, *a, **k):
        if str(path) == str(chat_db):
            raise PermissionError(1, "Operation not permitted")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", denied)
    status = imessage.check_access(chat_db)
    assert status.state is AccessState.NO_PERMISSION
    assert "Full Disk Access" in status.message and status.settings_url.startswith("x-apple.systempreferences:")
    with pytest.raises(MessagesAccessError) as e:
        MessagesReader(chat_db)
    assert e.value.status.state is AccessState.NO_PERMISSION


def test_default_path_uses_fake_home():
    # conftest points HOME at a temp folder, so this can never be the real DB.
    assert imessage.check_access().state is AccessState.NO_DATA


def test_open_settings(monkeypatch):
    calls = []

    class Done:
        returncode = 0

    monkeypatch.setattr(imessage.subprocess, "run", lambda args, **k: calls.append(args) or Done())
    assert imessage.open_settings()
    assert calls == [["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"]]


# --------------------------------------------------------------------------
# Contacts


@pytest.fixture
def addressbook(tmp_path):
    root = tmp_path / "AddressBook"
    make_addressbook(
        root,
        {
            None: [],  # the root store is often empty
            "ICLOUD-UUID": [
                ("Alice", "Ng", None, ["(555) 123-0001"], []),
                ("Bob", None, None, [], ["Bob@Example.com"]),
            ],
            "GOOGLE-UUID": [
                (None, None, "Carol's Bakery", ["+44 7700 900123"], []),
            ],
        },
    )
    return root


def test_contacts_lookup(addressbook):
    book = contacts.load_contacts(addressbook, region="US", use_framework=False)
    assert len(book.sources) == 3
    assert book.lookup(ALICE) == "Alice Ng"
    assert book.lookup("bob@example.com") == "Bob"
    assert book.lookup("mailto:BOB@example.com") == "Bob"
    assert book.lookup(CAROL) == "Carol's Bakery"
    assert book.lookup(SHORT) is None
    assert book.lookup("+15559999999") is None


def test_contacts_national_fallback(tmp_path):
    root = tmp_path / "AB"
    make_addressbook(root, {"X": [("Dee", None, None, ["020 7946 0018"], [])]})
    # Saved without a country code on a Mac whose region is wrong: still matches.
    book = contacts.load_contacts(root, region="US", use_framework=False)
    assert book.lookup("+442079460018") == "Dee"


def test_normalize_handle():
    assert contacts.normalize_handle("(555) 123-0001", "US") == "+15551230001"
    assert contacts.normalize_handle("tel:+44 7700 900123", "US") == "+447700900123"
    assert contacts.normalize_handle("A@B.com", "US") == "a@b.com"
    assert contacts.normalize_handle("12345", "US") == "12345"
    assert contacts.normalize_handle("AMAZON", "US") == "amazon"


def test_contacts_access_states(tmp_path, addressbook, monkeypatch):
    assert contacts.check_access(addressbook).state is AccessState.OK
    assert contacts.check_access(tmp_path / "missing").state is AccessState.NO_DATA
    empty = tmp_path / "empty"
    empty.mkdir()
    assert contacts.check_access(empty).state is AccessState.NO_DATA

    def denied(path, *a, **k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("builtins.open", denied)
    status = contacts.check_access(addressbook)
    assert status.state is AccessState.NO_PERMISSION
    assert status.settings_path == contacts.FDA_SETTINGS_PATH


def test_contacts_read_only(addressbook):
    files = sorted(addressbook.rglob("*.abcddb"))
    before = [hashlib.sha256(f.read_bytes()).hexdigest() for f in files]
    contacts.load_contacts(addressbook, region="US", use_framework=False)
    assert [hashlib.sha256(f.read_bytes()).hexdigest() for f in files] == before
