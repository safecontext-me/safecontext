"""The one entry point: RawMessage in, ScrubbedMessage or Dropped out.

Everything here runs in memory. Callers persist only the return value.
"""
from __future__ import annotations

import os
from typing import Dict, Optional, Union

from .drop import DEFAULT_CONFIG, DropConfig, check_headers, check_security_body
from .models import Attachment, Dropped, RawMessage, ScrubbedMessage
from .normalize import html_to_text, normalize_text
from .prose import NoProseClassifier, ProseClassifier, redact_prose
from .quotes import strip_quotes_and_signature
from .redact import redact

# Bump whenever rules change. Raw mail is never kept, so messages scrubbed
# under an older version can only be re-scrubbed by re-fetching them.
SCRUBBER_VERSION = "1"

Result = Union[ScrubbedMessage, Dropped]


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
