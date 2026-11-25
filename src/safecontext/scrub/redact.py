"""Layer 2: inline redaction, replacing matches with [REDACTED:TYPE].

Rules run in a fixed order. Long, unambiguous secrets (keys, tokens) go
first so later, looser number rules can't half-match them; phone numbers
and addresses go last because they are the broadest.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Pattern, Tuple

import phonenumbers

from . import otp

Span = Tuple[int, int]


def token(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def _replace_spans(text: str, spans: List[Span], kind: str) -> Tuple[str, int]:
    if not spans:
        return text, 0
    spans = sorted(spans)
    merged: List[Span] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    out, last = [], 0
    for s, e in merged:
        out.append(text[last:s])
        out.append(token(kind))
        last = e
    out.append(text[last:])
    return "".join(out), len(merged)


@dataclass(frozen=True)
class RegexRule:
    kind: str
    pattern: Pattern[str]
    group: int = 0
    validate: Optional[Callable[[str], bool]] = None

    def __call__(self, text: str) -> Tuple[str, int]:
        spans = []
        for m in self.pattern.finditer(text):
            s, e = m.span(self.group)
            if s < 0:
                continue
            if self.validate and not self.validate(m.group(self.group)):
                continue
            spans.append((s, e))
        return _replace_spans(text, spans, self.kind)


# --- validators -----------------------------------------------------------

def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        x = int(ch)
        if i % 2 == 1:
            x *= 2
            if x > 9:
                x -= 9
        total += x
    return total % 10 == 0


def card_prefix_ok(d: str) -> bool:
    n = len(d)
    p2, p3, p4, p6 = int(d[:2]), int(d[:3]), int(d[:4]), int(d[:6])
    return (
        (d[0] == "4" and n in (13, 16, 19))                                   # Visa
        or (51 <= p2 <= 55 and n == 16) or (2221 <= p4 <= 2720 and n == 16)   # Mastercard
        or (p2 in (34, 37) and n == 15)                                       # Amex
        or ((p4 == 6011 or p2 == 65 or 644 <= p3 <= 649) and 16 <= n <= 19)  # Discover
        or (622126 <= p6 <= 622925 and 16 <= n <= 19)
        or (3528 <= p4 <= 3589 and 16 <= n <= 19)                            # JCB
        or ((300 <= p3 <= 305 or p2 in (36, 38, 39) or p4 == 3095) and 14 <= n <= 19)  # Diners
        or (p2 == 62 and 16 <= n <= 19)                                       # UnionPay
    )


def ssn_ok(s: str) -> bool:
    d = re.sub(r"\D", "", s)
    if len(d) != 9:
        return False
    area, group, serial = d[:3], d[3:5], d[5:]
    return area not in ("000", "666") and area[0] != "9" and group != "00" and serial != "0000"


def iban_ok(compact: str) -> bool:
    if not (15 <= len(compact) <= 34) or not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]+", compact):
        return False
    rearranged = compact[4:] + compact[:4]
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    return int(numeric) % 97 == 1


# --- custom (non-single-regex) redactors ----------------------------------

_DIGIT_GROUPS = re.compile(r"(?<![\d])\d+(?:[ .-]\d+)*(?![\d])")


def redact_cards(text: str) -> Tuple[str, int]:
    """Find card numbers even when glued to neighbouring digit groups
    (e.g. a card followed by an expiry year), by testing every contiguous
    window of digit groups for a valid length, prefix, and Luhn checksum."""
    spans: List[Span] = []
    for m in _DIGIT_GROUPS.finditer(text):
        groups = [(g.start() + m.start(), g.end() + m.start(), g.group())
                  for g in re.finditer(r"\d+", m.group())]
        i = 0
        while i < len(groups):
            found = None
            digits = ""
            for j in range(i, len(groups)):
                digits += groups[j][2]
                if len(digits) > 19:
                    break
                if len(digits) >= 13 and card_prefix_ok(digits) and luhn_ok(digits):
                    found = j  # keep extending: prefer the longest valid window
            if found is not None:
                spans.append((groups[i][0], groups[found][1]))
                i = found + 1
            else:
                i += 1
    return _replace_spans(text, spans, "CREDIT_CARD")


_IBAN_CANDIDATE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{1,4}){3,8}\b")


def redact_ibans(text: str) -> Tuple[str, int]:
    spans: List[Span] = []
    for m in _IBAN_CANDIDATE.finditer(text):
        idx = [m.start() + k for k, c in enumerate(m.group()) if c != " "]
        compact = m.group().replace(" ", "")
        for end in range(len(compact), 14, -1):  # trim trailing junk groups
            if iban_ok(compact[:end]):
                spans.append((idx[0], idx[end - 1] + 1))
                break
    return _replace_spans(text, spans, "IBAN")


_NANP = re.compile(
    r"(?<![\w.-])(?:\+?1[ .-]?)?(?:\(\d{3}\)[ ]?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\w-])"
)


def redact_phones(text: str) -> Tuple[str, int]:
    spans: List[Span] = [(m.start, m.end) for m in phonenumbers.PhoneNumberMatcher(text, "US")]
    spans += [m.span() for m in _NANP.finditer(text)]
    return _replace_spans(text, spans, "PHONE")


# --- regex rules ----------------------------------------------------------

_I = re.I

PRIVATE_KEY = RegexRule("PRIVATE_KEY", re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
    r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)", re.S))

JWT = RegexRule("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"))

API_KEY_PATTERNS = [
    r"\b(?:AKIA|ASIA|ABIA|ACCA|A3T[A-Z0-9])[A-Z0-9]{16}\b",          # AWS access key id
    r"\bsk-(?:proj-|svcacct-|admin-|ant-[a-z0-9]+-)?[A-Za-z0-9_-]{20,}",  # OpenAI / Anthropic
    r"\bxai-[A-Za-z0-9]{20,}",                                          # xAI
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b",                    # GitHub classic
    r"\bgithub_pat_[A-Za-z0-9_]{22,}",                                  # GitHub fine-grained
    r"\bxox[abposr]-[A-Za-z0-9-]{10,}",                                 # Slack tokens
    r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+",              # Slack webhooks
    r"\bAIza[0-9A-Za-z_-]{35}",                                         # Google API key
    r"\bGOCSPX-[A-Za-z0-9_-]{20,}",                                     # Google OAuth secret
    r"\bya29\.[A-Za-z0-9_-]{20,}",                                      # Google access token
    r"\b1//0[A-Za-z0-9_-]{20,}",                                        # Google refresh token
    r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}",                   # Stripe
    r"\bwhsec_[A-Za-z0-9]{20,}",                                        # Stripe webhook secret
]
API_KEY = RegexRule("API_KEY", re.compile("|".join(f"(?:{p})" for p in API_KEY_PATTERNS)))
AWS_SECRET = RegexRule("API_KEY", re.compile(
    r"aws.{0,20}?secret.{0,20}?[=:]\s*[\"']?([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])", _I), group=1)

URL_CREDENTIALS = RegexRule("PASSWORD", re.compile(r"(?<=://)[^\s/:@]+:([^\s/@]+)@"), group=1)

PASSWORD = RegexRule("PASSWORD", re.compile(
    r"\b(?:password|passwd|passcode|passphrase|pwd|pw|pin)\b"
    r"(?:\s*[:=]\s*|[ \t]+-[ \t]+|\s+(?:is|was)\s*:?\s*)"  # not "password-protected"
    r"(?!\[REDACTED)(\S+)", _I), group=1)

# Meeting dial-in secrets. With the dial-in number, a PIN, conference ID or host
# key lets anyone join (or take over) the call. They are often written in groups
# ("PIN: 123 456 789#"), which PASSWORD would only half-eat, so this runs first.
DIAL_IN_PIN = RegexRule("PASSWORD", re.compile(
    r"\b(?:pin|passcode|password|host[ \t]+(?:key|pin)|moderator[ \t]+pin|participant[ \t]+(?:code|pin)|"
    r"attendee[ \t]+(?:id|pin)|(?:phone[ \t]+)?conference[ \t]+id)\b"
    r"[ \t]*[:=#]?[ \t]*(\d{3,}(?:[ \t.-]\d{2,}){0,4})#?(?!\d)", _I), group=1)
# One-tap dial strings ("+16699006833,,81234567890#,,,,*482913#"): the digits
# after the pauses are the meeting ID and passcode or conference PIN.
DIAL_IN_ONE_TAP = RegexRule("PASSWORD", re.compile(r",\*?(\d{4,})#"), group=1)

CVV = RegexRule("CVV", re.compile(r"\b(?:cvv2?|cvc2?|csc|card security code)\b\D{0,12}?(\d{3,4})\b", _I),
                group=1)

SSN = RegexRule("SSN", re.compile(r"(?<![\d-])\d{3}([- ])\d{2}\1\d{4}(?![\d-])"), validate=ssn_ok)
SSN_CONTEXT = RegexRule("SSN", re.compile(
    r"\b(?:ssn|social security(?: number| no\.?| #)?)\b\D{0,15}?(\d{9})\b", _I), group=1)

_NUM_LABEL = r"(?:[ \t]*(?:number|no\.?|num|#))?[ \t]*[:#]?[ \t]*(?:is[ \t]+)?"
ROUTING = RegexRule("ROUTING_NUMBER", re.compile(
    rf"\b(?:routing|aba|rtn|transit){_NUM_LABEL}(\d{{9}})\b", _I), group=1)
BANK_ACCOUNT = RegexRule("BANK_ACCOUNT", re.compile(
    rf"\b(?:account|acct|a/c){_NUM_LABEL}(\d[\d -]{{3,22}}\d)\b", _I), group=1)

# Booking codes: airline record locators / PNRs and hotel, rail and car-rental
# confirmation numbers. With the traveller's name, a code lets anyone view,
# change or cancel the booking. Only a code right after a booking word counts,
# so ordinary 6-character tokens and order numbers are left alone. Letters-only
# codes ("KXJQPT") also need a label or colon, so "RESERVATION DETAILS" in
# an all-caps heading isn't taken for one.
_BOOKING_KEY = r"(?i:\b(?:confirmation|booking|reservation|record[ \t]+locator|PNR|itinerary|trip[ \t]+id))"
_BOOKING_LABEL = r"(?i:[ \t]+(?:code|number|no\.?|num|#|id|ref(?:erence)?|locator))"
_BOOKING_SEP = r"[ \t]*(?:[:#][ \t]*)?(?i:(?:is|was)[ \t]+)?"
BOOKING_CODE = RegexRule("BOOKING_CODE", re.compile(
    rf"{_BOOKING_KEY}{_BOOKING_LABEL}?{_BOOKING_SEP}(?<![\w-])((?=[A-Z]*\d)[A-Z0-9]{{5,14}})(?![\w-])"), group=1)
_NOT_LOCATORS = frozenset({"CANCEL", "CHANGE", "STATUS", "UPDATE", "NUMBER", "DETAIL", "PLEASE", "REVIEW",
                           "RECORD", "MANAGE", "BELOW", "ACTIVE", "CLOSED", "PAYMENT", "TICKET"})
BOOKING_LOCATOR = RegexRule("BOOKING_CODE", re.compile(
    rf"{_BOOKING_KEY}(?:{_BOOKING_LABEL}{_BOOKING_SEP}|[ \t]*[:#][ \t]*)(?<![\w-])([A-Z]{{6}})(?![\w-])"), group=1,
    validate=lambda code: code not in _NOT_LOCATORS)

_MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_DATE = (rf"(?:\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}}|"
         rf"{_MONTHS}[ ]\d{{1,2}}(?:st|nd|rd|th)?,?[ ]\d{{4}}|\d{{1,2}}[ ]{_MONTHS}[ ]\d{{4}})")
DOB = RegexRule("DATE_OF_BIRTH", re.compile(
    rf"\b(?:dob|d\.o\.b\.?|date of birth|birth ?date|birthday|born(?: on)?)\b[^\n\d]{{0,15}}?({_DATE})", _I),
    group=1)

def _otp(text: str) -> Tuple[str, int]:
    return _replace_spans(text, otp.otp_spans(text), "ONE_TIME_CODE")


def _totp_secrets(text: str) -> Tuple[str, int]:
    return _replace_spans(text, otp.totp_secret_spans(text), "TOTP_SECRET")


def _backup_codes(text: str) -> Tuple[str, int]:
    return _replace_spans(text, otp.backup_code_spans(text), "BACKUP_CODE")


_STREET_SUFFIXES = [
    "Street", "St", "Avenue", "Ave", "Road", "Rd", "Boulevard", "Blvd", "Lane", "Ln", "Drive", "Dr",
    "Court", "Ct", "Place", "Pl", "Way", "Terrace", "Ter", "Circle", "Cir", "Parkway", "Pkwy",
    "Highway", "Hwy", "Square", "Sq", "Trail", "Trl", "Loop", "Alley", "Plaza", "Row", "Crescent",
]
_SUFFIX_ALT = "|".join(sorted({v for s in _STREET_SUFFIXES for v in (s, s.upper())}, key=len, reverse=True))
_STATES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|"
    "NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|PR"
)
_CITY_STATE_ZIP = rf"(?:[A-Z][A-Za-z.'-]*[ \t]){{0,2}}[A-Z][A-Za-z.'-]*,[ \t]*(?:{_STATES})[ \t]+\d{{5}}(?:-\d{{4}})?\b"
STREET_ADDRESS = RegexRule("STREET_ADDRESS", re.compile(
    rf"\b\d{{1,6}}[A-Za-z]?(?:-\d{{1,4}})?[ \t]+(?:[NSEW]\.?[ \t]+)?(?:[A-Za-z0-9][A-Za-z0-9.'-]*[ \t]+){{0,3}}?"
    rf"(?:{_SUFFIX_ALT})\b\.?"
    rf"(?:,?[ \t]*(?:Apt|Apartment|Suite|Ste|Unit|Fl|Floor|#)\.?[ \t]*#?[A-Za-z0-9-]+)?"
    rf"(?:,?\s*{_CITY_STATE_ZIP})?"
))
CITY_STATE_ZIP = RegexRule("STREET_ADDRESS", re.compile(rf"\b{_CITY_STATE_ZIP}"))
PO_BOX = RegexRule("STREET_ADDRESS", re.compile(r"\bP\.?[ ]?O\.?[ ]+Box[ ]+\d+\b", _I))


Redactor = Callable[[str], Tuple[str, int]]

# Order matters; see module docstring.
REDACTORS: List[Tuple[str, Redactor]] = [
    ("PRIVATE_KEY", PRIVATE_KEY),
    ("TOTP_SECRET", _totp_secrets),
    ("BACKUP_CODE", _backup_codes),
    ("ONE_TIME_CODE", _otp),   # early: codes must never be half-eaten by a looser rule
    ("JWT", JWT),
    ("API_KEY", API_KEY),
    ("API_KEY", AWS_SECRET),
    ("PASSWORD", URL_CREDENTIALS),
    ("PASSWORD", DIAL_IN_ONE_TAP),
    ("PASSWORD", DIAL_IN_PIN),
    ("PASSWORD", PASSWORD),
    ("IBAN", redact_ibans),
    ("CREDIT_CARD", redact_cards),
    ("CVV", CVV),
    ("SSN", SSN),
    ("SSN", SSN_CONTEXT),
    ("ROUTING_NUMBER", ROUTING),
    ("BANK_ACCOUNT", BANK_ACCOUNT),
    ("DATE_OF_BIRTH", DOB),
    ("BOOKING_CODE", BOOKING_CODE),   # before phones: a 10-digit hotel number isn't a phone
    ("BOOKING_CODE", BOOKING_LOCATOR),
    ("PHONE", redact_phones),
    ("STREET_ADDRESS", STREET_ADDRESS),
    ("STREET_ADDRESS", CITY_STATE_ZIP),
    ("STREET_ADDRESS", PO_BOX),
]


def redact(text: str) -> Tuple[str, Dict[str, int]]:
    counts: Dict[str, int] = {}
    for kind, fn in REDACTORS:
        text, n = fn(text)
        if n:
            counts[kind] = counts.get(kind, 0) + n
    return text, counts
