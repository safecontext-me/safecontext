"""What the scrubber removes from text extracted out of a file or PDF.

Built-in rules always run, and they are the same rules mail and calendar use.
The user can see those rules and add patterns of their own. Extra patterns are
stored on this Mac and applied only by `scrub_file_text`, so the mail and
calendar scrubber keeps behaving exactly as it does today.

Nothing in here logs the text or the match. A pattern is a regular expression
the user typed; the matched characters are replaced with [REDACTED:KIND] and
then discarded.
"""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from ..store import Store
from .redact import RegexRule, redact

# One row per kind, in the order the redactors run. The sentences are what the
# app shows; the expressions themselves stay in redact.py.
BUILTIN: Tuple[Tuple[str, str, str], ...] = (
    ("PRIVATE_KEY", "Private keys", "A private key block."),
    ("TOTP_SECRET", "Authenticator keys", "The secret used to set up an authenticator app."),
    ("BACKUP_CODE", "Backup codes", "A list of one-time backup codes."),
    ("ONE_TIME_CODE", "2FA / one-time codes", "A code from a sign-in or verification message."),
    ("JWT", "Login tokens", "A JSON web token."),
    ("API_KEY", "API keys", "A token or key for an API, including cloud and payment keys."),
    ("PASSWORD", "Passwords", "A password written next to a label such as “password”."),
    ("IBAN", "IBANs", "An international bank account number."),
    ("CREDIT_CARD", "Card numbers", "A payment card number."),
    ("CVV", "Card security codes", "A card security code next to a label such as CVV."),
    ("SSN", "Social Security numbers", "A Social Security number, with dashes or the words in front of it."),
    ("ROUTING_NUMBER", "Routing numbers", "A bank routing number next to a label such as routing."),
    ("BANK_ACCOUNT", "Bank account numbers", "An account number next to a label such as account."),
    ("DATE_OF_BIRTH", "Dates of birth", "A date next to a label such as date of birth."),
    ("BOOKING_CODE", "Booking and confirmation codes", "A confirmation or record-locator code."),
    ("PHONE", "Phone numbers", "A phone number."),
    ("STREET_ADDRESS", "Street addresses", "A street address, a PO box, or a city, state, and ZIP."),
)

_SOURCE = "scrubber"
_KEY = "file_patterns"
MAX_PATTERNS = 20
MAX_NAME = 40
MAX_PATTERN = 200
MAX_PREVIEW = 4000


class PatternError(Exception):
    """The user typed a pattern SafeContext will not keep. `message` is safe to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class UserPattern:
    id: str
    name: str
    kind: str
    pattern: str
    enabled: bool = True

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind,
                "pattern": self.pattern, "enabled": self.enabled}


def builtin_rules() -> List[dict]:
    """The rules that always run, in the order they run. Nothing here is editable."""
    return [{"kind": kind, "label": label, "detail": detail, "enabled": True, "builtin": True}
            for kind, label, detail in BUILTIN]


def scrub_file_text(text: str, patterns: Sequence[UserPattern] = ()) -> Tuple[str, Dict[str, int]]:
    """Scrub text that was extracted from a PDF or another file on this Mac.

    Built-in rules run first. Each enabled user pattern runs after them. A
    pattern that no longer compiles is skipped, so one bad row cannot stop
    the rest or leak the unscrubbed text into an error.
    """
    cleaned, counts = redact(text or "")
    for pattern in patterns:
        if not pattern.enabled:
            continue
        try:
            rx = re.compile(pattern.pattern)
        except re.error:
            continue
        if rx.match(""):
            continue
        cleaned, n = RegexRule(pattern.kind, rx)(cleaned)
        if n:
            counts[pattern.kind] = counts.get(pattern.kind, 0) + n
    return cleaned, counts


def load_patterns(store: Store) -> List[UserPattern]:
    raw = store.state(_SOURCE).get(_KEY)
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out = []
    if not isinstance(rows, list):
        return []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            out.append(UserPattern(
                id=str(row["id"]), name=str(row["name"]), kind=str(row["kind"]),
                pattern=str(row["pattern"]), enabled=bool(row.get("enabled", True))))
        except (KeyError, TypeError):
            continue
    return out


def add_pattern(store: Store, name: str, pattern: str) -> UserPattern:
    rows = load_patterns(store)
    if len(rows) >= MAX_PATTERNS:
        raise PatternError(f"You can add up to {MAX_PATTERNS} patterns.")
    cleaned_name, kind = _name_and_kind(name)
    compiled = _compile(pattern)
    if any(row.pattern == compiled for row in rows):
        raise PatternError("That pattern is already in the list.")
    created = UserPattern(id=secrets.token_hex(4), name=cleaned_name, kind=kind,
                          pattern=compiled, enabled=True)
    rows.append(created)
    _save(store, rows)
    return created


def set_enabled(store: Store, pattern_id: str, enabled: bool) -> None:
    rows = load_patterns(store)
    found = False
    updated = []
    for row in rows:
        if row.id == pattern_id:
            updated.append(UserPattern(row.id, row.name, row.kind, row.pattern, bool(enabled)))
            found = True
        else:
            updated.append(row)
    if not found:
        raise PatternError("That pattern is no longer in the list.")
    _save(store, updated)


def delete_pattern(store: Store, pattern_id: str) -> None:
    rows = load_patterns(store)
    kept = [row for row in rows if row.id != pattern_id]
    if len(kept) == len(rows):
        raise PatternError("That pattern is no longer in the list.")
    _save(store, kept)


def preview(store: Store, text: str) -> Tuple[str, Dict[str, int], bool]:
    """Scrub a sample the user typed. The sample is not stored."""
    if not isinstance(text, str) or not text.strip():
        raise PatternError("Type a sample first.")
    truncated = len(text) > MAX_PREVIEW
    cleaned, counts = scrub_file_text(text[:MAX_PREVIEW], load_patterns(store))
    return cleaned, counts, truncated


def _save(store: Store, rows: Sequence[UserPattern]) -> None:
    store.state(_SOURCE).set(_KEY, json.dumps([row.as_dict() for row in rows]))


def _name_and_kind(name: str) -> Tuple[str, str]:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise PatternError("Give the pattern a name, such as Employee ID.")
    if len(cleaned) > MAX_NAME:
        raise PatternError(f"Keep the name to {MAX_NAME} characters.")
    kind = re.sub(r"[^A-Za-z0-9]+", "_", cleaned).strip("_").upper()[:24]
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,23}", kind or ""):
        raise PatternError("Start the name with a letter, such as Employee ID.")
    return cleaned, kind


def _compile(pattern: str) -> str:
    pattern = (pattern or "").strip()
    if not pattern:
        raise PatternError("Enter a pattern, such as EMP-\\d{4}.")
    if len(pattern) > MAX_PATTERN:
        raise PatternError(f"Keep the pattern to {MAX_PATTERN} characters.")
    if pattern.count("(") > 12 or len(re.findall(r"[+*]|\{\d", pattern)) > 8 or re.search(r"\)[+*{]", pattern):
        raise PatternError("That pattern is too open-ended. Use a tighter one.")
    try:
        rx = re.compile(pattern)
    except re.error:
        raise PatternError("That pattern isn't a valid regular expression.") from None
    if rx.match(""):
        raise PatternError("That pattern matches empty text, so it would blank whole files.")
    return pattern
