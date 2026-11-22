"""Backfill the whole mailbox, then keep it current with history.list.

Flow of one message: Gmail API -> parse (memory) -> scrub (memory) -> Store.
Nothing unscrubbed is ever persisted or logged.

Resumability: the backfill saves its page token only after a page's
messages are stored, and skips ids already stored under the current
scrubber version, so an interrupted run picks up where it stopped. The
history id is recorded *before* the backfill starts, so mail that changes
during a long backfill is caught by the first incremental sync.

Sync state lives in Store meta:
  account_email, sync_mode ('full'|'headers'),
  backfill_start_history_id, backfill_page_token, resync (in progress),
  history_id (set once backfill completes), auth_state, last_sync_ms
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .auth import NeedsReconnect
from .gmail_api import FULL_MODE_QUERY, GmailAPI, HistoryExpired, TransientError
from .gmail_parse import history_message_ids, parse_gmail_message
from .scrub import Dropped, scrub
from .store import Store

log = logging.getLogger("mycontext.sync")  # ids and counts only, never content


@dataclass
class SyncResult:
    status: str                 # "ok" | "needs_reconnect" | "retry_later"
    phase: str = ""             # "backfill" | "incremental" | "resync"
    message: str = ""
    fetched: int = 0
    kept: int = 0
    dropped: int = 0
    deleted: int = 0
    errors: Dict[str, int] = field(default_factory=dict)


ProgressFn = Callable[[SyncResult], None]


class Syncer:
    def __init__(self, store: Store, api: GmailAPI, *, mode: str = "full", batch_size: int = 50,
                 progress: Optional[ProgressFn] = None, scrub_fn=scrub) -> None:
        self.store, self.api, self.mode = store, api, mode
        self.batch_size = batch_size
        self.progress = progress or (lambda r: None)
        self.scrub_fn = scrub_fn
        self.result = SyncResult(status="ok")

    # --- public -----------------------------------------------------------

    def run(self) -> SyncResult:
        self.result = SyncResult(status="ok")
        try:
            if self.store.get_meta("history_id") is None:
                self._backfill()
            else:
                self._incremental()
            self._refresh_stale()
            self.store.set_meta("auth_state", "ok")
            self.store.set_meta("last_sync_ms", str(int(time.time() * 1000)))
        except NeedsReconnect as e:
            self.store.set_meta("auth_state", "needs_reconnect")
            self.result.status, self.result.message = "needs_reconnect", e.message
            log.warning("sync stopped: reconnect needed (%s)", e.reason)
        except TransientError as e:
            self.result.status = "retry_later"
            self.result.message = "Couldn't reach Gmail. Progress is saved; sync will resume automatically."
            log.warning("sync paused: %s", type(e).__name__)
        return self.result

    # --- phases -----------------------------------------------------------

    def _query(self) -> Optional[str]:
        return FULL_MODE_QUERY if self.mode == "full" else None  # q isn't allowed with gmail.metadata

    def _fmt(self) -> str:
        return "full" if self.mode == "full" else "metadata"

    def _backfill(self) -> None:
        s = self.store
        self.result.phase = "resync" if s.get_meta("resync") else "backfill"
        start = s.get_meta("backfill_start_history_id")
        if start is None:
            profile = self.api.get_profile()
            if not s.get_meta("account_email"):
                s.set_meta("account_email", profile["emailAddress"].lower())
            start = str(profile["historyId"])
            s.clear_seen()
            s.set_meta("backfill_start_history_id", start)
            s.set_meta("backfill_page_token", None)
        token = s.get_meta("backfill_page_token")
        while True:
            ids, next_token = self.api.list_message_ids(token, self._query())
            s.mark_seen(ids)
            known = s.known_ids(ids)
            self._fetch_and_store([i for i in ids if i not in known])
            if not next_token:
                break
            s.set_meta("backfill_page_token", next_token)  # only after the page is stored
            token = next_token
            self.progress(self.result)
        self.result.deleted += s.prune_unseen()
        s.set_meta("history_id", start)
        for key in ("backfill_start_history_id", "backfill_page_token", "resync"):
            s.set_meta(key, None)
        log.info("backfill complete: %s fetched", self.result.fetched)

    def _incremental(self) -> None:
        s = self.store
        self.result.phase = "incremental"
        start = s.get_meta("history_id")
        changed, deleted = set(), set()
        latest, token = start, None
        try:
            while True:
                page = self.api.list_history(start, token)
                c, d = history_message_ids(page)
                changed |= c
                deleted |= d
                latest = str(page.get("historyId") or latest)
                token = page.get("nextPageToken")
                if not token:
                    break
        except HistoryExpired:
            log.info("history id expired; starting full resync")
            s.set_meta("history_id", None)
            s.set_meta("resync", "1")
            self._backfill()
            return
        if deleted:
            s.delete(sorted(deleted))
            self.result.deleted += len(deleted)
        self._fetch_and_store(sorted(changed - deleted))
        s.set_meta("history_id", latest)

    def _refresh_stale(self) -> None:
        """Re-fetch anything scrubbed under an older rule version."""
        stale = self.store.stale_ids()
        if stale:
            log.info("re-scrubbing %d messages after a rule change", len(stale))
            self._fetch_and_store(stale)

    # --- fetch / scrub / store -------------------------------------------

    def _fetch_and_store(self, ids: List[str]) -> None:
        for i in range(0, len(ids), self.batch_size):
            chunk = ids[i:i + self.batch_size]
            resources = self.api.get_messages(chunk, self._fmt())
            results, gone = [], []
            for mid in chunk:
                res = resources.get(mid)
                if res is None:
                    gone.append(mid)  # deleted between listing and fetching
                    continue
                results.append(self._scrub_one(mid, res))
            if gone:
                self.store.delete(gone)
                self.result.deleted += len(gone)
            self.store.put_many(results)
            self.result.fetched += len(results)
            for r in results:
                if isinstance(r, Dropped):
                    self.result.dropped += 1
                else:
                    self.result.kept += 1
            self.progress(self.result)

    def _scrub_one(self, mid: str, resource: dict):
        try:
            raw = parse_gmail_message(resource, metadata_only=(self.mode != "full"))
            return self.scrub_fn(raw)
        except Exception as e:  # never let one odd message stop the sync, never store it raw
            name = type(e).__name__
            self.result.errors[name] = self.result.errors.get(name, 0) + 1
            log.warning("could not scrub message %s (%s); stored as dropped", mid, name)
            return Dropped(mid, resource.get("threadId") or mid, f"error:{name}",
                           int(resource.get("internalDate") or 0))


def sync_account(store: Store, api: GmailAPI, mode: str = "full",
                 progress: Optional[ProgressFn] = None) -> SyncResult:
    store.set_meta("sync_mode", mode)
    return Syncer(store, api, mode=mode, progress=progress).run()

