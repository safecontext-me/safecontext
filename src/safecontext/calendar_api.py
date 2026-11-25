"""Google Calendar API access behind a small interface, so sync logic can be
tested against an in-memory fake (demo/fake_calendar.py).

Read-only: SafeContext requests the calendar.readonly scope and only ever
lists calendars and events. Incremental sync uses Calendar's sync tokens; an
expired token (HTTP 410) surfaces as `SyncTokenExpired` and the syncer does a
full resync of that calendar.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, List, Optional

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

from .auth import CALENDAR_RECONNECT_MESSAGE as CALENDAR_RECONNECT
from .auth import CALENDAR_API_DISABLED, NeedsReconnect, NotConfigured, TransientAuthError, is_api_disabled
from .auth import is_invalid_grant
from .errors import TransientError
from .gmail_api import _is_rate_limit, _status

log = logging.getLogger("safecontext.calendar")  # status codes and counts only, never content

_RETRYABLE = {429, 500, 502, 503, 504}


class SyncTokenExpired(Exception):
    """The calendar's sync token is no longer valid (HTTP 410); do a full sync."""


class CalendarAPI(Protocol):
    def get_primary(self) -> dict:
        """The primary calendar: {"id": the account's email, "summary": ..., "timeZone": ...}."""
        ...

    def list_calendars(self) -> List[dict]:
        """Every calendar in the user's calendar list (calendarList resources)."""
        ...

    def list_events(self, calendar_id: str, sync_token: Optional[str], page_token: Optional[str]) -> dict:
        """One page of events: {"items": [...], "nextPageToken"?: ..., "nextSyncToken"?: ...}.
        With a sync token, only changes since then (cancelled events included)."""
        ...


class GoogleCalendarAPI:
    def __init__(self, creds, *, max_retries: int = 6, sleep: Callable[[float], None] = time.sleep) -> None:
        from googleapiclient.discovery import build  # uses the bundled discovery doc, no network

        self._svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self._max_retries = max_retries
        self._sleep = sleep

    def _map_auth(self, err: Exception) -> None:
        from google.auth.exceptions import RefreshError

        if isinstance(err, RefreshError):
            if is_invalid_grant(err):
                raise NeedsReconnect("revoked_or_expired", CALENDAR_RECONNECT) from None
            raise TransientError(str(err)) from None
        if isinstance(err, TransientAuthError):
            raise TransientError(str(err)) from None
        if _status(err) == 401:
            raise NeedsReconnect("unauthorized", CALENDAR_RECONNECT) from None
        if is_api_disabled(err):  # nothing the user can fix; the source shows it and retries later
            raise NotConfigured(CALENDAR_API_DISABLED) from None
        if _status(err) == 403 and not _is_rate_limit(err):
            text = str(getattr(err, "content", b"") or err).lower()
            if "insufficient" in text or "scope" in text:
                raise NeedsReconnect("scope_not_granted", CALENDAR_RECONNECT) from None

    def _execute(self, request):
        from googleapiclient.errors import HttpError

        for attempt in range(self._max_retries):
            try:
                return request.execute(num_retries=0)
            except HttpError as e:
                if _status(e) == 410:
                    raise SyncTokenExpired() from None
                self._map_auth(e)
                if _is_rate_limit(e) or _status(e) in _RETRYABLE:
                    self._sleep(min(64.0, 2 ** attempt + random.random()))
                    continue
                raise
            except (OSError, TimeoutError) as e:
                if attempt == self._max_retries - 1:
                    raise TransientError(str(e)) from None
                self._sleep(min(64.0, 2 ** attempt + random.random()))
            except Exception as e:
                self._map_auth(e)
                raise
        raise TransientError("Google Calendar kept rate-limiting or failing; will retry on the next sync")

    def get_primary(self) -> dict:
        return self._execute(self._svc.calendars().get(calendarId="primary"))

    def list_calendars(self) -> List[dict]:
        out, token = [], None
        while True:
            kwargs = {"maxResults": 250, "showHidden": False}
            if token:
                kwargs["pageToken"] = token
            resp = self._execute(self._svc.calendarList().list(**kwargs))
            out += resp.get("items") or []
            token = resp.get("nextPageToken")
            if not token:
                return out

    def list_events(self, calendar_id, sync_token, page_token):
        kwargs = {"calendarId": calendar_id, "maxResults": 250, "singleEvents": False}
        if sync_token:
            kwargs["syncToken"] = sync_token  # can't be combined with timeMin/timeMax/q
        else:
            kwargs["showDeleted"] = False
        if page_token:
            kwargs["pageToken"] = page_token
        return self._execute(self._svc.events().list(**kwargs))

