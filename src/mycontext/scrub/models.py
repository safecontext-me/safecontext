"""Data shapes for the scrubber.

`RawMessage` exists only in memory, between the Gmail fetch and `scrub()`.
It must never be written, logged, or pickled. Attachments carry metadata
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


@dataclass(frozen=True)
class Dropped:
    """A message excluded entirely. Holds no content, only why."""

    id: str
    thread_id: str
    reason: str
    date_ms: int = 0
    detail: Optional[str] = None
