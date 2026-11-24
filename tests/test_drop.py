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


@pytest.mark.parametrize("frm", ["MAILER-DAEMON@mx.example.com", "Mail Delivery <postmaster@x.example>",
                                 "bounces+123@lists.example.com"])
def test_bounce_senders(frm):
    assert reason(msg(1, frm=frm, text="hi")) == "bulk:bounce"


@pytest.mark.parametrize("frm", ["Reply Guy <replyguy@example.com>", "noreen@example.com", "jamie@example.com",
                                 "Shop <no-reply@shop.example>", "noreply+orders@example.com",
                                 "Airline <donotreply@air.example>", "do-not-reply@example.com"])
def test_normal_and_no_reply_senders_kept(frm):
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


ACCOUNT_SECURITY_NOTICES = [
    "[GitHub] A third-party OAuth application has been added to your account",
    "Slack was granted access to your Google Account",
    "An app was connected to your Dropbox account",
    "Zapier has been authorized to access your account",
    "New device signed in to your account",
    "Your Apple ID was used to sign in to a new device",
    "You signed in on a new device",
    "New login alert",
    "[GitHub] A new SSH authentication public key was added to your account",
    "A new GPG key was added to your account",
    "A personal access token was created for your account",
    "A passkey was added",
    "Your recovery email was changed",
    "Your recovery phone number has been updated",
    "Your email address was changed",
    "Two-factor authentication has been turned off",
    "2-step verification is now enabled",
]


@pytest.mark.parametrize("subject", ACCOUNT_SECURITY_NOTICES)
def test_account_security_subjects(subject):
    assert reason(msg(1, subject=subject, text="hi")) == "security:subject"


@pytest.mark.parametrize("body", [
    "Hi Alex, a third-party OAuth application (Example CI) with read:org scope was recently authorized "
    "to access your account.",
    "A new SSH key was added to your account. If you did not do this, remove it now.",
    "We noticed you signed in from a new browser. Was this you?",
    "The application Zapier has been granted access to your Google Account.",
    "Your backup phone number was removed from your account.",
])
def test_account_security_bodies(body):
    assert reason(msg(1, subject="Security notification", text=body)) in {"security:subject", "security:body"}
    assert reason(msg(1, subject="Hello", text=body)) == "security:body"


@pytest.mark.parametrize("subject,body", [
    ("How to rotate your API keys", "Best practice: rotate API keys every 90 days."),
    ("Changelog: API key management", "We added API key management to the dashboard."),
    ("New device lineup announced", "Our new phones ship Friday."),
    ("New integrations this month", "Ten new app integrations were added to the marketplace."),
    ("Your password manager questions answered", "Passkeys explained in plain English."),
    ("Trip photos", "We connected to the hotel wifi from a new phone."),
])
def test_account_security_lookalikes_kept(subject, body):
    assert reason(msg(1, subject=subject, text=body)) is None


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


@pytest.mark.parametrize("frm", ["Hinge <hello@hinge.co>", "Tinder <noreply@mail.gotinder.com>",
                                 "Bumble <no-reply@bumble.com>", "Match <info@match.com>",
                                 "OkCupid <noreply@okcupid.com>", "Grindr <help@grindr.com>",
                                 "Feeld <hello@feeld.co>", "CMB <hello@coffeemeetsbagel.com>",
                                 "news@localsingles.example", "team@speeddating.example"])
def test_dating_app_senders(frm):
    assert reason(msg(1, frm=frm, subject="You have a new match", text="Say hi!")) == "sensitive:dating"


@pytest.mark.parametrize("frm", ["tickets@matchday.example", "Hinged Doors <sales@hingeddoors.example>",
                                 "Bumblebee Tuna <hi@bumblebee.example>"])
def test_dating_lookalike_senders_kept(frm):
    assert reason(msg(1, frm=frm, subject="Hello", text="hi")) is None


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
