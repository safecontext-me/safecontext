"""In-memory Gmail API that behaves like the real one where sync cares:
paged listing, history records, expiring history ids, deletions between
list and get, revoked tokens, and injectable failures."""
from __future__ import annotations

import base64
from collections import Counter
from typing import Callable, Dict, List, Optional

from safecontext.auth import NeedsReconnect
from safecontext.gmail_api import HistoryExpired, TransientError
from safecontext.scrub import RawMessage

_EXCLUDED_BY_QUERY = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS", "DRAFT", "CHAT"}


def _b64(text: str, charset: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(text.encode(charset)).decode().rstrip("=")


def resource_from(m: RawMessage) -> dict:
    """Encode a synthetic RawMessage the way Gmail's format=full does."""
    headers = [{"name": k.title() if k != "message-id" else "Message-ID", "value": v} for k, v in m.headers.items()]
    alt = []
    if m.text_body:
        alt.append({"partId": "0.0", "mimeType": "text/plain", "filename": "",
                    "headers": [{"name": "Content-Type", "value": "text/plain; charset=UTF-8"}],
                    "body": {"size": len(m.text_body), "data": _b64(m.text_body)}})
    if m.html_body:
        alt.append({"partId": "0.1", "mimeType": "text/html", "filename": "",
                    "headers": [{"name": "Content-Type", "value": "text/html; charset=UTF-8"}],
                    "body": {"size": len(m.html_body), "data": _b64(m.html_body)}})
    parts = [{"partId": "0", "mimeType": "multipart/alternative", "filename": "", "headers": [],
              "body": {"size": 0}, "parts": alt}]
    for n, a in enumerate(m.attachments, start=1):
        parts.append({"partId": str(n), "mimeType": a.mime_type, "filename": a.filename,
                      "headers": [{"name": "Content-Disposition", "value": f'attachment; filename="{a.filename}"'}],
                      "body": {"size": a.size, "attachmentId": f"att-{m.id}-{n}"}})
    return {
        "id": m.id, "threadId": m.thread_id, "labelIds": list(m.label_ids),
        "snippet": (m.text_body or m.html_body)[:100],  # Gmail's snippet is raw text; we must ignore it
        "internalDate": str(m.internal_date_ms), "historyId": "0",
        "payload": {"partId": "", "mimeType": "multipart/mixed", "filename": "", "headers": headers,
                    "body": {"size": 0}, "parts": parts},
    }


class FakeGmail:
    def __init__(self, page_size: int = 4, email: str = "alex@example.org") -> None:
        self.email = email
        self.page_size = page_size
        self.messages: Dict[str, dict] = {}
        self.history: List[dict] = []
        self.history_id = 5000
        self.oldest_history_id = 5000
        self.revoked = False
        self.calls: Counter = Counter()
        self.fetched: Counter = Counter()          # successful gets per id
        self.formats: Counter = Counter()
        self.queries: List[Optional[str]] = []
        self.fail_list_call: Optional[int] = None  # raise TransientError on this list call number
        self.fail_get_call: Optional[int] = None
        self.after_list: Optional[Callable[[int], None]] = None
        self.before_get: Optional[Callable[[List[str]], None]] = None

    # --- mailbox mutations (each records history like Gmail) -------------
    def _record(self, **item) -> None:
        self.history_id += 1
        self.history.append({"id": str(self.history_id), **item})

    def add(self, m: RawMessage, *, record: bool = True) -> None:
        res = resource_from(m)
        self.messages[m.id] = res
        if record:
            self._record(messagesAdded=[{"message": {"id": m.id, "threadId": m.thread_id}}])

    def delete(self, mid: str, *, record: bool = True) -> None:
        self.messages.pop(mid, None)
        if record:
            self._record(messagesDeleted=[{"message": {"id": mid}}])

    def relabel(self, mid: str, add=(), remove=()) -> None:
        labels = self.messages[mid]["labelIds"]
        for lbl in add:
            if lbl not in labels:
                labels.append(lbl)
        for lbl in remove:
            if lbl in labels:
                labels.remove(lbl)
        if add:
            self._record(labelsAdded=[{"message": {"id": mid}, "labelIds": list(add)}])
        if remove:
            self._record(labelsRemoved=[{"message": {"id": mid}, "labelIds": list(remove)}])

    def expire_history(self) -> None:
        self.oldest_history_id = self.history_id

    # --- GmailAPI interface ------------------------------------------------
    def _check_auth(self) -> None:
        if self.revoked:
            raise NeedsReconnect("revoked_or_expired")

    def get_profile(self) -> dict:
        self._check_auth()
        self.calls["profile"] += 1
        return {"emailAddress": self.email, "historyId": str(self.history_id), "messagesTotal": len(self.messages)}

    def list_message_ids(self, page_token, query):
        self._check_auth()
        self.calls["list"] += 1
        self.queries.append(query)
        if self.fail_list_call == self.calls["list"]:
            raise TransientError("simulated network drop")
        visible = []
        for res in self.messages.values():
            labels = set(res["labelIds"])
            if labels & {"SPAM", "TRASH"}:
                continue  # includeSpamTrash=False
            if query and labels & _EXCLUDED_BY_QUERY:
                continue
            visible.append(res)
        visible.sort(key=lambda r: int(r["internalDate"]), reverse=True)
        start = int(page_token or 0)
        page = visible[start:start + self.page_size]
        nxt = str(start + self.page_size) if start + self.page_size < len(visible) else None
        if self.after_list:
            self.after_list(self.calls["list"])
        return [r["id"] for r in page], nxt

    def list_unread_ids(self, page_token):
        self._check_auth()
        self.calls["list_unread"] += 1
        unread = sorted((r for r in self.messages.values()
                         if "UNREAD" in r["labelIds"] and not set(r["labelIds"]) & {"SPAM", "TRASH"}),
                        key=lambda r: int(r["internalDate"]), reverse=True)
        start = int(page_token or 0)
        page = unread[start:start + self.page_size]
        nxt = str(start + self.page_size) if start + self.page_size < len(unread) else None
        return [r["id"] for r in page], nxt

    def get_messages(self, ids, fmt):
        self._check_auth()
        self.calls["get_batch"] += 1
        if self.fail_get_call == self.calls["get_batch"]:
            raise TransientError("simulated batch failure")
        if self.before_get:
            self.before_get(list(ids))
        out = {}
        for mid in ids:
            res = self.messages.get(mid)
            if res is None:
                out[mid] = None
                continue
            self.fetched[mid] += 1
            self.formats[fmt] += 1
            if fmt == "metadata":
                res = {**res, "payload": {"headers": res["payload"]["headers"], "mimeType": "multipart/mixed"}}
            out[mid] = res
        return out

    def list_history(self, start_history_id, page_token):
        self._check_auth()
        self.calls["history"] += 1
        if int(start_history_id) < self.oldest_history_id:
            raise HistoryExpired()
        records = [h for h in self.history if int(h["id"]) > int(start_history_id)]
        start = int(page_token or 0)
        page = records[start:start + self.page_size]
        resp = {"history": page, "historyId": str(self.history_id)}
        if start + self.page_size < len(records):
            resp["nextPageToken"] = str(start + self.page_size)
        return resp
