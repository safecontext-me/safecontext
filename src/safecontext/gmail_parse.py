"""Turn a Gmail API message resource (format=full or format=metadata) into
an in-memory RawMessage.

Attachments: only filename, MIME type and size are read. Gmail returns
attachment bytes by reference (attachmentId) and we never request them;
if a small part arrives inline with data, that data is never decoded.
"""
from __future__ import annotations

import base64
import binascii
import quopri
import re
from typing import Dict, List, Set, Tuple

from .scrub.models import Attachment, RawMessage

MAX_TEXT_CHARS = 2_000_000  # cap pathological bodies before scrubbing

_TEXT_TYPES = {"text/plain", "text/html"}


def _headers(part: dict) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for h in part.get("headers") or []:
        name = (h.get("name") or "").lower()
        if name and name not in out:  # first occurrence wins
            out[name] = h.get("value") or ""
    return out


def _charset(content_type: str) -> str:
    m = re.search(r"charset\s*=\s*\"?([\w.:-]+)", content_type or "", re.I)
    return m.group(1) if m else "utf-8"


def _decode_text(part: dict, headers: Dict[str, str]) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    # Gmail normally removes the transfer encoding; undo it ourselves only
    # if quoted-printable markers clearly survived.
    if (headers.get("content-transfer-encoding", "").lower() == "quoted-printable"
            and re.search(rb"=\r?\n|=[0-9A-F]{2}", raw)):
        raw = quopri.decodestring(raw)
    charset = _charset(headers.get("content-type", ""))
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    return text[:MAX_TEXT_CHARS]


def _walk(part: dict, texts: List[str], htmls: List[str], attachments: List[Attachment], depth: int = 0) -> None:
    if depth > 20:
        return
    headers = _headers(part)
    mime = (part.get("mimeType") or "").lower()
    filename = part.get("filename") or ""
    body = part.get("body") or {}
    disposition = headers.get("content-disposition", "").lower()

    is_attachment = bool(filename) or bool(body.get("attachmentId")) or disposition.startswith("attachment")
    if is_attachment or (mime not in _TEXT_TYPES and not mime.startswith("multipart/") and mime):
        # Metadata only. Covers files, inline images, calendar invites, forwarded .eml, signatures.
        attachments.append(Attachment(filename=filename or "(unnamed)", mime_type=mime or "application/octet-stream",
                                      size=int(body.get("size") or 0)))
        return
    if mime == "text/plain":
        texts.append(_decode_text(part, headers))
    elif mime == "text/html":
        htmls.append(_decode_text(part, headers))
    for child in part.get("parts") or []:
        _walk(child, texts, htmls, attachments, depth + 1)


def parse_gmail_message(resource: dict, metadata_only: bool = False) -> RawMessage:
    payload = resource.get("payload") or {}
    texts: List[str] = []
    htmls: List[str] = []
    attachments: List[Attachment] = []
    if not metadata_only:
        _walk(payload, texts, htmls, attachments)
    return RawMessage(
        id=resource["id"],
        thread_id=resource.get("threadId") or resource["id"],
        label_ids=list(resource.get("labelIds") or []),
        headers=_headers(payload),
        text_body="\n\n".join(t for t in texts if t.strip()),
        html_body="\n".join(h for h in htmls if h.strip()),
        attachments=attachments,
        internal_date_ms=int(resource.get("internalDate") or 0),
    )


UNREAD = "UNREAD"


class HistoryChanges:
    """What history.list pages say changed, in order. A message whose only change was its UNREAD
    label goes to `unread` (id -> unread now) and needs no fetch; anything else that touched it
    (added, another label, ...) puts it in `changed`, and the fetch brings its read state along."""

    def __init__(self) -> None:
        self.changed: Set[str] = set()
        self.deleted: Set[str] = set()
        self.unread: Dict[str, bool] = {}

    def add_page(self, page: dict) -> None:
        for h in page.get("history") or []:
            for item in h.get("messagesAdded") or []:
                self.changed.add(item["message"]["id"])
            for key, now in (("labelsAdded", True), ("labelsRemoved", False)):
                for item in h.get(key) or []:
                    mid = item["message"]["id"]
                    if set(item.get("labelIds") or ()) == {UNREAD}:
                        self.unread[mid] = now  # later records win: history comes oldest first
                    else:
                        self.changed.add(mid)
            for item in h.get("messagesDeleted") or []:
                self.deleted.add(item["message"]["id"])

    def unread_only(self) -> Dict[str, bool]:
        """Read-state changes for messages that need nothing else."""
        return {m: u for m, u in self.unread.items() if m not in self.changed and m not in self.deleted}


def history_message_ids(page: dict) -> Tuple[Set[str], Set[str]]:
    """From one history.list page, return (changed_ids, deleted_ids)."""
    changed: Set[str] = set()
    deleted: Set[str] = set()
    for h in page.get("history") or []:
        for item in h.get("messagesAdded") or []:
            changed.add(item["message"]["id"])
        for key in ("labelsAdded", "labelsRemoved"):
            for item in h.get(key) or []:
                changed.add(item["message"]["id"])
        for item in h.get("messagesDeleted") or []:
            deleted.add(item["message"]["id"])
    return changed, deleted

