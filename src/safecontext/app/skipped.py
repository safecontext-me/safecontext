"""Showing a skipped item to the user, on demand, without storing it.

The scrubber skips some items whole (bank mail, security codes, health texts...), and the store
keeps only their id, thread, date and reason. When the user browses Not synced, this fetches those
items again from where they live (Gmail, Google Calendar and Dropbox over their read-only APIs;
Messages from chat.db on this Mac) so the user can see them, clearly marked as skipped.

What's fetched is unscrubbed, so it stays in this process's memory only:

  * never written to the store, the search index, a cache file or a log (log lines name the
    source and the error type, never content);
  * never reachable from an AI app: MCP tools read the store, and nothing here touches it;
  * held in two small in-memory caches (list-row previews and a few full items, so moving through
    the list feels instant) that `forget()` empties when the user leaves Not synced, and that
    disappear when the app quits.

Only items in the `dropped` table can be fetched, so this can't be used to read anything else.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import auth
from ..errors import NeedsPermission, TransientError
from ..scrub import RawItem
from ..scrub.normalize import html_to_text
from ..sources import Sources
from ..store import Store
from .mail import reason_info

log = logging.getLogger("safecontext.skipped")  # source ids and error types only, never content

PREVIEWS_KEPT = 400        # list rows: sender, title and a short snippet
FULL_KEPT = 16             # whole items: the one open and the few fetched ahead
PREVIEW_BATCH = 40         # most list rows one call fetches
SNIPPET_CHARS = 200
MAX_BODY_CHARS = 200_000   # a pathological message is shortened on screen; "Open in ..." has the rest

OPEN_IN = {"gmail": "Gmail", "gcal": "Google Calendar", "dropbox": "Dropbox", "imessage": "Messages"}


def _iso(ms: Optional[int]) -> Optional[str]:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _text(raw: RawItem) -> str:
    """The item's text as plain text. HTML is never rendered: it's reduced to text here, the same
    way the scrubber reads it, so no remote images, scripts or tracking pixels load."""
    if raw.text and raw.text.strip():
        return raw.text
    return html_to_text(raw.html) if raw.html else ""


def _preview(raw: RawItem) -> Dict[str, Any]:
    return {"sender": raw.sender, "title": raw.title,
            "snippet": " ".join(_text(raw).split())[:SNIPPET_CHARS]}


def _full(raw: RawItem, source: str, headers_only: bool) -> Dict[str, Any]:
    body = _text(raw)
    note = ""
    if headers_only:
        note = "Gmail is connected in headers-only mode, so only the sender, recipients and subject can be shown."
    elif source == "dropbox" and not body and raw.attachments:
        note = "Only text files can be shown here. Open it in Dropbox to see it."
    return {
        "id": raw.id, "kind": raw.kind, "thread_id": raw.thread_id,
        "date": _iso(raw.date_ms), "end": _iso(raw.end_ms),
        "sender": raw.sender, "participants": ", ".join(p for p in raw.participants if p),
        "title": raw.title, "body": body[:MAX_BODY_CHARS], "body_truncated": len(body) > MAX_BODY_CHARS,
        "attachments": [{"filename": a.filename, "mime_type": a.mime_type, "size": a.size} for a in raw.attachments],
        "details": dict(raw.extra), "link": raw.link, "note": note,
    }


class SkippedView:
    def __init__(self, store: Store, sources: Sources, reason_groups: Dict[str, str]) -> None:
        self.store, self.sources, self.groups = store, sources, reason_groups
        self._lock = threading.Lock()
        self._fetching: Dict[str, threading.Lock] = {}  # one fetch per source at a time
        self._previews: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()
        self._full: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()

    # --- the in-memory caches ------------------------------------------------------
    def _get(self, cache: "OrderedDict", key: Tuple[str, str]) -> Optional[Dict[str, Any]]:
        with self._lock:
            v = cache.get(key)
            if v is not None:
                cache.move_to_end(key)
            return v

    def _put(self, cache: "OrderedDict", key: Tuple[str, str], value: Dict[str, Any], keep: int) -> None:
        with self._lock:
            cache[key] = value
            cache.move_to_end(key)
            while len(cache) > keep:
                cache.popitem(last=False)

    def forget(self, source: str = "") -> None:
        """Drop everything fetched (or one source's): the user left Not synced, or disconnected it."""
        with self._lock:
            for cache in (self._previews, self._full):
                for k in [k for k in cache if not source or k[0] == source]:
                    del cache[k]

    def held(self) -> int:
        """How many items are in memory right now (for tests)."""
        with self._lock:
            return len(self._previews) + len(self._full)

    # --- fetching ------------------------------------------------------------------
    def _source_lock(self, source: str) -> threading.Lock:
        with self._lock:
            return self._fetching.setdefault(source, threading.Lock())

    def _fetch(self, source: str, ids: Sequence[str], full: bool) -> Tuple[Dict[str, Optional[RawItem]], str, Dict]:
        """(id -> RawItem or None, error message, error flags). Never raises."""
        src = self.sources.get(source)
        name = OPEN_IN.get(source, src.name)
        if not src.can_fetch_skipped:
            return {}, f"{src.name} items can't be shown here.", {}
        if not src.connected:
            return {}, f"Connect {src.name} again to see skipped items.", {"needs_reconnect": True}
        try:
            with self._source_lock(source):
                return src.fetch_skipped(list(ids), full=full), "", {}
        except auth.NeedsReconnect:
            return {}, f"Reconnect {src.name} to see this.", {"needs_reconnect": True}
        except NeedsPermission as e:
            return {}, e.message, {"needs_permission": True}
        except (TransientError, auth.TransientAuthError, OSError, TimeoutError):
            return {}, f"Couldn't reach {name}. Check your internet connection and try again.", {"offline": True}
        except auth.NotConfigured as e:
            return {}, str(e) or f"Couldn't reach {name}.", {}
        except Exception as e:  # the type only: an exception's text could carry content
            log.warning("fetching a skipped %s item failed: %s", source, type(e).__name__)
            return {}, f"Couldn't fetch it from {name} ({type(e).__name__}).", {}

    def item(self, source: str, item_id: str) -> Dict[str, Any]:
        """One skipped item, fetched live: {"skipped": the dropped row (reason, date), "item": the content
        or None, "error": why it couldn't be shown}. Callable from anywhere in the app (the Mail view,
        the Security Dashboard): it needs only the source and the item's id."""
        self.sources.get(source)
        row = self.store.dropped_entries(source, [item_id]).get(item_id)
        if row is None:
            raise auth.NotConfigured("That item isn't in Not synced (it may have been synced since).")
        row["reason_info"] = reason_info(str(row["reason"]), self.groups)
        key = (source, item_id)
        cached = self._get(self._full, key)
        if cached is not None:
            return {"skipped": row, "item": cached, "error": ""}
        found, error, flags = self._fetch(source, [item_id], full=True)
        if error:
            return {"skipped": row, "item": None, "error": error, **flags}
        raw = found.get(item_id)
        if raw is None:
            return {"skipped": row, "item": None, "gone": True,
                    "error": f"It's no longer in {OPEN_IN.get(source, source)}: it was deleted or moved."}
        src = self.sources.get(source)
        out = _full(raw, source, headers_only=source == "gmail" and getattr(src, "mode", "") == "headers")
        self._put(self._full, key, out, FULL_KEPT)
        self._put(self._previews, key, _preview(raw), PREVIEWS_KEPT)
        return {"skipped": row, "item": out, "error": ""}

    def previews(self, rows: Sequence[Dict[str, str]]) -> Dict[str, Any]:
        """Sender, title and a snippet for skipped list rows ({"source", "id"}), keyed "source:id".
        A row that couldn't be fetched gets {"error"}; one gone at the source gets {"gone": True}."""
        want: Dict[str, List[str]] = {}
        out: Dict[str, Dict[str, Any]] = {}
        for r in list(rows)[:PREVIEW_BATCH]:
            source, item_id = str(r.get("source") or ""), str(r.get("id") or "")
            if not source or not item_id:
                continue
            hit = self._get(self._previews, (source, item_id))
            if hit is not None:
                out[f"{source}:{item_id}"] = hit
            else:
                want.setdefault(source, []).append(item_id)
        for source, ids in want.items():
            self.sources.get(source)
            known = self.store.dropped_entries(source, ids)  # only skipped items, never anything else
            ids = [i for i in ids if i in known]
            if not ids:
                continue
            found, error, _ = self._fetch(source, ids, full=False)
            for i in ids:
                k = f"{source}:{i}"
                if error:
                    out[k] = {"error": error}
                elif found.get(i) is None:
                    out[k] = {"gone": True}
                else:
                    p = _preview(found[i])
                    self._put(self._previews, (source, i), p, PREVIEWS_KEPT)
                    out[k] = p
        return {"previews": out}
