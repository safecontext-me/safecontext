"""Chat-only drop rules: texts from banks, pharmacies and other businesses arrive from
shortcodes, sender IDs or toll-free numbers, with no email domain for the sender rule to
see. They are dropped like sensitive mail (id + reason only); texts from people stay."""
import pytest

from safecontext.demo.fake_messages import FakeFullDiskAccess, make_messages_db
from safecontext.demo.synthetic import mailbox
from safecontext.scrub import Dropped, RawItem, ScrubbedItem, scrub, scrub_item
from safecontext.scrub.drop import is_business_handle
from safecontext.sources.imessage import IMessageSource
from safecontext.store import Store

FRIEND = "+14155550134"


def chat(sender, text, handle=None, title="", kind="chat"):
    return scrub_item(RawItem(id="m1", kind=kind, thread_id="t1", date_ms=1_700_000_000_000, sender=sender,
                              text=text, title=title, sender_handle=handle))


def reason(r):
    return r.reason if isinstance(r, Dropped) else None


# --- the reviewer's samples ------------------------------------------------------------

@pytest.mark.parametrize("sender, text", [
    ("72166", "Chase: A $52.10 transaction was made on your card ending in 1234 at AMAZON."),
    ("+18885551234", "Your Wells Fargo balance is $3,210.55 as of 11/23."),
    ("CVS", "Your prescription for atorvastatin is ready for pickup."),
    ("22395", "Your verification code is 482913"),
])
def test_business_texts_are_dropped(sender, text):
    assert reason(chat(sender, text)) == "business_sender"


@pytest.mark.parametrize("sender, text", [
    ("72166", "Chase: A $52.10 transaction was made on your card ending in 1234 at AMAZON."),
    ("+18885551234", "Your Wells Fargo balance is $3,210.55 as of 11/23."),
    ("CVS", "Your prescription for atorvastatin is ready for pickup."),
])
def test_saved_in_contacts_is_still_a_business(sender, text):
    # the source shows the Contacts name and passes the raw handle separately
    assert reason(chat("My Bank", text, handle=sender)) == "business_sender"
    assert reason(chat("Mom", "see you at 6", handle=sender)) == "business_sender"


@pytest.mark.parametrize("handle, business", [
    ("72166", True), ("22395", True), ("123", True), ("12345678", True),
    ("CVS", True), ("AMZNSMS", True), ("urn:biz:1234-abcd", True),
    ("+18885551234", True), ("+1 (800) 555-1234", True), ("8335551234", True), ("+18445551234", True),
    ("+14155550134", False), ("+447700900123", False), ("4155550134", False), ("+18185551234", False),
    ("jamie@icloud.com", False), ("bob@example.com", False), ("", False),
])
def test_handle_classification(handle, business):
    assert is_business_handle(handle) is business


# --- people: the same kind of notice forwarded, and normal texts ------------------------

@pytest.mark.parametrize("text", [
    "Chase: A $52.10 transaction was made on your card ending in 1234 at AMAZON.",
    "Your Wells Fargo balance is $3,210.55 as of 11/23.",
    "Your prescription for atorvastatin is ready for pickup.",
    "Your lab results are ready. Log in to MyChart to view them.",
    "Reminder: your appointment with Dr. Lee is tomorrow at 3pm. Reply C to confirm.",
    "Your EOB for the claim from 10/2 is available.",
    "Your card was charged $80.00",
])
def test_sensitive_notices_drop_even_from_a_person(text):
    assert reason(chat("Jamie Rivera", text, handle=FRIEND)) == "sensitive:text"


@pytest.mark.parametrize("text", [
    "meet you at the bank at 5?",
    "Dinner Saturday? I can book Nopa for 7.",
    "Chase: you coming tonight?",
    "Chase - can you grab milk on the way",
    "my lab partner is ready for the test tomorrow",
    "I'll pay you back, what's the total? $40?",
    "ugh dentist appointment tomorrow",
    "",
])
def test_normal_texts_from_a_friend_are_kept(text):
    r = chat("Jamie Rivera", text, handle=FRIEND)
    assert isinstance(r, ScrubbedItem), reason(r)


def test_imessage_email_handle_and_my_own_messages_are_kept():
    assert isinstance(chat("Taylor Wu", "running late, 10 min", handle="taylor@example.org"), ScrubbedItem)
    assert isinstance(chat("Me", "ok see you there", handle=""), ScrubbedItem)


def test_group_chat_is_kept():
    r = chat("Sam Patel", "Leaving at 9 from the Mission.", handle="+14155550188", title="Saturday climbing")
    assert isinstance(r, ScrubbedItem)


def test_contacts_name_that_says_bank_or_clinic_drops():
    assert reason(chat("Bright Smile Dental", "see you thursday", handle="+14155550199")) == \
        "sensitive:sender_keyword"
    # without a separate handle, `sender` is the handle itself, not a name
    assert isinstance(chat(FRIEND, "see you thursday"), ScrubbedItem)


# --- only chats get these rules ---------------------------------------------------------

@pytest.mark.parametrize("kind", ["event", "transcript"])
def test_chat_rules_dont_touch_other_kinds(kind):
    r = chat("CVS", "Pick up the cake at 5", kind=kind)
    assert isinstance(r, ScrubbedItem)


def test_mail_drop_behaviour_is_unchanged():
    """Mail goes through scrub() and the email rules only: same outcomes as before chats got rules."""
    outcomes = {}
    for m in mailbox():
        r = scrub(m)
        outcomes[m.id] = r.reason if isinstance(r, Dropped) else "kept"
    kept = sum(1 for v in outcomes.values() if v == "kept")
    assert kept == 6  # what test_app_backend's demo mailbox has always kept
    assert not any(v in ("business_sender", "sensitive:text") for v in outcomes.values())
    # and a mail item through scrub_item doesn't get the chat rules either
    r = scrub_item(RawItem(id="e1", kind="message", thread_id="t", date_ms=0, sender="CVS <hi@example.com>",
                           text="Your prescription is ready for pickup"))
    assert isinstance(r, ScrubbedItem)


# --- end to end: stored like dropped mail -------------------------------------------------

def test_dropped_chats_store_only_id_and_reason(tmp_path):
    msgs = [
        ("jamie", "+14155550134", "Your prescription for atorvastatin is ready for pickup.", None),
        ("jamie", "+14155550134", "meet you at the bank at 5?", None),
        ("bank", "72166", "Chase: A $52.10 transaction was made on your card ending in 1234 at AMAZON.", None),
        ("bank", None, "STOP", None),  # my reply to a shortcode goes with it
    ]
    chats = {"jamie": ("iMessage", ("+14155550134",), None), "bank": ("SMS", ("72166",), None)}
    db = make_messages_db(tmp_path / "Messages" / "chat.db", msgs, chats)
    store = Store(tmp_path / "data" / "mail.db")
    try:
        access = FakeFullDiskAccess(granted=True)
        src = IMessageSource(store, db_path=db, access_fn=access.check, settings_fn=access.open_settings,
                             names_fn=lambda: {"+14155550134": "Jamie Rivera", "72166": "Chase"}.get)
        src.connect()
        r = src.sync()
        assert r.status == "ok" and (r.kept, r.dropped) == (1, 3)
        rows = store._db.execute("SELECT * FROM dropped WHERE source='imessage' ORDER BY id").fetchall()
        assert sorted(row["reason"] for row in rows) == ["business_sender", "business_sender", "sensitive:text"]
        assert set(rows[0].keys()) <= {"source", "id", "thread_id", "date_ms", "reason", "scrubber_version"}
        assert store.search("atorvastatin") == [] and store.search("AMAZON") == []
        # synced again: nothing comes back
        assert src.sync().fetched == 0 and store.counts("imessage") == (1, 3)
    finally:
        store.close()


def test_dropped_chats_show_in_the_not_synced_view(tmp_path):
    from safecontext.app.backend import Backend
    from safecontext.demo.fake_messages import demo_names
    db = make_messages_db(tmp_path / "Messages" / "chat.db")
    access = FakeFullDiskAccess(granted=True)
    b = Backend(tmp_path / "data", demo=True, auto_sync=False,
                imessage_options={"db_path": db, "access_fn": access.check, "names_fn": demo_names})
    try:
        b.connect_source("imessage")
        b._threads["imessage"].join(10)
        rows = b.mail_list("not_synced", "imessage")["rows"]
        assert [(r["type"], r["reason"]) for r in rows] == [("dropped", "business_sender")]
        assert rows[0]["reason_info"]["label"] == "Business or automated text"
        labels = b.privacy_report()["dropped"]
        assert "Texts from businesses & shortcodes" in labels
    finally:
        b._shutdown()
