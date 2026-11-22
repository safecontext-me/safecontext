"""2FA / one-time-code detection, shared by the drop layer and the redactor.

This is the highest-priority rule set. It covers:
  * codes after a keyword       "Your code is 482913", "Verification code:\\n\\n482 913"
  * codes before a keyword      "482913 is your Instagram code", "G-482913 is your Google ..."
  * "use/enter X to verify"     "Enter 4829 to sign in"
  * alphanumeric codes          "K7X-9QP", "AB12CD"
  * authenticator setup keys    otpauth:// URLs, base32 "secret key: JBSW Y3DP EHPK 3PXP"
  * backup / recovery code lists
"""
from __future__ import annotations

import re
from typing import List, Tuple

Span = Tuple[int, int]

# A code: 6 digits split 3+3, 4-8 digits, XXXX-XXXX, or 5-8 uppercase alnum containing a digit and a letter.
CODE = (
    r"(?:\d{3}[ -]\d{3}|\d{4,8}|[A-Z0-9]{3,4}-[A-Z0-9]{3,4}|"
    r"(?=[A-Z0-9]{0,7}\d)(?=[A-Z0-9]{0,7}[A-Z])[A-Z0-9]{5,8})"
)
_KW_BEFORE = r"(?i:\b(?:code|otp|passcode|pin|token|2fa|mfa|verification)\b)"
_KW_AFTER = r"(?i:\b(?:code|otp|passcode|pin|password)\b)"
# A code must stand alone: not one group of a longer digit run (card, phone, account).
_PRE = r"(?<![\w-])(?<!\d[ .])"
_POST = r"(?![\w-])(?![ .]\d)"

# Gap between keyword and code: no digits, no redaction tokens, at most two line breaks.
_GAP = r"[^\d\[\n]{0,40}?(?:\n[^\d\[\n]{0,40}?){0,2}"

_AFTER_KEYWORD = re.compile(rf"{_KW_BEFORE}{_GAP}{_PRE}({CODE}){_POST}")
_BEFORE_KEYWORD = re.compile(
    rf"{_PRE}((?:G-)?{CODE}){_POST}[ \t]*(?i:is|are|=|:)[ \t]+(?i:your|the|ur)\b[^\n]{{0,30}}?{_KW_AFTER}")
_USE_TO_VERIFY = re.compile(
    rf"(?i:\b(?:use|enter|type)\b)[ \t]+(?:(?i:the )?(?i:code )?){_PRE}({CODE}){_POST}[ \t]+"
    r"(?i:to|as|for)\b[^\n]{0,40}?(?i:verif|sign|log|confirm|authenticat|account|continue|reset)")
_GOOGLE = re.compile(r"\bG-\d{5,8}\b")

_OTPAUTH = re.compile(r"otpauth(?:-migration)?://[^\s\"'<>]+", re.I)
_SETUP_KEY = re.compile(
    r"(?i:\b(?:secret(?: key)?|setup key|set-?up code|manual(?:ly)?(?: entry)? key|authenticator key|"
    r"2fa key|totp(?: secret| key)?|seed)\b)[^\n\w]{0,10}"
    r"((?:[A-Za-z2-7]{4}[ ]?){3,7}[A-Za-z2-7]{2,4}|[A-Z2-7]{16,64})")

_BACKUP_HEADER = re.compile(r"(?i)\b(?:backup|recovery|scratch|emergency) (?:verification )?codes?\b")
_BACKUP_ITEM = re.compile(r"(?<![\w-])(?:\d{4}[ -]?\d{4}|\d{6,10}|[a-z0-9]{4,5}-[a-z0-9]{4,5})(?![\w-])")


def _is_base32_key(s: str) -> bool:
    compact = s.replace(" ", "")
    return len(compact) >= 16 and (bool(re.search(r"[2-7]", compact)) or compact.isupper())


def otp_spans(text: str) -> List[Span]:
    spans: List[Span] = []
    for pat in (_AFTER_KEYWORD, _BEFORE_KEYWORD, _USE_TO_VERIFY):
        spans += [m.span(1) for m in pat.finditer(text)]
    spans += [m.span() for m in _GOOGLE.finditer(text)]
    return spans


def totp_secret_spans(text: str) -> List[Span]:
    spans = [m.span() for m in _OTPAUTH.finditer(text)]
    spans += [m.span(1) for m in _SETUP_KEY.finditer(text) if _is_base32_key(m.group(1))]
    return spans


def backup_code_spans(text: str) -> List[Span]:
    spans: List[Span] = []
    for h in _BACKUP_HEADER.finditer(text):
        window_end = min(len(text), h.end() + 600)
        spans += [(h.end() + m.start(), h.end() + m.end())
                  for m in _BACKUP_ITEM.finditer(text[h.end():window_end])]
    return spans


# --- whole-message signals for the drop layer ------------------------------

_STRONG_KW_CODE = re.compile(
    r"(?i:\b(?:verification|security|login|log-in|sign[- ]?in|one[- ]time|confirmation|authentication|"
    r"access|2fa|mfa|2-step|two[- ]factor|single[- ]use|magic)\b[ \t]*(?:code|passcode|password|pin|otp)\b)"
    rf"{_GAP}{_PRE}{CODE}{_POST}")


def looks_like_security_mail(text: str) -> bool:
    return bool(
        _STRONG_KW_CODE.search(text)
        or _BEFORE_KEYWORD.search(text)
        or _USE_TO_VERIFY.search(text)
        or _GOOGLE.search(text)
        or _OTPAUTH.search(text)
        or (_BACKUP_HEADER.search(text) and backup_code_spans(text))
    )
