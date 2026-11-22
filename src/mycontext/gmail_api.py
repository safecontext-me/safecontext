"""Gmail API access behind a small interface, so sync logic can be tested
against an in-memory fake.

The real client batches message fetches (50 per HTTP batch), stays under the
per-user quota of 250 units/second with a token bucket, retries rate-limit
and server errors with exponential backoff, and maps auth failures to
`NeedsReconnect`.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

from .auth import NeedsReconnect, TransientAuthError, is_invalid_grant


class GmailError(Exception):
    pass


class HistoryExpired(GmailError):
    """startHistoryId is too old (Gmail keeps roughly a week); do a full resync."""


class TransientError(GmailError):
    """Network or server trouble that outlasted retries. Safe to try again later."""


# Exclude what the drop layer would discard anyway, so it is never downloaded.
# (Not allowed with the metadata scope; there the labels are checked locally.)
FULL_MODE_QUERY = "-in:chats -in:drafts -category:promotions -category:social -category:forums"


class GmailAPI(Protocol):
    def get_profile(self) -> dict: ...

    def list_message_ids(self, page_token: Optional[str], query: Optional[str]) -> Tuple[List[str], Optional[str]]: ...

    def get_messages(self, ids: Sequence[str], fmt: str) -> Dict[str, Optional[dict]]:
        """id -> message resource, or None if the message no longer exists."""
        ...

    def list_history(self, start_history_id: str, page_token: Optional[str]) -> dict: ...


# --- real implementation ---------------------------------------------------

QUOTA_UNITS = {"get": 5, "list": 5, "history": 2, "profile": 1}
_RETRYABLE = {429, 500, 502, 503, 504}
_METADATA_HEADERS = ["From", "To", "Cc", "Subject", "Date", "Message-ID", "List-Unsubscribe", "Precedence"]


class _TokenBucket:
    def __init__(self, rate: float, capacity: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.rate, self.capacity = rate, capacity
        self.clock, self.sleep = clock, sleep
        self.tokens, self.updated = capacity, clock()
        self.lock = threading.Lock()

    def take(self, n: float) -> None:
        """Wait until the bucket is full enough, then spend n. A request larger than
        the bucket (e.g. a 50-message batch = 250 units) waits for a full bucket and
        goes into debt, which later calls repay; otherwise it would wait forever."""
        need = min(n, self.capacity)
        while True:
            with self.lock:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= need - 1e-9:  # tolerate float rounding
                    self.tokens -= n
                    return
                wait = (need - self.tokens) / self.rate
            self.sleep(wait)


def _status(err: Exception) -> int:
    resp = getattr(err, "resp", None)
    return int(getattr(resp, "status", 0) or 0)


def _is_rate_limit(err: Exception) -> bool:
    st = _status(err)
    if st == 429:
        return True
    if st == 403:
        text = str(getattr(err, "content", b"") or err).lower()
        return "ratelimitexceeded" in text or "userratelimitexceeded" in text or "quota" in text
    return False


class GoogleGmailAPI:
    def __init__(self, creds, *, units_per_second: float = 200, max_retries: int = 6,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        from googleapiclient.discovery import build  # uses the bundled discovery doc, no network

        self._svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._bucket = _TokenBucket(units_per_second, units_per_second)
        self._max_retries = max_retries
        self._sleep = sleep

    # error mapping -------------------------------------------------------
    def _map_auth(self, err: Exception) -> None:
        from google.auth.exceptions import RefreshError

        if isinstance(err, RefreshError):
            if is_invalid_grant(err):
                raise NeedsReconnect("revoked_or_expired") from None
            raise TransientError(str(err)) from None
        if isinstance(err, TransientAuthError):
            raise TransientError(str(err)) from None
        if _status(err) == 401:
            raise NeedsReconnect("unauthorized") from None
        if _status(err) == 403 and not _is_rate_limit(err):
            text = str(getattr(err, "content", b"") or err).lower()
            if "insufficient" in text or "scope" in text:
                raise NeedsReconnect("scope_not_granted") from None

    def _backoff(self, attempt: int) -> None:
        self._sleep(min(64.0, (2 ** attempt) + random.random()))

    def _execute(self, request, units: int):
        from googleapiclient.errors import HttpError

        for attempt in range(self._max_retries):
            self._bucket.take(units)
            try:
                return request.execute(num_retries=0)
            except HttpError as e:
                self._map_auth(e)
                if _is_rate_limit(e) or _status(e) in _RETRYABLE:
                    self._backoff(attempt)
                    continue
                raise
            except (OSError, TimeoutError) as e:  # connection reset, DNS, timeouts
                if attempt == self._max_retries - 1:
                    raise TransientError(str(e)) from None
                self._backoff(attempt)
            except Exception as e:
                self._map_auth(e)
                raise
        raise TransientError("Gmail kept rate-limiting or failing; will retry on the next sync")

    # interface -----------------------------------------------------------
    def get_profile(self) -> dict:
        return self._execute(self._svc.users().getProfile(userId="me"), QUOTA_UNITS["profile"])

    def list_message_ids(self, page_token, query):
        kwargs = dict(userId="me", maxResults=500, includeSpamTrash=False)
        if page_token:
            kwargs["pageToken"] = page_token
        if query:
            kwargs["q"] = query
        resp = self._execute(self._svc.users().messages().list(**kwargs), QUOTA_UNITS["list"])
        return [m["id"] for m in resp.get("messages") or []], resp.get("nextPageToken")

    def list_history(self, start_history_id, page_token):
        from googleapiclient.errors import HttpError

        kwargs = dict(userId="me", startHistoryId=start_history_id, maxResults=500,
                      historyTypes=["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"])
        if page_token:
            kwargs["pageToken"] = page_token
        try:
            return self._execute(self._svc.users().history().list(**kwargs), QUOTA_UNITS["history"])
        except HttpError as e:
            if _status(e) == 404:
                raise HistoryExpired() from None
            raise

    def get_messages(self, ids, fmt):
        from googleapiclient.errors import HttpError

        results: Dict[str, Optional[dict]] = {}
        pending = list(dict.fromkeys(ids))
        for attempt in range(self._max_retries):
            if not pending:
                break
            failed: List[str] = []
            fatal: List[Exception] = []

            def callback(request_id, response, exception):
                if exception is None:
                    results[request_id] = response
                elif _status(exception) == 404:
                    results[request_id] = None
                elif _is_rate_limit(exception) or _status(exception) in _RETRYABLE:
                    failed.append(request_id)
                else:
                    fatal.append(exception)

            for i in range(0, len(pending), 50):
                chunk = pending[i:i + 50]
                batch = self._svc.new_batch_http_request(callback=callback)
                for mid in chunk:
                    kwargs = dict(userId="me", id=mid, format=fmt)
                    if fmt == "metadata":
                        kwargs["metadataHeaders"] = _METADATA_HEADERS
                    batch.add(self._svc.users().messages().get(**kwargs), request_id=mid)
                self._bucket.take(QUOTA_UNITS["get"] * len(chunk))
                try:
                    batch.execute()
                except HttpError as e:
                    self._map_auth(e)
                    failed.extend(m for m in chunk if m not in results)
                except Exception as e:
                    self._map_auth(e)
                    if isinstance(e, (OSError, TimeoutError)):
                        failed.extend(m for m in chunk if m not in results)
                    else:
                        raise
            for e in fatal:
                self._map_auth(e)
            if fatal:
                raise fatal[0]
            pending = failed
            if pending:
                self._backoff(attempt)
        if pending:
            raise TransientError(f"{len(pending)} messages could not be fetched; will retry on the next sync")
        return results

