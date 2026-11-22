import pytest

from safecontext.scrub import Dropped, ScrubbedMessage, scrub
from safecontext.scrub.drop import DropConfig
from safecontext.demo.synthetic import msg


def reason(m, config=None):
    r = scrub(m, config) if config else scrub(m)
    return r.reason if isinstance(r, Dropped) else None


@pytest.mark.parametrize("labels,expected", [
    (("SPAM",), "spam_trash"),
    (("TRASH",), "spam_trash"),
    (("INBOX", "CATEGORY_PROMOTIONS"), "category:promotions"),
    (("INBOX", "CATEGORY_SOCIAL"), "category:social"),
    (("INBOX", "CATEGORY_FORUMS"), "category:forums"),
])
def test_label_drops(labels, expected):
    assert reason(msg(1, labels=labels, text="hi")) == expected


@pytest.mark.parametrize("labels", [("INBOX",), ("SENT",), ("INBOX", "CATEGORY_PERSONAL"),
                                    ("INBOX", "CATEGORY_UPDATES"), ("IMPORTANT", "STARRED")])
def test_label_keeps(labels):
    assert reason(msg(1, labels=labels, text="hi")) is None


def test_list_unsubscribe():
    m = msg(1, text="hi", extra_headers={"List-Unsubscribe": "<mailto:x@example.com>"})
    assert reason(m) == "bulk:list_unsubscribe"


def test_precedence_bulk():
    assert reason(msg(1, text="hi", extra_headers={"Precedence": "bulk"})) == "bulk:precedence"


@pytest.mark.parametrize("frm", ["noreply@example.com", "no-reply@example.com", "No_Reply <no_reply@x.example>",
                                 "donotreply@example.com", "do-not-reply@example.com",
                                 "MAILER-DAEMON@mx.example.com", "noreply+tag@example.com"])
def test_no_reply_senders(frm):
    assert reason(msg(1, frm=frm, text="hi")) == "bulk:no_reply_sender"


@pytest.mark.parametrize("frm", ["Reply Guy <replyguy@example.com>", "noreen@example.com", "jamie@example.com"])
def test_normal_senders_kept(frm):
    assert reason(msg(1, frm=frm, text="hi")) is None


@pytest.mark.parametrize("subject", ["Your verification code", "Reset your password", "Password reset request",
                                     "New sign-in to your account", "Your one-time passcode",
                                     "Security alert", "Your login link", "Enable 2FA", "Verify your email"])
def test_security_subjects(subject):
    assert reason(msg(1, subject=subject, text="hi")) == "security:subject"


@pytest.mark.parametrize("body", ["Your verification code is 771204.", "Use login code: 4821 to continue",
                                  "Click here to reset your password."])
def test_security_bodies(body):
    assert reason(msg(1, subject="Hello", text=body)) == "security:body"


@pytest.mark.parametrize("frm,expected", [
    ("Chase <alerts@chase.com>", "sensitive:sender_domain"),
    ("alerts@notify.wellsfargo.com", "sensitive:sender_domain"),
    ("IRS <noticeinfo@irs.gov>", "sensitive:sender_domain"),
    ("MyChart <mychart@mychart.org>", "sensitive:sender_domain"),
    ("x@firstcitybank.example", "sensitive:sender_keyword"),
    ("x@valleyhealth.example", "sensitive:sender_keyword"),
    ("x@mountainview-dental.example", "sensitive:sender_keyword"),
    ("x@acmecreditunion.example", "sensitive:sender_keyword"),
])
def test_sensitive_senders(frm, expected):
    assert reason(msg(1, frm=frm, text="hi")) == expected


def test_allow_list_overrides_sender_rules():
    cfg = DropConfig(allow_domains=frozenset({"valleyhealth.example"}))
    assert reason(msg(1, frm="colleague@valleyhealth.example", text="lunch?"), cfg) is None


@pytest.mark.parametrize("subject", ["Your statement is ready", "Your 2025 W-2 is available", "1099-INT attached",
                                     "Tax return draft", "Lab results available", "Your prescription is ready",
                                     "Explanation of Benefits", "Direct deposit change", "Pay stub for Oct",
                                     "Appointment reminder", "Your account statement"])
def test_sensitive_subjects(subject):
    assert reason(msg(1, subject=subject, text="hi")) == "sensitive:subject"


@pytest.mark.parametrize("subject", ["Dinner Saturday?", "Press statement draft", "Code review for PR 12",
                                     "Notes from Tuesday", "Trip to Taxco"])
def test_ordinary_subjects_kept(subject):
    assert reason(msg(1, subject=subject, text="see attached")) is None


def test_dropped_carries_no_content():
    r = scrub(msg(1, subject="Your verification code", text="Your verification code is 771204"))
    assert isinstance(r, Dropped)
    assert "771204" not in repr(r) and "verification" not in repr(r)


def test_category_toggle():
    cfg = DropConfig(drop_categories=False)
    assert isinstance(scrub(msg(1, labels=("CATEGORY_SOCIAL",), text="hi"), cfg), ScrubbedMessage)
