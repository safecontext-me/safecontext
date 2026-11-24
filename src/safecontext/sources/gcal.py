"""Google Calendar as a source.

Syncs every calendar in the user's calendar list that's shown in Google
Calendar, with one sync token per calendar, so after the first sync only
changed events are fetched. Each event becomes one item: title, time,
organizer, attendees, description, location and the video-call link, all
scrubbed. Dial-in PINs and passcodes are never read from the API response.

Recurring events are stored once (the series, with its repeat rule) plus any
individually changed occurrences, grouped under the series' thread id.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from .. import auth
from ..calendar_api import CalendarAPI, SyncTokenExpired
from ..scrub import RawItem, scrub_item
from ..store import Store, to_ms
from ..sync import BaseSyncer, ProgressFn
from .base import OAUTH_SCOPE, BaseSource, Requirement

log = logging.getLogger("safecontext.gcal")

GCAL = "gcal"
CalendarFactory = Callable[[str], CalendarAPI]                  # account -> CalendarAPI
CalendarConnect = Callable[[Optional[str]], str]                # email hint -> account
SKIP_EVENT_TYPES = {"workingLocation", "focusTime"}             # status markers, not events


def _real_connect(email: Optional[str]) -> str:
    _, account = auth.connect_calendar(login_hint=email or None)
    return account


def _real_calendar(account: str) -> CalendarAPI:
    from ..calendar_api import GoogleCalendarAPI
    return GoogleCalendarAPI(auth.get_calendar_credentials(account))


# --- mapping one event ------------------------------------------------------------

def _when(t: Dict[str, str]) -> Optional[int]:
    if not t:
        return None
    return to_ms(t.get("dateTime") or t.get("date"))


def _person(p: Dict[str, Any]) -> str:
    email, name = p.get("email") or "", p.get("displayName") or ""
    if name and email:
        return f"{name} <{email}>"
    return name or email


def _strip_query(url: str) -> str:
    """Meeting links can carry passwords (Zoom's ?pwd=...). Keep only the room."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _video_link(ev: Dict[str, Any]) -> str:
    for ep in (ev.get("conferenceData") or {}).get("entryPoints") or []:
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return _strip_query(ep["uri"])  # pin / passcode / accessCode are deliberately ignored
    if ev.get("hangoutLink"):
        return _strip_query(ev["hangoutLink"])
    return ""


_RRULE_FREQ = {"DAILY": "daily", "WEEKLY": "weekly", "MONTHLY": "monthly", "YEARLY": "yearly"}


def _repeats(ev: Dict[str, Any]) -> str:
    for rule in ev.get("recurrence") or []:
        m = re.search(r"FREQ=(\w+)", rule)
        if rule.startswith("RRULE") and m:
            return _RRULE_FREQ.get(m.group(1), m.group(1).lower())
    return ""


_RESPONSE = {"accepted": "", "declined": "declined", "tentative": "maybe", "needsAction": "invited"}
_LOOKS_HTML = re.compile(r"<(?:br|p|div|a|b|i|u|ul|ol|li|span)\b", re.I)


def event_to_item(calendar_id: str, ev: Dict[str, Any], calendar_name: str = "") -> Optional[RawItem]:
    """Map a Calendar API event resource to a RawItem, or None to skip it."""
    if ev.get("status") == "cancelled" or ev.get("eventType") in SKIP_EVENT_TYPES:
        return None
    start = _when(ev.get("start") or {})
    if start is None:
        return None
    end = _when(ev.get("end") or {})
    people, rooms, me = [], [], ""
    for a in ev.get("attendees") or []:
        if a.get("resource"):
            rooms.append(a.get("displayName") or a.get("email") or "")
            continue
        label = _person(a)
        note = _RESPONSE.get(a.get("responseStatus") or "", "")
        people.append(f"{label} ({note})" if note else label)
        if a.get("self"):
            me = note
    extra: Dict[str, str] = {}
    if ev.get("start", {}).get("date"):
        extra["When"] = "All day"
    if ev.get("location"):
        extra["Location"] = ev["location"]
    if rooms:
        extra["Room"] = ", ".join(r for r in rooms if r)
    link = _video_link(ev)
    if link:
        extra["Video call"] = link
    rep = _repeats(ev)
    if rep:
        extra["Repeats"] = rep
    if me:
        extra["Your response"] = me
    if calendar_name:
        extra["Calendar"] = calendar_name
    description = ev.get("description") or ""
    is_html = bool(_LOOKS_HTML.search(description))
    series = ev.get("recurringEventId") or ev["id"]
    return RawItem(
        id=f"{calendar_id}/{ev['id']}",
        kind="event",
        thread_id=f"{calendar_id}/{series}",
        date_ms=start,
        end_ms=end,
        sender=_person(ev.get("organizer") or {}),
        participants=people,
        title=ev.get("summary") or "(no title)",
        text="" if is_html else description,
        html=description if is_html else "",
        link=ev.get("htmlLink") or "",
        extra=extra,
    )


# --- sync ---------------------------------------------------------------------------

class CalendarSyncer(BaseSyncer):
    source = GCAL
    service = "Google Calendar"
    reconnect_message = auth.CALENDAR_RECONNECT_MESSAGE

    def __init__(self, store: Store, api: CalendarAPI, *, progress: Optional[ProgressFn] = None) -> None:
        super().__init__(store, progress=progress, api=api)

    def _tokens(self) -> Dict[str, str]:
        return json.loads(self.state.get("sync_tokens") or "{}")

    def _save_tokens(self, tokens: Dict[str, str]) -> None:
        self.state.set("sync_tokens", json.dumps(tokens))

    def _sync(self) -> None:
        tokens = self._tokens()
        if self.store.stale_ids(source=GCAL):
            tokens = {}  # scrubbing rules changed: re-fetch everything once
        calendars = [c for c in self.api.list_calendars()
                     if not c.get("deleted") and (c.get("primary") or c.get("selected", True))]
        wanted = {c["id"] for c in calendars}
        stored = self.store.ids(GCAL)
        gone_cals = [i for i in stored if i.split("/", 1)[0] not in wanted]
        if gone_cals:  # a calendar was removed or hidden: forget its events
            self.store.delete(gone_cals, GCAL)
            self.result.deleted += len(gone_cals)
        tokens = {k: v for k, v in tokens.items() if k in wanted}
        self.result.phase = "incremental" if tokens else "backfill"
        for cal in calendars:
            cid = cal["id"]
            name = "" if cal.get("primary") else (cal.get("summaryOverride") or cal.get("summary") or "")
            try:
                tokens[cid] = self._sync_calendar(cid, name, tokens.get(cid))
            except SyncTokenExpired:
                log.info("calendar sync token expired; full resync of one calendar")
                tokens[cid] = self._sync_calendar(cid, name, None)
            self._save_tokens(tokens)  # after each calendar, so progress survives an interruption

    def _sync_calendar(self, cid: str, name: str, token: Optional[str]) -> str:
        full = token is None
        seen = set()
        page_token: Optional[str] = None
        while True:
            page = self.api.list_events(cid, token, page_token)
            results, deleted = [], []
            for ev in page.get("items") or []:
                iid = f"{cid}/{ev['id']}"
                try:
                    item = event_to_item(cid, ev, name)
                    if item is None:
                        deleted.append(iid)
                    else:
                        results.append(scrub_item(item))
                except Exception as e:  # never stop the sync for one odd event, never store it raw
                    results.append(self._error(iid, e))
            seen.update(r.id for r in results)
            if deleted:
                existing = self.store.ids(GCAL)
                gone = [d for d in deleted if d in existing]
                self.store.delete(gone, GCAL)
                self.result.deleted += len(gone)
            self.store.put_many(results, GCAL)
            self._count(results)
            self._report()
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        if full:  # drop local events of this calendar that a full listing no longer has
            stale = [i for i in self.store.ids(GCAL) if i.startswith(cid + "/") and i not in seen]
            if stale:
                self.store.delete(stale, GCAL)
                self.result.deleted += len(stale)
        return page.get("nextSyncToken") or token or ""

# --- the source -------------------------------------------------------------------------

class GoogleCalendarSource(BaseSource):
    id = GCAL
    name = "Google Calendar"
    description = "Your events, attendees and meeting links, scrubbed on this Mac."
    kinds = ("event",)
    api_version = 1
    link_prefixes = ("https://www.google.com/calendar/", "https://calendar.google.com/")
    requirements = (Requirement(OAUTH_SCOPE, auth.SCOPE_CALENDAR, "Read-only Google Calendar access",
                                "To copy your events here so AI can answer questions about your schedule. "
                                "SafeContext can't create, change or delete events."),)
    google = True

    def __init__(self, store: Store, *, api_factory: Optional[CalendarFactory] = None,
                 connect_fn: Optional[CalendarConnect] = None,
                 revoke_fn: Optional[Callable[..., None]] = None) -> None:
        super().__init__(store)
        self.api_factory = api_factory or _real_calendar
        self.connect_fn = connect_fn or _real_connect
        self.revoke_fn = revoke_fn

    def connect(self, email: Optional[str] = None, **_: Any) -> str:
        account = self.connect_fn(email)
        previous = self.account
        if previous and previous != account:
            if not self.revoke_fn:  # a real sign-in just saved a token for the other account; forget it
                auth.calendar_tokens().delete(account)
            raise auth.NotConfigured(f"This Mac already holds calendar events for {previous}. Disconnect "
                                     "Google Calendar first to switch accounts.")
        self._mark_connected(account)
        return account

    def _syncer(self, progress: Optional[ProgressFn]) -> CalendarSyncer:
        return CalendarSyncer(self.store, self.api_factory(self.account), progress=progress)

    def disconnect(self, wipe: bool = True, revoke: bool = True) -> None:
        if self.account:
            fn = self.revoke_fn or (lambda account, revoke=True: auth.disconnect(
                account, auth.calendar_tokens(), revoke=revoke))
            fn(self.account, revoke=revoke)
        super().disconnect(wipe=wipe)
