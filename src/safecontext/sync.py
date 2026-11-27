"""Sync machinery shared by every source, and Gmail's syncer.

Every source syncs through a `BaseSyncer`: it takes that source's lock file
(so the app and the command line never sync the same source at once, while
different sources sync side by side), writes a heartbeat other processes can
read, and turns failures into a `SyncResult` instead of an exception, so one
failing source never stops another.

Gmail: backfill the whole mailbox, then keep it current with history.list.
Flow of one message: Gmail API -> parse (memory) -> scrub (memory) -> Store.
Nothing unscrubbed is ever persisted or logged.

Resumability: the backfill saves its page token only after a page's
messages are stored, and skips ids already stored under the current
scrubber version, so an interrupted run picks up where it stopped. The
history id is recorded *before* the backfill starts. A long backfill (a big
mailbox can take hours or days under Gmail's quota) also catches up on new
mail every CATCH_UP_SECONDS from that history id and moves it forward, so new
mail shows up while older mail is still downloading.

Gmail's state lives in source_state('gmail'):
  account, sync_mode ('full'|'headers'),
  backfill_start_history_id, backfill_page_token, resync (in progress),
  history_id (set once backfill completes), auth_state, last_sync_ms,
  mailbox_total (Gmail's message count when the backfill started),
  sync_heartbeat (JSON progress of a running sync, readable by any process)
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .auth import NeedsReconnect, TransientAuthError
from .errors import NeedsPermission, TransientError
from .gmail_api import FULL_MODE_QUERY, GmailAPI, HistoryExpired
from .gmail_parse import HistoryChanges, parse_gmail_message
from .scrub import Dropped, scrub
from .store import GMAIL, Store

log = logging.getLogger("safecontext.sync")  # ids and counts only, never content


@dataclass
class SyncResult:
    status: str                 # "ok" | "needs_reconnect" | "needs_permission" | "retry_later" | "busy"
    phase: str = ""             # "backfill" | "incremental" | "resync"
    message: str = ""
    fetched: int = 0
    kept: int = 0
    dropped: int = 0
    deleted: int = 0
    errors: Dict[str, int] = field(default_factory=dict)


ProgressFn = Callable[[SyncResult], None]
HEARTBEAT_KEY = "sync_heartbeat"
CATCH_UP_SECONDS = 5 * 60  # during a backfill, fetch new mail this often


def _now_ms() -> int:
    return int(time.time() * 1000)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_heartbeat(store: Store, source: str = GMAIL) -> Optional[dict]:
    """Progress of the sync of `source` running right now in any process, or None."""
    raw = store.get_state(source, HEARTBEAT_KEY)
    if not raw:
        return None
    try:
        hb = json.loads(raw)
    except ValueError:
        return None
    if not _pid_alive(int(hb.get("pid") or 0)):
        return None  # that process ended without cleaning up (killed, crashed)
    return hb


def lock_path(store: Store, source: str):
    name = "sync.lock" if source == GMAIL else f"sync-{source}.lock"  # Gmail keeps its v1 name
    return store.path.parent / name


class _SyncLock:
    def __init__(self, path) -> None:
        self.path, self.fd = path, None

    def acquire(self) -> bool:
        import fcntl
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            os.close(self.fd)
            self.fd = None
            return False

    def release(self) -> None:
        if self.fd is not None:
            import fcntl
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


class BaseSyncer:
    """Lock, heartbeat, progress and error handling for one source's sync.
    Subclasses set `source` and `service` and implement `_sync()`."""

    source: str = ""
    service: str = "the source"  # used in messages: "Couldn't reach {service}."
    reconnect_message: Optional[str] = None  # overrides NeedsReconnect's (Gmail-worded) default

    def __init__(self, store: Store, *, progress: Optional[ProgressFn] = None, api=None) -> None:
        self.store = store
        self.state = store.state(self.source)
        self.api = api
        self.progress = progress or (lambda r: None)
        self.result = SyncResult(status="ok")
        self._started_ms = 0
        self._samples: deque = deque()  # (time, fetched) for a recent pace
        self._waiting_until_ms: Optional[int] = None
        self._lock = _SyncLock(lock_path(store, self.source)) if getattr(store, "path", None) else None

    # --- heartbeat --------------------------------------------------------

    def _pace(self) -> Optional[float]:
        """Items per minute over roughly the last 10 minutes."""
        now = time.time()
        self._samples.append((now, self.result.fetched))
        while len(self._samples) > 2 and now - self._samples[0][0] > 600:
            self._samples.popleft()
        t0, f0 = self._samples[0]
        if now - t0 < 30:
            return None
        return round((self.result.fetched - f0) / (now - t0) * 60, 1)

    def _beat(self) -> None:
        r = self.result
        hb = {"pid": os.getpid(), "phase": r.phase, "fetched": r.fetched, "kept": r.kept,
              "dropped": r.dropped, "deleted": r.deleted, "started_ms": self._started_ms,
              "updated_ms": _now_ms(), "per_min": self._pace(),
              "waiting_until_ms": self._waiting_until_ms}
        try:
            self.state.set(HEARTBEAT_KEY, json.dumps(hb))
        except Exception:  # status reporting must never break a sync
            log.debug("heartbeat write failed", exc_info=True)

    def _on_wait(self, seconds: Optional[float]) -> None:
        """Called by an API client before/after waiting out a quota window."""
        self._waiting_until_ms = _now_ms() + int(seconds * 1000) if seconds else None
        self._beat()

    def _report(self) -> None:
        self._beat()
        self.progress(self.result)

    def _count(self, results) -> None:
        self.result.fetched += len(results)
        for r in results:
            if isinstance(r, Dropped):
                self.result.dropped += 1
            else:
                self.result.kept += 1

    def _error(self, item_id: str, e: Exception, thread_id: str = "", date_ms: int = 0) -> Dropped:
        """An item that couldn't be scrubbed is stored as dropped, never raw."""
        name = type(e).__name__
        self.result.errors[name] = self.result.errors.get(name, 0) + 1
        log.warning("could not scrub %s item %s (%s); stored as dropped", self.source, item_id, name)
        return Dropped(item_id, thread_id or item_id, f"error:{name}", date_ms)

    # --- public -----------------------------------------------------------

    def run(self) -> SyncResult:
        self.result = SyncResult(status="ok")
        if self._lock and not self._lock.acquire():
            self.result.status = "busy"
            self.result.message = "Another SafeContext sync of this source is already running."
            return self.result
        self._started_ms = _now_ms()
        self._samples.clear()
        if hasattr(self.api, "wait_hook"):
            self.api.wait_hook = self._on_wait
        try:
            self._beat()
            return self._run()
        finally:
            self.state.set(HEARTBEAT_KEY, None)
            if hasattr(self.api, "wait_hook"):
                self.api.wait_hook = None
            if self._lock:
                self._lock.release()

    def _run(self) -> SyncResult:
        try:
            self._sync()
            self.state.set("auth_state", "ok")
            self.state.set("last_error", None)
            self.state.set("last_sync_ms", str(_now_ms()))
        except NeedsReconnect as e:
            self.state.set("auth_state", "needs_reconnect")
            self.result.status, self.result.message = "needs_reconnect", self.reconnect_message or e.message
            log.warning("%s sync stopped: reconnect needed (%s)", self.source, e.reason)
        except NeedsPermission as e:
            self.state.set("auth_state", "needs_permission")
            self.result.status, self.result.message = "needs_permission", e.message
            log.warning("%s sync stopped: permission needed (%s)", self.source, e.requirement)
        except (TransientError, TransientAuthError) as e:
            self.result.status = "retry_later"
            self.result.message = (f"Couldn't reach {self.service}. Progress is saved; "
                                   "sync will resume automatically.")
            log.warning("%s sync paused: %s", self.source, e)  # e carries counts and status codes only
        if self.result.status != "ok":
            self.state.set("last_error", self.result.message)
        return self.result

    def _sync(self) -> None:
        raise NotImplementedError


class Syncer(BaseSyncer):
    """Gmail."""

    source = GMAIL
    service = "Gmail"

    def __init__(self, store: Store, api: GmailAPI, *, mode: str = "full", batch_size: int = 50,
                 progress: Optional[ProgressFn] = None, scrub_fn=scrub) -> None:
        super().__init__(store, progress=progress, api=api)
        self.mode = mode
        self.batch_size = batch_size
        self.scrub_fn = scrub_fn
        self.catch_up_seconds = CATCH_UP_SECONDS
        self.clock: Callable[[], float] = time.monotonic

    def _sync(self) -> None:
        self._refresh_labels()
        if self.state.get("history_id") is None:
            self._backfill()
        else:
            self._incremental()
        self._refresh_stale()

    # --- phases -----------------------------------------------------------

    def _query(self) -> Optional[str]:
        return FULL_MODE_QUERY if self.mode == "full" else None  # q isn't allowed with gmail.metadata

    def _fmt(self) -> str:
        return "full" if self.mode == "full" else "metadata"

    def _backfill(self) -> None:
        s, st = self.store, self.state
        self.result.phase = "resync" if st.get("resync") else "backfill"
        start = st.get("backfill_start_history_id")
        if start is None:
            profile = self.api.get_profile()
            if not st.get("account"):
                st.set("account", profile["emailAddress"].lower())
            start = str(profile["historyId"])
            # Gmail's own count (it includes categories we never list); shown as an estimate.
            st.set("mailbox_total", str(profile["messagesTotal"]) if profile.get("messagesTotal") else None)
            s.clear_seen(GMAIL)
            st.set("backfill_start_history_id", start)
            st.set("backfill_page_token", None)
        elif st.get("mailbox_total") is None:
            # A backfill begun by an older build never recorded the total, so the
            # progress bar had no estimate. One cheap profile call fills it in.
            total = self.api.get_profile().get("messagesTotal")
            st.set("mailbox_total", str(total) if total else None)
        token = st.get("backfill_page_token")
        last_catch_up = self.clock()
        while True:
            ids, next_token = self.api.list_message_ids(token, self._query())
            s.mark_seen(ids, GMAIL)
            known = s.known_ids(ids, source=GMAIL)
            self._fetch_and_store([i for i in ids if i not in known])
            if not next_token:
                break
            st.set("backfill_page_token", next_token)  # only after the page is stored
            token = next_token
            self._report()
            if self.clock() - last_catch_up >= self.catch_up_seconds:
                start = self._catch_up(start)
                last_catch_up = self.clock()
        self.result.deleted += s.prune_unseen(GMAIL)
        st.set("history_id", start)
        for key in ("backfill_start_history_id", "backfill_page_token", "resync"):
            st.set(key, None)
        log.info("backfill complete: %s fetched", self.result.fetched)

    def _incremental(self) -> None:
        s, st = self.store, self.state
        self.result.phase = "incremental"
        start = st.get("history_id")
        try:
            ch, latest = self._history_since(start)
        except HistoryExpired:
            log.info("history id expired; starting full resync")
            st.set("history_id", None)
            st.set("resync", "1")
            self._backfill()
            return
        if ch.deleted:
            s.delete(sorted(ch.deleted), GMAIL)
            self.result.deleted += len(ch.deleted)
        missing = s.set_item_labels(GMAIL, ch.label_only())  # not stored at all: fetch them instead
        self._fetch_and_store(sorted((ch.changed | missing) - ch.deleted))
        st.set("history_id", latest)

    def _history_since(self, start: str):
        """(HistoryChanges, latest history id) since `start`. Raises HistoryExpired."""
        ch = HistoryChanges()
        latest, token = start, None
        while True:
            page = self.api.list_history(start, token)
            ch.add_page(page)
            latest = str(page.get("historyId") or latest)
            token = page.get("nextPageToken")
            if not token:
                return ch, latest

    def _refresh_labels(self) -> None:
        """Mirror Gmail's label names and colours (one cheap call per sync). Renames and deletions
        apply to every stored message at once; nothing is re-fetched."""
        if hasattr(self.api, "list_labels"):
            self.store.replace_labels(GMAIL, self.api.list_labels())

    def _catch_up(self, start: str) -> str:
        """Mid-backfill: store what changed since `start` and return the new start, so new
        mail doesn't wait for the whole mailbox. What it stores is marked seen, so the
        backfill's final prune keeps it."""
        s, st = self.store, self.state
        try:
            ch, latest = self._history_since(start)
            deleted = ch.deleted
            changed = ch.changed | s.set_item_labels(GMAIL, ch.label_only())
        except HistoryExpired:
            # The backfill outlived Gmail's history (about a week): restart from now and take the
            # newest page, which the backfill passed long ago.
            latest = str(self.api.get_profile()["historyId"])
            ids, _ = self.api.list_message_ids(None, self._query())
            known = s.known_ids(ids, source=GMAIL)
            changed, deleted = {i for i in ids if i not in known}, set()
            s.mark_seen(ids, GMAIL)
        if deleted:
            s.delete(sorted(deleted), GMAIL)
            self.result.deleted += len(deleted)
        fresh = sorted(changed - deleted)
        s.mark_seen(fresh, GMAIL)
        self._fetch_and_store(fresh)
        st.set("backfill_start_history_id", latest)  # only after it's stored
        if fresh or deleted:
            log.info("caught up during backfill: %d changed, %d deleted", len(fresh), len(deleted))
        return latest

    def _refresh_stale(self) -> None:
        """Re-fetch anything scrubbed under an older rule version."""
        stale = self.store.stale_ids(source=GMAIL)
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
                self.store.delete(gone, GMAIL)
                self.result.deleted += len(gone)
            self.store.put_many(results, GMAIL)
            self._count(results)
            self._report()

    def _scrub_one(self, mid: str, resource: dict):
        try:
            raw = parse_gmail_message(resource, metadata_only=(self.mode != "full"))
            return self.scrub_fn(raw)
        except Exception as e:  # never let one odd message stop the sync, never store it raw
            return self._error(mid, e, resource.get("threadId") or mid, int(resource.get("internalDate") or 0))


def sync_account(store: Store, api: GmailAPI, mode: str = "full",
                 progress: Optional[ProgressFn] = None) -> SyncResult:
    store.set_state(GMAIL, "sync_mode", mode)
    return Syncer(store, api, mode=mode, progress=progress).run()
