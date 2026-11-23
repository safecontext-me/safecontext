"""Tests for safecontext.readers.whatsapp. Fixture data only: never the user's real WhatsApp."""

from __future__ import annotations

import os
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from safecontext.readers import whatsapp as wa
from safecontext.readers.whatsapp import (
    APPLE_EPOCH,
    WhatsAppDesktopReader,
    check_access,
    parse_export,
    parse_export_text,
)

UTC = timezone.utc

# Column lists copied from WhatsApp for Mac 26.45.75 (subset of tables).
SCHEMA = """
CREATE TABLE ZWACHATSESSION ( Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZARCHIVED INTEGER, ZCONTACTABID INTEGER, ZFLAGS INTEGER, ZHIDDEN INTEGER, ZIDENTITYVERIFICATIONEPOCH INTEGER, ZIDENTITYVERIFICATIONSTATE INTEGER, ZMESSAGECOUNTER INTEGER, ZREMOVED INTEGER, ZSESSIONTYPE INTEGER, ZSPOTLIGHTSTATUS INTEGER, ZUNREADCOUNT INTEGER, ZGROUPINFO INTEGER, ZLASTMESSAGE INTEGER, ZPROPERTIES INTEGER, ZLASTMESSAGEDATE TIMESTAMP, ZLOCATIONSHARINGENDDATE TIMESTAMP, ZCONTACTIDENTIFIER VARCHAR, ZCONTACTJID VARCHAR, ZETAG VARCHAR, ZLASTMESSAGETEXT VARCHAR, ZPARTNERNAME VARCHAR, ZSAVEDINPUT VARCHAR );
CREATE TABLE ZWAGROUPMEMBER ( Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZCONTACTABID INTEGER, ZISACTIVE INTEGER, ZISADMIN INTEGER, ZSENDERKEYSENT INTEGER, ZCHATSESSION INTEGER, ZRECENTGROUPCHAT INTEGER, ZCONTACTIDENTIFIER VARCHAR, ZCONTACTNAME VARCHAR, ZFIRSTNAME VARCHAR, ZMEMBERJID VARCHAR );
CREATE TABLE ZWAMEDIAITEM ( Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZCLOUDSTATUS INTEGER, ZFILESIZE INTEGER, ZMEDIAORIGIN INTEGER, ZMOVIEDURATION INTEGER, ZMESSAGE INTEGER, ZASPECTRATIO FLOAT, ZHACCURACY FLOAT, ZLATITUDE FLOAT, ZLONGITUDE FLOAT, ZMEDIAURLDATE TIMESTAMP, ZAUTHORNAME VARCHAR, ZCOLLECTIONNAME VARCHAR, ZMEDIALOCALPATH VARCHAR, ZMEDIAURL VARCHAR, ZTHUMBNAILLOCALPATH VARCHAR, ZTITLE VARCHAR, ZVCARDNAME VARCHAR, ZVCARDSTRING VARCHAR, ZXMPPTHUMBPATH VARCHAR, ZMEDIAKEY BLOB, ZMETADATA BLOB );
CREATE TABLE ZWAMESSAGE ( Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZCHILDMESSAGESDELIVEREDCOUNT INTEGER, ZCHILDMESSAGESPLAYEDCOUNT INTEGER, ZCHILDMESSAGESREADCOUNT INTEGER, ZDATAITEMVERSION INTEGER, ZDOCID INTEGER, ZENCRETRYCOUNT INTEGER, ZFILTEREDRECIPIENTCOUNT INTEGER, ZFLAGS INTEGER, ZGROUPEVENTTYPE INTEGER, ZISFROMME INTEGER, ZMESSAGEERRORSTATUS INTEGER, ZMESSAGESTATUS INTEGER, ZMESSAGETYPE INTEGER, ZSORT INTEGER, ZSPOTLIGHTSTATUS INTEGER, ZSTARRED INTEGER, ZCHATSESSION INTEGER, ZGROUPMEMBER INTEGER, ZLASTSESSION INTEGER, ZMEDIAITEM INTEGER, ZMESSAGEINFO INTEGER, ZPARENTMESSAGE INTEGER, ZMESSAGEDATE TIMESTAMP, ZSENTDATE TIMESTAMP, ZFROMJID VARCHAR, ZMEDIASECTIONID VARCHAR, ZPHASH VARCHAR, ZPUSHNAME VARCHAR, ZSTANZAID VARCHAR, ZTEXT VARCHAR, ZTOJID VARCHAR );
CREATE TABLE ZWAPROFILEPUSHNAME ( Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZJID VARCHAR, ZPUSHNAME VARCHAR );
"""
CONTACTS_SCHEMA = """
CREATE TABLE ZWAADDRESSBOOKCONTACT ( Z_PK INTEGER PRIMARY KEY, ZFULLNAME VARCHAR, ZGIVENNAME VARCHAR, ZLID VARCHAR, ZPHONENUMBER VARCHAR, ZWHATSAPPID VARCHAR );
"""

ALICE = "15550001111@s.whatsapp.net"
BOB_LID = "987654321@lid"
BOB = "15550002222@s.whatsapp.net"
GROUP = "120363000000000001@g.us"


def apple(dt: datetime) -> float:
    return dt.timestamp() - APPLE_EPOCH


def pb_bytes(field_no: int, value: bytes) -> bytes:
    key = (field_no << 3) | 2
    return bytes([key]) + bytes([len(value)]) + value


def quoted_meta(stanza: str, jid: str) -> bytes:
    # Mimics real blobs: other fields around the quote fields.
    return b"\x18\x01" + pb_bytes(5, stanza.encode()) + pb_bytes(6, jid.encode()) + pb_bytes(19, b"hi")


T0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def container(tmp_path: Path) -> Path:
    root = tmp_path / "group.net.whatsapp.WhatsApp.shared"
    root.mkdir()
    db = sqlite3.connect(root / "ChatStorage.sqlite")
    db.executescript(SCHEMA)
    db.execute("PRAGMA journal_mode=WAL")
    sessions = [
        # pk, jid, name, type, archived, removed, hidden
        (1, ALICE, "Alice A", 0, 0, 0, 0),
        (2, GROUP, "Book club", 1, 1, 0, 0),
        (3, "status@broadcast", None, 2, 0, 0, 1),
        (4, BOB_LID, None, 0, 0, 0, 0),
    ]
    for pk, jid, name, stype, arch, rem, hid in sessions:
        db.execute(
            "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE, ZARCHIVED, ZREMOVED, ZHIDDEN, ZLASTMESSAGEDATE) VALUES (?,?,?,?,?,?,?,?)",
            (pk, jid, name, stype, arch, rem, hid, apple(T0 + timedelta(hours=pk))),
        )
    db.executemany(
        "INSERT INTO ZWAGROUPMEMBER (Z_PK, ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME, ZISADMIN, ZISACTIVE) VALUES (?,?,?,?,?,?)",
        [(1, 2, ALICE, "Alice A", 1, 1), (2, 2, BOB_LID, "Bobby", 0, 1), (3, 2, "1555000999@s.whatsapp.net", "Gone", 0, 0)],
    )
    db.executemany(
        "INSERT INTO ZWAPROFILEPUSHNAME (ZJID, ZPUSHNAME) VALUES (?,?)",
        [(ALICE, "ali"), (BOB_LID, "bobcat")],
    )
    # (pk, session, from_me, from_jid, member, type, text, stanza, minutes, media(title, path, size, vcard, meta), starred)
    msgs = [
        (1, 1, 0, ALICE, None, 0, "Hello there", "A1", 0, (None, None, 0, None, b"\x18\x01"), 0),
        (2, 1, 1, None, None, 0, "Hi Alice", "M1", 1, (None, None, 0, None, quoted_meta("A1", ALICE)), 1),
        (3, 1, 0, ALICE, None, 1, None, "A2", 2, ("Look at this", "Media/x/a/b/photo-1.jpg", 1234, "hash", None), 0),
        (4, 2, 0, GROUP, 2, 8, None, "G1", 3, ("Minutes.pdf", None, 5000, "hash", None), 0),
        (5, 2, 0, GROUP, None, 6, None, "G2", 4, (None, None, 0, None, None), 0),
        (6, 2, 0, GROUP, 1, 0, "Reply in group", "G3", 5, (None, None, 0, None, quoted_meta("G1", BOB_LID)), 0),
        (7, 4, 0, BOB_LID, None, 14, "", "B1", 6, ("", None, 0, None, None), 0),
        (8, 4, 0, BOB_LID, None, 4, None, "B2", 7, (None, None, 0, "Carol Card", None), 0),
        (9, 4, 0, BOB_LID, None, 5, None, "B3", 8, (None, None, 0, None, None), 0),
        (10, 1, 0, ALICE, None, 999, "future type", "A3", 9, (None, None, 0, None, b"\xff\xff"), 0),
    ]
    for pk, sess, me, frm, member, mtype, text, stanza, mins, media, starred in msgs:
        title, path, size, vcard, meta = media
        db.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMESSAGE, ZTITLE, ZMEDIALOCALPATH, ZFILESIZE, ZVCARDNAME, ZMETADATA, ZLATITUDE) VALUES (?,?,?,?,?,?,?,?)",
            (pk, pk, title, path, size, vcard, meta, 51.5 if mtype == 5 else 0),
        )
        db.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZFROMJID, ZGROUPMEMBER, ZMESSAGETYPE, ZTEXT, ZSTANZAID, ZMESSAGEDATE, ZMEDIAITEM, ZSTARRED, ZPUSHNAME) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pk, sess, me, frm, member, mtype, text, stanza, apple(T0 + timedelta(minutes=mins)), pk, starred, "QUJDRA=="),
        )
    db.commit()
    db.close()

    c = sqlite3.connect(root / "ContactsV2.sqlite")
    c.executescript(CONTACTS_SCHEMA)
    c.execute(
        "INSERT INTO ZWAADDRESSBOOKCONTACT (ZFULLNAME, ZLID, ZPHONENUMBER, ZWHATSAPPID) VALUES (?,?,?,?)",
        ("Bob Builder", BOB_LID, "+1 555 000 2222", BOB),
    )
    c.commit()
    c.close()
    return root


# --------------------------------------------------------------------- desktop


def test_conversations(container: Path) -> None:
    with WhatsAppDesktopReader(container) as r:
        convs = {c.id: c for c in r.conversations()}
    assert set(convs) == {ALICE, GROUP, BOB_LID}  # hidden status broadcast skipped
    assert convs[ALICE].kind == "direct" and convs[ALICE].phone == "+15550001111"
    assert convs[ALICE].name == "Alice A"
    grp = convs[GROUP]
    assert grp.kind == "group" and grp.archived and grp.name == "Book club"
    assert grp.last_message_at == T0 + timedelta(hours=2)
    members = {m.jid: m for m in grp.members}
    assert set(members) == {ALICE, BOB_LID}  # inactive member dropped
    assert members[ALICE].is_admin
    assert members[BOB_LID].phone == "+15550002222"  # via ContactsV2 LID mapping
    assert members[BOB_LID].name == "Bob Builder"
    # LID chat with no partner name falls back to address book
    assert convs[BOB_LID].name == "Bob Builder" and convs[BOB_LID].phone == "+15550002222"


def test_include_hidden(container: Path) -> None:
    with WhatsAppDesktopReader(container) as r:
        ids = {c.id for c in r.conversations(include_hidden=True)}
    assert "status@broadcast" in ids


def test_messages_fields(container: Path) -> None:
    with WhatsAppDesktopReader(container) as r:
        msgs = {m.id: m for m in r.messages()}
    a1 = msgs["A1"]
    assert a1.text == "Hello there" and a1.kind == "text"
    assert a1.sent_at == T0 and a1.sent_at.tzinfo is UTC
    assert a1.sender_jid == ALICE and a1.sender_phone == "+15550001111"
    assert a1.sender_push_name == "ali" and a1.sender_name == "ali"
    assert a1.reply_to_id is None

    m1 = msgs["M1"]
    assert m1.is_from_me and m1.sender_jid is None and m1.starred
    assert m1.reply_to_id == "A1" and m1.reply_to_sender_jid == ALICE

    photo = msgs["A2"]
    assert photo.kind == "image" and photo.text == "Look at this"  # caption
    assert photo.attachment == wa.Attachment(kind="image", name="photo-1.jpg", size=1234)

    doc = msgs["G1"]
    assert doc.conversation_id == GROUP
    assert doc.sender_jid == BOB_LID and doc.sender_phone == "+15550002222"
    assert doc.sender_name == "Bob Builder" and doc.sender_push_name == "bobcat"
    assert doc.attachment == wa.Attachment(kind="document", name="Minutes.pdf", size=5000)

    sys_msg = msgs["G2"]
    assert sys_msg.kind == "system" and sys_msg.sender_jid is None

    g3 = msgs["G3"]
    assert g3.sender_jid == ALICE and g3.reply_to_id == "G1" and g3.reply_to_sender_jid == BOB_LID

    assert msgs["B1"].kind == "deleted" and msgs["B1"].text is None
    assert msgs["B2"].attachment == wa.Attachment(kind="contact", name="Carol Card", size=None)
    loc = msgs["B3"]
    assert loc.kind == "location" and loc.attachment.name is None
    assert "51.5" not in repr(loc)  # no coordinates leak

    future = msgs["A3"]
    assert future.kind == "other" and future.raw_type == 999
    assert future.reply_to_id is None  # malformed metadata tolerated


def test_push_name_column_is_not_exposed(container: Path) -> None:
    with WhatsAppDesktopReader(container) as r:
        assert all("QUJDRA" not in repr(m) for m in r.messages())


def test_incremental_cursor(container: Path) -> None:
    with WhatsAppDesktopReader(container) as r:
        first = list(r.messages(limit=4))
        assert [m.rowid for m in first] == [1, 2, 3, 4]
        rest = list(r.messages(after_rowid=first[-1].rowid))
        assert [m.rowid for m in rest] == [5, 6, 7, 8, 9, 10]
        assert r.latest_rowid() == 10
        assert list(r.messages(after_rowid=10)) == []
        only_alice = [m.id for m in r.messages(conversation_id=ALICE)]
        assert only_alice == ["A1", "M1", "A2", "A3"]
        assert all(m.kind != "system" for m in r.messages(include_system=False))


def test_late_history_sync_is_picked_up(container: Path) -> None:
    """Old messages synced after linking get a higher Z_PK; the rowid cursor still sees them."""
    db = sqlite3.connect(container / "ChatStorage.sqlite")
    db.execute(
        "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZFROMJID, ZMESSAGETYPE, ZTEXT, ZSTANZAID, ZMESSAGEDATE) VALUES (11, 1, 0, ?, 0, 'old', 'OLD', ?)",
        (ALICE, apple(datetime(2019, 5, 1, tzinfo=UTC))),
    )
    db.commit()
    db.close()
    with WhatsAppDesktopReader(container) as r:
        new = list(r.messages(after_rowid=10))
    assert [m.id for m in new] == ["OLD"] and new[0].sent_at.year == 2019


def test_snapshot_does_not_modify_source(container: Path) -> None:
    db = container / "ChatStorage.sqlite"
    before = db.read_bytes()
    with WhatsAppDesktopReader(container) as r:
        list(r.messages())
        r.conversations()
    assert db.read_bytes() == before


def test_live_mode_and_schema_drift(container: Path) -> None:
    """Missing optional columns/tables degrade to None rather than crash."""
    db = sqlite3.connect(container / "ChatStorage.sqlite")
    db.execute("DROP TABLE ZWAPROFILEPUSHNAME")
    db.execute("ALTER TABLE ZWAMESSAGE DROP COLUMN ZSTARRED")
    db.execute("ALTER TABLE ZWACHATSESSION DROP COLUMN ZARCHIVED")
    db.commit()
    db.close()
    with WhatsAppDesktopReader(container, snapshot=False) as r:
        msgs = list(r.messages())
        convs = r.conversations()
    assert len(msgs) == 10 and not any(m.starred for m in msgs)
    assert all(m.sender_push_name is None for m in msgs)
    assert not any(c.archived for c in convs)


# ------------------------------------------------------------------- access


def test_check_access_ok(container: Path) -> None:
    status = check_access(container)
    assert status.ok and status.state == "ok"


def test_check_access_not_installed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(wa, "APP_PATHS", (str(tmp_path / "nope.app"),))
    status = check_access(tmp_path / "missing")
    assert status.state == "not_installed"


def test_check_access_installed_but_no_container(tmp_path: Path, monkeypatch) -> None:
    app = tmp_path / "WhatsApp.app"
    app.mkdir()
    monkeypatch.setattr(wa, "APP_PATHS", (str(app),))
    assert check_access(tmp_path / "missing").state == "no_data"


def test_check_access_no_db_and_empty_db(tmp_path: Path) -> None:
    root = tmp_path / "c"
    root.mkdir()
    assert check_access(root).state == "no_data"
    db = sqlite3.connect(root / "ChatStorage.sqlite")
    db.executescript(SCHEMA)
    db.close()
    assert check_access(root).state == "no_data"


def test_check_access_no_permission(container: Path, monkeypatch) -> None:
    def deny(*a, **k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(wa.os, "listdir", deny)
    status = check_access(container)
    assert status.state == "no_permission"
    assert status.settings_path == "System Settings > Privacy & Security > Full Disk Access"
    assert status.settings_url.startswith("x-apple.systempreferences:")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_check_access_no_permission_real_chmod(container: Path) -> None:
    container.chmod(0)
    try:
        assert check_access(container).state == "no_permission"
    finally:
        container.chmod(0o700)


# ------------------------------------------------------------------ exports

IOS_EXPORT = (
    "‎[01/11/2026, 09:15:02] Book club: ‎Messages and calls are end-to-end encrypted. Only people in this chat can read them.\n"
    "[01/11/2026, 09:15:30] Alice A: Hello all\n"
    "[01/11/2026, 09:16:00] Bob: First line\n"
    "second line\n"
    "\n"
    "third after blank\n"
    "‎[01/11/2026, 09:17:00] Bob: ‎<attached: 00000012-PHOTO-2026-11-01-09-17-00.jpg>\n"
    "‎[13/11/2026, 21:00:00] Alice A: ‎image omitted\n"
    "[13/11/2026, 21:01:00] Alice A: ‎Report.pdf • ‎3 pages ‎document omitted\n"
    "[13/11/2026, 21:02:00] Bob: This message was deleted\n"
    "[13/11/2026, 21:03:00] Bob: fixed typo <This message was edited>\n"
    "‎[13/11/2026, 21:04:00] Alice A: ‎Alice A added Carol\n"
)

ANDROID_US_12H = (
    "11/23/26, 2:05 PM - Messages and calls are end-to-end encrypted. No one outside of this chat can read them.\n"
    "11/23/26, 2:05 PM - +1 (555) 000-3333: hey\n"
    "11/23/26, 2:06 PM - Me: IMG-20261123-WA0001.jpg (file attached)\n"
    "caption for photo\n"
    "11/23/26, 12:07 AM - +1 (555) 000-3333: <Media omitted>\n"
    "11/23/26, 12:30 PM - Me: noon: with colon\n"
)

ANDROID_DE_24H = (
    "23.11.26, 14:05 - Anna: Hallo\n"
    "23.11.26, 14:06 - Ben: Wie geht's?\n"
    "Zeile zwei\n"
    "24.11.26, 08:00 - Anna hat Ben hinzugefügt\n"
)


def test_ios_export_parsing() -> None:
    chat = parse_export_text(IOS_EXPORT, name="Book club", tz=UTC)
    msgs = chat.messages
    assert chat.conversation.kind == "group"
    assert chat.conversation.id == "export:Book club"
    assert msgs[0].kind == "system" and msgs[0].sender_name is None
    assert msgs[1].sender_name == "Alice A" and msgs[1].text == "Hello all"
    assert msgs[1].sent_at == datetime(2026, 11, 1, 9, 15, 30, tzinfo=UTC)
    assert msgs[2].text == "First line\nsecond line\n\nthird after blank"
    assert msgs[3].kind == "image" and msgs[3].attachment.name == "00000012-PHOTO-2026-11-01-09-17-00.jpg"
    assert msgs[4].kind == "image" and msgs[4].attachment.name is None
    assert msgs[4].sent_at.day == 13  # D/M/Y detected from 13/11
    assert msgs[5].kind == "document" and msgs[5].attachment.name == "Report.pdf"
    assert msgs[6].kind == "deleted" and msgs[6].text is None
    assert msgs[7].text == "fixed typo"
    assert msgs[8].kind == "system"
    assert len({m.id for m in msgs}) == len(msgs)
    assert all(m.source == "whatsapp-export" for m in msgs)


def test_android_us_12h_export() -> None:
    chat = parse_export_text(ANDROID_US_12H, name="+1 (555) 000-3333", tz=UTC, me=["Me"])
    msgs = chat.messages
    assert chat.conversation.kind == "direct"
    assert msgs[0].kind == "system"
    hey = msgs[1]
    assert hey.sent_at == datetime(2026, 11, 23, 14, 5, tzinfo=UTC)
    assert hey.sender_phone == "+15550003333" and not hey.is_from_me
    photo = msgs[2]
    assert photo.is_from_me and photo.kind == "image"
    assert photo.attachment.name == "IMG-20261123-WA0001.jpg" and photo.text == "caption for photo"
    assert msgs[3].sent_at.hour == 0 and msgs[3].kind == "media"
    assert msgs[4].sent_at.hour == 12 and msgs[4].text == "noon: with colon"


def test_android_german_24h_export_with_tz() -> None:
    from zoneinfo import ZoneInfo

    chat = parse_export_text(ANDROID_DE_24H, name="Anna", tz=ZoneInfo("Europe/Berlin"))
    msgs = chat.messages
    assert msgs[0].sent_at == datetime(2026, 11, 23, 13, 5, tzinfo=UTC)  # CET = UTC+1
    assert msgs[1].text == "Wie geht's?\nZeile zwei"
    assert msgs[2].kind == "system" and msgs[2].sent_at.day == 24


def test_ambiguous_dates_default_and_override() -> None:
    text = "[01/02/2026, 10:00:00] A: x\n"
    assert parse_export_text(text, name="A", tz=UTC).messages[0].sent_at.month == 2  # DMY default for 24h
    assert parse_export_text(text, name="A", tz=UTC, date_order="MDY").messages[0].sent_at.month == 1
    us = "[1/2/26, 10:00:00 AM] A: x\n"
    assert parse_export_text(us, name="A", tz=UTC).messages[0].sent_at.month == 1  # 12h -> MDY default


def test_ymd_and_spanish_ampm() -> None:
    ymd = "2026/11/23 14:05 - A: x\n"
    assert parse_export_text(ymd, name="A", tz=UTC).messages[0].sent_at == datetime(2026, 11, 23, 14, 5, tzinfo=UTC)
    es = "23/11/26, 2:05 p. m. - A: x\n"
    assert parse_export_text(es, name="A", tz=UTC).messages[0].sent_at.hour == 14


def test_export_zip_and_txt(tmp_path: Path) -> None:
    z = tmp_path / "WhatsApp Chat - Book club.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("_chat.txt", "﻿" + IOS_EXPORT)
        zf.writestr("00000012-PHOTO-2026-11-01-09-17-00.jpg", b"\xff\xd8not really")
    chat = parse_export(z, tz=UTC)
    assert chat.conversation.name == "Book club" and len(chat.messages) == 9
    assert not (tmp_path / "00000012-PHOTO-2026-11-01-09-17-00.jpg").exists()  # nothing extracted

    t = tmp_path / "WhatsApp Chat with Anna.txt"
    t.write_text(ANDROID_DE_24H, encoding="utf-8")
    chat2 = parse_export(t, tz=UTC)
    assert chat2.conversation.name == "Anna" and len(chat2.messages) == 3

    android_zip = tmp_path / "WhatsApp Chat with Anna.zip"
    with zipfile.ZipFile(android_zip, "w") as zf:
        zf.writestr("WhatsApp Chat with Anna.txt", ANDROID_DE_24H)
    assert parse_export(android_zip, tz=UTC).conversation.name == "Anna"


def test_export_zip_without_text(tmp_path: Path) -> None:
    z = tmp_path / "x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("photo.jpg", b"x")
    with pytest.raises(ValueError):
        parse_export(z)


def test_export_ids_stable() -> None:
    a = parse_export_text(IOS_EXPORT, name="Book club", tz=UTC)
    b = parse_export_text(IOS_EXPORT, name="Book club", tz=UTC)
    assert [m.id for m in a.messages] == [m.id for m in b.messages]


# -------------------------------------------------------------- real smoke


@pytest.mark.skipif(
    not os.environ.get("SAFECONTEXT_WHATSAPP_SMOKE"),
    reason="set SAFECONTEXT_WHATSAPP_SMOKE=1 to run a read-only count check against the real DB",
)
def test_real_db_smoke_counts_only() -> None:
    import pwd

    # conftest fakes HOME, so resolve the real one explicitly.
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    container = real_home / "Library" / "Group Containers" / wa.GROUP_CONTAINER
    status = check_access(container)
    if not status.ok:
        pytest.skip(status.state)
    with WhatsAppDesktopReader(container) as r:
        convs = r.conversations()
        n = sum(1 for _ in r.messages())
        replies = sum(1 for m in r.messages() if m.reply_to_id)
        kinds: dict[str, int] = {}
        for m in r.messages():
            kinds[m.kind] = kinds.get(m.kind, 0) + 1
    print(f"\nconversations={len(convs)} messages={n} replies={replies} kinds={dict(sorted(kinds.items()))}")
    assert n > 0
