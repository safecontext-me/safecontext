"""The entry points: RawMessage (Gmail) or RawItem (any other source) in,
ScrubbedMessage / ScrubbedItem or Dropped out.

Everything here runs in memory. Callers persist only the return value.
"""
from __future__ import annotations

import os
from typing import Dict, Optional, Union

from .drop import DEFAULT_CONFIG, DropConfig, check_headers, check_security_body, check_sensitive_text, rules_for
from .models import KIND_MESSAGE, KINDS, Attachment, Dropped, RawItem, RawMessage, ScrubbedItem, ScrubbedMessage
from .normalize import html_to_text, normalize_text
from .prose import NoProseClassifier, ProseClassifier, redact_prose
from .quotes import strip_quotes_and_signature
from .redact import redact

# Bump whenever rules change. Raw mail is never kept, so messages scrubbed
# under an older version can only be re-scrubbed by re-fetching them.
SCRUBBER_VERSION = "1"

Result = Union[ScrubbedMessage, Dropped]
ItemResult = Union[ScrubbedItem, Dropped]


def _merge(into: Dict[str, int], more: Dict[str, int]) -> None:
    for k, v in more.items():
        into[k] = into.get(k, 0) + v


def _scrub_filename(name: str, counts: Dict[str, int]) -> str:
    stem, ext = os.path.splitext(normalize_text(name))
    # Underscores glue tokens together ("SSN_123-45-6789"); treat them as spaces.
    clean, c = redact(stem.replace("_", " "))
    _merge(counts, c)
    return clean + ext


def scrub(
    msg: RawMessage,
    config: DropConfig = DEFAULT_CONFIG,
    prose: Optional[ProseClassifier] = None,
) -> Result:
    subject = normalize_text(msg.header("subject"))

    reason = check_headers(msg, subject, config)
    if reason:
        return Dropped(msg.id, msg.thread_id, reason, msg.internal_date_ms)

    body = msg.text_body if msg.text_body.strip() else html_to_text(msg.html_body)
    body = normalize_text(body)

    reason = check_security_body(body)
    if reason:
        return Dropped(msg.id, msg.thread_id, reason, msg.internal_date_ms)

    body = strip_quotes_and_signature(body)

    counts: Dict[str, int] = {}
    body, c = redact(body)
    _merge(counts, c)
    subject, c = redact(subject)
    _merge(counts, c)

    body, c = redact_prose(body, prose or NoProseClassifier())
    _merge(counts, c)

    attachments = [
        Attachment(_scrub_filename(a.filename, counts), a.mime_type, int(a.size))
        for a in msg.attachments
    ]

    return ScrubbedMessage(
        id=msg.id,
        thread_id=msg.thread_id,
        date_ms=msg.internal_date_ms,
        sender=msg.header("from"),
        to=msg.header("to"),
        cc=msg.header("cc"),
        subject=subject,
        body=body,
        message_id_header=msg.header("message-id"),
        attachments=attachments,
        redactions=counts,
        scrubber_version=SCRUBBER_VERSION,
    )


def scrub_item(
    item: RawItem,
    config: DropConfig = DEFAULT_CONFIG,
    prose: Optional[ProseClassifier] = None,
    *,
    strip_quotes: bool = True,
) -> ItemResult:
    """Scrub one item from any non-Gmail source. Same layers as `scrub`:
    drop rules for the item's kind (see drop.rules_for), then inline redaction
    of everything written by a person: title, text, extra fields and
    attachment names. Quote and signature stripping is for email only, and
    `strip_quotes=False` skips it for a "message" that is really a document (a
    Dropbox file), where a "--" line or a "> " blockquote is content.
    Raises ValueError for a kind the drop rules don't know, so no item
    skips the rules meant for it."""
    if item.kind not in KINDS:
        raise ValueError(f"unknown item kind {item.kind!r}")
    title = normalize_text(item.title)

    reason = check_headers(item, title, config, kind=item.kind)
    if reason:
        return Dropped(item.id, item.thread_id, reason, item.date_ms)

    body = item.text if item.text.strip() else html_to_text(item.html)
    body = normalize_text(body)
    extra = {normalize_text(k): normalize_text(v) for k, v in item.extra.items() if v and v.strip()}

    reason = check_security_body("\n".join([body, *extra.values()]))
    if not reason and "sensitive_text" in rules_for(item.kind):
        reason = check_sensitive_text(body, config)
    if reason:
        return Dropped(item.id, item.thread_id, reason, item.date_ms)

    if item.kind == KIND_MESSAGE and strip_quotes:
        body = strip_quotes_and_signature(body)

    counts: Dict[str, int] = {}
    body, c = redact(body)
    _merge(counts, c)
    title, c = redact(title)
    _merge(counts, c)
    clean_extra: Dict[str, str] = {}
    for k, v in extra.items():
        v, c = redact(v)
        _merge(counts, c)
        clean_extra[k] = v

    body, c = redact_prose(body, prose or NoProseClassifier())
    _merge(counts, c)

    attachments = [
        Attachment(_scrub_filename(a.filename, counts), a.mime_type, int(a.size))
        for a in item.attachments
    ]

    return ScrubbedItem(
        id=item.id,
        kind=item.kind,
        thread_id=item.thread_id,
        date_ms=item.date_ms,
        end_ms=item.end_ms,
        sender=item.sender,
        participants=list(item.participants),
        title=title,
        body=body,
        link=item.link,
        attachments=attachments,
        extra=clean_extra,
        redactions=counts,
        scrubber_version=SCRUBBER_VERSION,
    )
