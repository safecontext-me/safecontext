"""Data shapes for the scrubber.

`RawMessage` (Gmail) and `RawItem` (every other source) exist only in
memory, between the fetch and `scrub()` / `scrub_item()`. They must never
be written, logged, or pickled. Attachments carry metadata
only: there is deliberately no field that could hold attachment bytes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Attachment:
    filename: str
    mime_type: str
    size: int


@dataclass
class RawMessage:
    id: str
    thread_id: str
    label_ids: List[str]
    headers: Dict[str, str]  # keys lower-cased
    text_body: str = ""
    html_body: str = ""
    attachments: List[Attachment] = field(default_factory=list)
    internal_date_ms: int = 0

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")

    def __repr__(self) -> str:  # keep raw content out of tracebacks and logs
        return f"RawMessage(id={self.id!r}, <content hidden>)"


@dataclass
class ScrubbedMessage:
    id: str
    thread_id: str
    date_ms: int
    sender: str
    to: str
    cc: str
    subject: str
    body: str
    message_id_header: str
    attachments: List[Attachment]
    redactions: Dict[str, int]
    scrubber_version: str


# Kinds of item a source can produce. "message" means email: only email gets
# the email-only drop rules (labels, List-Unsubscribe, no-reply senders).
KIND_MESSAGE, KIND_EVENT, KIND_CHAT, KIND_TRANSCRIPT = "message", "event", "chat", "transcript"
KINDS = (KIND_MESSAGE, KIND_EVENT, KIND_CHAT, KIND_TRANSCRIPT)


@dataclass
class RawItem:
    """One unscrubbed item from any source other than Gmail (a calendar event,
    a chat message, a call transcript, mail from another provider).

    `sender` and `participants` are identities (names, addresses, handles) and
    are kept as-is, like an email's From and To. Everything a person wrote
    (title, text, html, extra values, attachment names) is scrubbed.
    `extra` holds short labelled fields shown with the item, e.g.
    {"Location": ..., "Join": ...}. `headers` and `labels` are only read by
    the email-only rules, for email-like sources.
    """

    id: str
    kind: str
    thread_id: str
    date_ms: int
    end_ms: Optional[int] = None
    sender: str = ""
    participants: List[str] = field(default_factory=list)
    title: str = ""
    text: str = ""
    html: str = ""
    link: str = ""
    attachments: List[Attachment] = field(default_factory=list)
    extra: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)  # keys lower-cased
    labels: List[str] = field(default_factory=list)

    @property
    def label_ids(self) -> List[str]:  # same shape as RawMessage for the label rule
        return self.labels

    def header(self, name: str) -> str:
        name = name.lower()
        if name == "from" and name not in self.headers:
            return self.sender
        return self.headers.get(name, "")

    def __repr__(self) -> str:
        return f"RawItem(id={self.id!r}, kind={self.kind!r}, <content hidden>)"


@dataclass
class ScrubbedItem:
    id: str
    kind: str
    thread_id: str
    date_ms: int
    end_ms: Optional[int]
    sender: str
    participants: List[str]
    title: str
    body: str
    link: str
    attachments: List[Attachment]
    extra: Dict[str, str]
    redactions: Dict[str, int]
    scrubber_version: str


@dataclass(frozen=True)
class Dropped:
    """A message excluded entirely. Holds no content, only why."""

    id: str
    thread_id: str
    reason: str
    date_ms: int = 0
    detail: Optional[str] = None
