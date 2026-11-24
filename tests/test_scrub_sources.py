"""Every source goes through scrub/. Email-only drop rules (labels, bulk-mail
headers, bounce senders) apply only to email; OTP/security, financial and
health rules apply to every kind of item."""
import pytest

from safecontext.scrub import Dropped, RawItem, ScrubbedItem, scrub_item
from safecontext.scrub.drop import CHAT_ONLY_RULES, EMAIL_ONLY_RULES, UNIVERSAL_RULES, rules_for
from safecontext.store import Store

KINDS_NOT_EMAIL = ["event", "chat", "transcript"]
ALL_KINDS = ["message", *KINDS_NOT_EMAIL]


def item(kind="chat", **kw):
    base = dict(id="i1", kind=kind, thread_id="t1", date_ms=1_700_000_000_000)
    base.update(kw)
    return RawItem(**base)


def kept(r):
    assert isinstance(r, ScrubbedItem), getattr(r, "reason", r)
    return r


# --- which rules apply where ----------------------------------------------------

def test_rule_table():
    assert rules_for("message") == EMAIL_ONLY_RULES + UNIVERSAL_RULES
    assert rules_for("chat") == UNIVERSAL_RULES + CHAT_ONLY_RULES
    for k in ("event", "transcript"):
        assert rules_for(k) == UNIVERSAL_RULES
    assert set(EMAIL_ONLY_RULES) == {"labels", "bulk"}
    assert set(UNIVERSAL_RULES) == {"sensitive_sender", "subject", "security_body"}


@pytest.mark.parametrize("kind", KINDS_NOT_EMAIL)
def test_list_unsubscribe_and_precedence_only_for_email(kind):
    headers = {"list-unsubscribe": "<mailto:u@x.example>", "precedence": "bulk"}
    kept(scrub_item(item(kind, text="see you at 7", headers=headers, sender="Jamie <jamie@example.com>")))
    r = scrub_item(item("message", text="see you at 7", headers=headers, sender="Jamie <jamie@example.com>"))
    assert isinstance(r, Dropped) and r.reason == "bulk:list_unsubscribe"


@pytest.mark.parametrize("kind", KINDS_NOT_EMAIL)
def test_bounce_sender_only_for_email(kind):
    kept(scrub_item(item(kind, title="Book club", sender="mailer-daemon@meetup.com")))
    r = scrub_item(item("message", title="Book club", sender="mailer-daemon@meetup.com"))
    assert isinstance(r, Dropped) and r.reason == "bulk:bounce"


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_no_reply_sender_kept(kind):
    kept(scrub_item(item(kind, title="Book club", sender="no-reply@meetup.com")))


@pytest.mark.parametrize("kind", KINDS_NOT_EMAIL)
def test_labels_only_for_email(kind):
    kept(scrub_item(item(kind, text="hi", labels=["SPAM", "CATEGORY_PROMOTIONS"])))
    assert scrub_item(item("message", text="hi", labels=["SPAM"])).reason == "spam_trash"


# --- universal rules ----------------------------------------------------------------

@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("text", [
    "Your verification code is 482913",
    "482913 is your Instagram code. Don't share it.",
    "G-482913 is your Google verification code",
    "Use 4829 to sign in to your account",
])
def test_one_time_codes_drop_everywhere(kind, text):
    r = scrub_item(item(kind, text=text))
    assert isinstance(r, Dropped) and r.reason == "security:body"


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_security_titles_drop_everywhere(kind):
    r = scrub_item(item(kind, title="Reset your password", text="click here"))
    assert isinstance(r, Dropped) and r.reason == "security:subject"


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("sender,reason", [
    ("Chase <alerts@chase.com>", "sensitive:sender_domain"),
    ("MyChart <noreply@mychart.org>", "sensitive:sender_domain"),  # no-reply, still dropped as health
    ("appointments@brightsmiledental.com", "sensitive:sender_keyword"),
    ("IRS <info@irs.gov>", "sensitive:sender_domain"),
])
def test_financial_and_health_senders_drop_everywhere(kind, sender, reason):
    r = scrub_item(item(kind, sender=sender, text="hello"))
    assert isinstance(r, Dropped) and r.reason == reason


@pytest.mark.parametrize("kind", ALL_KINDS)
@pytest.mark.parametrize("title", ["Lab results review", "Tax return with accountant", "Pay stub question",
                                   "Explanation of benefits"])
def test_financial_and_health_titles_drop_everywhere(kind, title):
    r = scrub_item(item(kind, title=title))
    assert isinstance(r, Dropped) and r.reason == "sensitive:subject"


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_inline_redaction_everywhere(kind):
    r = kept(scrub_item(item(kind, title="Rent for Dec",
                             text="Card 4111 1111 1111 1111, routing number 021000021, call (415) 555-0134.")))
    assert "4111" not in r.body and "021000021" not in r.body and "555-0134" not in r.body
    assert {"CREDIT_CARD", "ROUTING_NUMBER", "PHONE"} <= set(r.redactions)


def test_extra_fields_are_scrubbed():
    r = kept(scrub_item(item("event", title="Vendor sync", extra={
        "Location": "1 Market St, San Francisco, CA 94105",
        "Notes": "password: hunter2"})))
    assert "Market St" not in r.extra["Location"] and "hunter2" not in r.extra["Notes"]
    assert r.redactions["STREET_ADDRESS"] >= 1 and r.redactions["PASSWORD"] == 1


def test_codes_hidden_in_extra_fields_drop_the_item():
    r = scrub_item(item("event", title="Setup", extra={"Notes": "Your verification code is 482913"}))
    assert isinstance(r, Dropped) and r.reason == "security:body"


def test_html_bodies_are_converted():
    r = kept(scrub_item(item("event", html="<p>Agenda: <b>hiring</b></p><script>x()</script>")))
    assert "<" not in r.body and "hiring" in r.body


def test_quote_stripping_is_for_email_only():
    text = "Sounds good\n\nOn Mon, Jamie wrote:\n> old stuff"
    assert "old stuff" in kept(scrub_item(item("chat", text=text))).body
    assert "old stuff" not in kept(scrub_item(item("message", text=text))).body


def test_identities_kept_as_is():
    r = kept(scrub_item(item("chat", sender="+1 415 555 0134", participants=["+1 415 555 0134", "Alex"],
                             text="see you soon")))
    assert r.sender == "+1 415 555 0134" and r.participants == ["+1 415 555 0134", "Alex"]


def test_attachment_names_scrubbed():
    from safecontext.scrub import Attachment
    r = kept(scrub_item(item("chat", text="here", attachments=[Attachment("SSN_123-45-6789.pdf", "application/pdf", 9)])))
    assert "123-45-6789" not in r.attachments[0].filename


def test_raw_items_never_reach_the_store(tmp_path):
    s = Store(tmp_path / "mail.db")
    with pytest.raises(TypeError):
        s.put(item("chat", text="raw"), source="imessage")


def test_raw_item_repr_hides_content():
    assert "secret" not in repr(item("chat", text="secret", title="secret"))
