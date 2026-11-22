"""One test group per redaction rule: things it must catch, things it must leave alone."""
import pytest

from mycontext.scrub.redact import card_prefix_ok, iban_ok, luhn_ok, redact, ssn_ok
from mycontext.demo.synthetic import PRIVATE_KEY, _pad


def kinds(text):
    return redact(text)[1]


def assert_redacted(text, kind, secret):
    out, counts = redact(text)
    assert secret not in out, out
    assert counts.get(kind, 0) >= 1, (out, counts)
    assert f"[REDACTED:{kind}]" in out


def assert_untouched(text):
    out, counts = redact(text)
    assert out == text and counts == {}, (out, counts)


# --- credit cards ---------------------------------------------------------

@pytest.mark.parametrize("card", [
    "4111 1111 1111 1111", "4111-1111-1111-1111", "4111111111111111",
    "378282246310005", "3782 822463 10005", "5555555555554444", "6011111111111117",
    "2223003122003222", "3530111333300000",
])
def test_card_caught(card):
    assert_redacted(f"card: {card} thanks", "CREDIT_CARD", card)


def test_card_glued_to_expiry_year():
    out, _ = redact("4111 1111 1111 1111 2027")
    assert out == "[REDACTED:CREDIT_CARD] 2027"


@pytest.mark.parametrize("text", [
    "4111 1111 1111 1112",           # fails Luhn
    "1234567890123456",              # bad prefix
    "Order #112-4456789-1234567",
    "Tracking 1Z999AA10123456784",
])
def test_card_not_caught(text):
    assert "CREDIT_CARD" not in kinds(text)


def test_card_validators():
    assert luhn_ok("4111111111111111") and not luhn_ok("4111111111111112")
    assert card_prefix_ok("378282246310005") and not card_prefix_ok("1111111111111117")


# --- SSN ------------------------------------------------------------------

@pytest.mark.parametrize("text,secret", [
    ("SSN 536-22-1987", "536-22-1987"),
    ("ssn is 536 22 1987", "536 22 1987"),
    ("social security number: 536221987", "536221987"),
    ("SSN#536221987", "536221987"),
])
def test_ssn_caught(text, secret):
    assert_redacted(text, "SSN", secret)


@pytest.mark.parametrize("text", ["000-12-3456", "666-12-3456", "912-12-3456", "123-00-4567", "123-45-0000",
                                  "536-22 1987", "call 536221987 now"])
def test_ssn_not_caught(text):
    assert "SSN" not in kinds(text)


def test_ssn_validator():
    assert ssn_ok("536-22-1987") and not ssn_ok("666-22-1987")


# --- API keys -------------------------------------------------------------

@pytest.mark.parametrize("secret", [
    "AKIAIOSFODNN7EXAMPLE",
    "sk-proj-" + _pad("", 40),
    "sk-" + _pad("", 48),
    "sk-ant-api03-" + _pad("", 40),
    _pad("xai-", 40),
    _pad("ghp_", 36),
    _pad("gho_", 36),
    _pad("github_pat_", 50, "Canary_0123456789"),
    "xoxb-1234567890-" + _pad("", 20),
    "https://hooks.slack.com/services/T000/B000/" + _pad("", 24),
    _pad("AIza", 35),
    _pad("GOCSPX-", 28),
    "ya29." + _pad("", 60),
    "1//0" + _pad("", 60),
    _pad("sk_live_", 24),
    _pad("rk_test_", 24),
    _pad("whsec_", 32),
])
def test_api_key_caught(secret):
    assert_redacted(f"here you go: {secret} (rotate later)", "API_KEY", secret)


def test_aws_secret_access_key():
    s = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    assert_redacted(f"aws_secret_access_key = {s}", "API_KEY", s)


@pytest.mark.parametrize("text", ["skip-this-step", "the xai-team meeting", "AKIA is a prefix", "sk_live_short"])
def test_api_key_not_caught(text):
    assert "API_KEY" not in kinds(text)


# --- JWT / private keys ---------------------------------------------------

def test_jwt_caught():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.c2lnbmF0dXJlLWNhbmFyeQ"
    assert_redacted(f"Authorization: Bearer {jwt}", "JWT", jwt)


def test_private_key_caught():
    out, counts = redact(f"key:\n{PRIVATE_KEY}\nthanks")
    assert "PRIVATE KEY" not in out and "Canary" not in out
    assert counts["PRIVATE_KEY"] == 1 and out.endswith("thanks")


def test_private_key_unterminated_redacts_to_end():
    out, _ = redact("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n(truncated)")
    assert out == "[REDACTED:PRIVATE_KEY]"


def test_pgp_private_key():
    block = "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF0xyz\n-----END PGP PRIVATE KEY BLOCK-----"
    assert redact(block)[0] == "[REDACTED:PRIVATE_KEY]"


# --- passwords ------------------------------------------------------------

@pytest.mark.parametrize("text,secret", [
    ("password: Hunter2!Canary", "Hunter2!Canary"),
    ("Password = Hunter2!Canary", "Hunter2!Canary"),
    ("the wifi password is Hunter2!Canary", "Hunter2!Canary"),
    ("pwd: Hunter2!Canary", "Hunter2!Canary"),
    ("passcode - Hunter2!Canary", "Hunter2!Canary"),
    ("Password:\nHunter2!Canary", "Hunter2!Canary"),
    ("https://admin:Hunter2!Canary@db.example.com/x", "Hunter2!Canary"),
])
def test_password_caught(text, secret):
    assert_redacted(text, "PASSWORD", secret)


@pytest.mark.parametrize("text", ["The file is password-protected", "I forgot my password again",
                                  "password reset happened", "pinned the message"])
def test_password_not_caught(text):
    assert "PASSWORD" not in kinds(text)


# --- one-time codes -------------------------------------------------------

@pytest.mark.parametrize("text,secret", [
    ("Your code is 482913", "482913"),
    ("verification code: 482-913", "482-913"),
    ("OTP 4829", "4829"),
    ("G-482913 is your Google verification code", "G-482913"),
    ("2FA token 48291375", "48291375"),
])
def test_otp_caught(text, secret):
    assert_redacted(text, "ONE_TIME_CODE", secret)


@pytest.mark.parametrize("text", ["error code 404", "the code review is done", "code freeze on Nov 14"])
def test_otp_not_caught(text):
    assert "ONE_TIME_CODE" not in kinds(text)


# --- bank / routing / IBAN / CVV -----------------------------------------

@pytest.mark.parametrize("text,kind,secret", [
    ("routing number 021000021", "ROUTING_NUMBER", "021000021"),
    ("ABA: 021000021", "ROUTING_NUMBER", "021000021"),
    ("RTN#021000021", "ROUTING_NUMBER", "021000021"),
    ("account number 000123456789", "BANK_ACCOUNT", "000123456789"),
    ("Acct #: 0001-2345-6789", "BANK_ACCOUNT", "0001-2345-6789"),
    ("account no. is 12345678", "BANK_ACCOUNT", "12345678"),
    ("CVV 737", "CVV", "737"),
    ("cvc: 1234", "CVV", "1234"),
])
def test_bank_caught(text, kind, secret):
    assert_redacted(text, kind, secret)


@pytest.mark.parametrize("iban", ["GB82 WEST 1234 5698 7654 32", "GB82WEST12345698765432",
                                  "DE89370400440532013000", "FR1420041010050500013M02606"])
def test_iban_caught(iban):
    assert_redacted(f"IBAN {iban} THANKS", "IBAN", iban)


def test_iban_trailing_word_kept():
    assert redact("IBAN GB82 WEST 1234 5698 7654 32 OK")[0] == "IBAN [REDACTED:IBAN] OK"


@pytest.mark.parametrize("text", ["GB82 WEST 1234 5698 7654 33", "US12 ABCD 1234 5678 90",
                                  "account for 2024", "the account was closed"])
def test_bank_not_caught(text):
    k = kinds(text)
    assert "IBAN" not in k and "BANK_ACCOUNT" not in k


def test_iban_validator():
    assert iban_ok("GB82WEST12345698765432") and not iban_ok("GB82WEST12345698765433")


# --- date of birth --------------------------------------------------------

@pytest.mark.parametrize("text,secret", [
    ("DOB: 03/14/1985", "03/14/1985"),
    ("date of birth 1985-03-14", "1985-03-14"),
    ("born on March 14, 1985", "March 14, 1985"),
    ("Birthdate: 14 Mar 1985", "14 Mar 1985"),
    ("D.O.B. 3-14-85", "3-14-85"),
])
def test_dob_caught(text, secret):
    assert_redacted(text, "DATE_OF_BIRTH", secret)


@pytest.mark.parametrize("text", ["meeting on 03/14/1985", "shipped 2024-03-14", "due March 14, 2025"])
def test_dob_not_caught(text):
    assert_untouched(text)


# --- phone ----------------------------------------------------------------

@pytest.mark.parametrize("phone", ["(415) 555-0132", "415-555-0132", "415.555.0132", "+1 212-555-0187",
                                   "+1 (212) 555-0187", "+44 20 7946 0958", "4155550132"])
def test_phone_caught(phone):
    assert_redacted(f"call me at {phone} tonight", "PHONE", phone)


@pytest.mark.parametrize("text", ["v2.4.1 at 10:30 on 2024-03-14", "$1,299.00 per seat", "room 415"])
def test_phone_not_caught(text):
    assert_untouched(text)


# --- street address -------------------------------------------------------

@pytest.mark.parametrize("text,secret", [
    ("I live at 742 Evergreen Terrace, Springfield, IL 62704", "742 Evergreen Terrace"),
    ("ship to 1600 Pennsylvania Ave NW", "1600 Pennsylvania Ave"),
    ("123 Main St, Apt 4B", "Apt 4B"),
    ("office: 500 W 2nd Street Suite 1900", "500 W 2nd Street"),
    ("Springfield, IL 62704", "62704"),
    ("mail it to PO Box 1234", "Box 1234"),
    ("42 MARKET STREET", "42 MARKET STREET"),
])
def test_address_caught(text, secret):
    assert_redacted(text, "STREET_ADDRESS", secret)


@pytest.mark.parametrize("text", ["I ran 3 miles today", "call at 5 and we'll drive over",
                                  "We need 2 more chairs", "Section 4 is done"])
def test_address_not_caught(text):
    assert_untouched(text)


# --- whole-text behaviour -------------------------------------------------

def test_clean_text_untouched():
    assert_untouched("Notes:\n- Ship v2.4.1 by Nov 14\n- Budget is $1,299.00\n- Order #112-4456789-1234567")


def test_counts_accumulate():
    _, counts = redact("SSN 536-22-1987 and 4111 1111 1111 1111 and (415) 555-0132")
    assert counts == {"SSN": 1, "CREDIT_CARD": 1, "PHONE": 1}
