"""2FA codes are the top-priority rule. Two layers are tested:
  * automated security mail is dropped whole (nothing stored at all)
  * a code that shows up in ordinary mail is redacted inline
All codes and keys below are synthetic."""
import pytest

from safecontext.scrub import Dropped, ScrubbedMessage, scrub
from safecontext.scrub.redact import redact
from safecontext.demo.synthetic import msg

# --- automated 2FA mail: dropped whole, even with a bland subject --------

AUTOMATED = [
    "Your verification code is 593017. It expires in 10 minutes.",
    "593017 is your Instagram code. Don't share it.",
    "G-593017 is your Google verification code.",
    "Your login code:\n\n593017\n\nIf you didn't request this, ignore this email.",
    "Security code\n593 017",
    "Enter 5930 to sign in to your account.",
    "Use 593017 to verify your email address.",
    "Your one-time passcode is K7X-9QP",
    "Your authentication code: AB12CD",
    "Here is your sign-in code: 59301746",
    "Your 2FA code is 593017",
    "Two-factor code 593017",
    "Confirmation code: 593-017",
    "Scan this QR or open otpauth://totp/Example:alex?secret=JBSWY3DPEHPK3PXP&issuer=Example",
    "Your backup codes:\n8412 5590\n3307 1864\n9921 0475",
]


@pytest.mark.parametrize("body", AUTOMATED)
def test_automated_2fa_mail_dropped(body):
    r = scrub(msg(1, frm="Example <accounts@example-app.com>", subject="Action required", text=body))
    assert isinstance(r, Dropped) and r.reason.startswith("security"), r


@pytest.mark.parametrize("body", AUTOMATED)
def test_automated_2fa_mail_dropped_as_html(body):
    html = "<table><tr><td>" + body.replace("\n", "<br>") + "</td></tr></table>"
    r = scrub(msg(1, frm="Example <accounts@example-app.com>", subject="Hello", html=html))
    assert isinstance(r, Dropped), r


@pytest.mark.parametrize("subject", ["593017 is your Slack code", "G-593017 is your Google verification code",
                                     "Your Netflix code", "Your code: 593017", "Sign in to Example",
                                     "Your backup codes", "Login attempt from a new device",
                                     "Confirm it's you", "Your access code"])
def test_2fa_subjects_dropped(subject):
    r = scrub(msg(1, subject=subject, text="see body"))
    assert isinstance(r, Dropped) and r.reason == "security:subject"


# --- codes inside ordinary mail: redacted inline ---------------------------

@pytest.mark.parametrize("text,secret", [
    ("Can you read me the code? it's 593017", "593017"),
    ("the code they texted was 593 017", "593 017"),
    ("OTP 4829 lol", "4829"),
    ("pin is 4829", "4829"),
    ("code:\n593017", "593017"),
    ("593017 is the code from the bank", "593017"),
    ("type 593017 to continue", "593017"),
    ("the token was ZX81QP", "ZX81QP"),
    ("verification K7X-9QP", "K7X-9QP"),
])
def test_inline_code_redacted(text, secret):
    out, counts = redact(text)
    assert secret not in out, out
    assert counts.get("ONE_TIME_CODE", 0) + counts.get("PASSWORD", 0) >= 1


@pytest.mark.parametrize("text,secret", [
    ("otpauth://totp/Ex:alex?secret=JBSWY3DPEHPK3PXP&issuer=Ex", "JBSWY3DPEHPK3PXP"),
    ("setup key: jbsw y3dp ehpk 3pxp", "jbsw y3dp ehpk 3pxp"),
    ("Secret key JBSWY3DPEHPK3PXPJBSWY3DP", "JBSWY3DPEHPK3PXPJBSWY3DP"),
])
def test_authenticator_secret_redacted(text, secret):
    out, counts = redact(text)
    assert secret not in out and counts.get("TOTP_SECRET") == 1, out


def test_backup_codes_redacted():
    out, counts = redact("Save these backup codes somewhere safe:\n8412 5590  3307 1864\n9921-0475  77ab-k2m9q")
    for code in ["8412 5590", "3307 1864", "9921-0475", "77ab-k2m9q"]:
        assert code not in out
    assert counts["BACKUP_CODE"] == 4


def test_personal_mail_with_code_is_kept_but_redacted():
    r = scrub(msg(1, subject="netflix", text="It's asking for a code, can you read it to me? code is 593017"))
    assert isinstance(r, ScrubbedMessage)
    assert "593017" not in r.body and r.redactions["ONE_TIME_CODE"] == 1


# --- must not break other rules or over-redact -----------------------------

def test_code_rule_does_not_split_a_card():
    out, counts = redact("promo code 4111 1111 1111 1111")
    assert "1111" not in out and counts == {"CREDIT_CARD": 1}


def test_code_rule_does_not_split_a_phone():
    out, counts = redact("area code 415 555 0132")
    assert "555" not in out and counts == {"PHONE": 1}


@pytest.mark.parametrize("text", [
    "the code review is Tuesday", "error code 404", "zip code is 9", "code freeze on Nov 14",
    "our team code name is BLUEJAY", "the secret is just keep this safe",
    "my pin board is full", "backup codes are a good idea",
])
def test_no_false_positive(text):
    out, counts = redact(text)
    assert counts.get("ONE_TIME_CODE") is None and counts.get("TOTP_SECRET") is None \
        and counts.get("BACKUP_CODE") is None, out


@pytest.mark.parametrize("body", ["Can you review my code? It's in PR 4821 on GitHub.",
                                  "The access road code is posted at the gate"])
def test_ordinary_mail_about_code_not_dropped(body):
    assert isinstance(scrub(msg(1, subject="question", text=body)), ScrubbedMessage)
