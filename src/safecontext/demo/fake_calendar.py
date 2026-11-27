"""In-memory Google Calendar API that behaves like the real one where sync
cares: a calendar list, paged event listing, sync tokens that return only
changes (cancelled events included), expiring sync tokens (HTTP 410),
revoked access, and injectable network failures. Plus a synthetic calendar
for tests and demo mode."""
from __future__ import annotations

from collections import Counter
from typing import Dict, List, Optional, Tuple

from safecontext.auth import NeedsReconnect
from safecontext.calendar_api import SyncTokenExpired
from safecontext.errors import TransientError


class FakeCalendar:
    def __init__(self, email: str = "alex@example.org", page_size: int = 3) -> None:
        self.email = email
        self.page_size = page_size
        self.calendars: Dict[str, dict] = {}
        self.events: Dict[str, Dict[str, dict]] = {}   # calendar id -> event id -> resource
        self.changed_at: Dict[Tuple[str, str], int] = {}
        self.seq = 100
        self.oldest_token: Dict[str, int] = {}
        self.revoked = False
        self.calls: Counter = Counter()
        self.fail_list_call: Optional[int] = None
        self.add_calendar(email, "Alex", primary=True)

    # --- calendar mutations ---------------------------------------------------
    def add_calendar(self, cal_id: str, summary: str, primary: bool = False, selected: bool = True) -> None:
        self.calendars[cal_id] = {"id": cal_id, "summary": summary, "primary": primary, "selected": selected,
                                  "accessRole": "owner" if primary else "reader"}
        self.events.setdefault(cal_id, {})
        self.oldest_token.setdefault(cal_id, 0)

    def remove_calendar(self, cal_id: str) -> None:
        self.calendars.pop(cal_id, None)

    def put(self, ev: dict, cal_id: Optional[str] = None) -> None:
        cal_id = cal_id or self.email
        self.seq += 1
        ev = {"status": "confirmed", "kind": "calendar#event", **ev}
        ev.setdefault("htmlLink", f"https://www.google.com/calendar/event?eid={ev['id']}")
        self.events[cal_id][ev["id"]] = ev
        self.changed_at[(cal_id, ev["id"])] = self.seq

    def cancel(self, event_id: str, cal_id: Optional[str] = None) -> None:
        cal_id = cal_id or self.email
        self.seq += 1
        self.events[cal_id][event_id] = {"id": event_id, "status": "cancelled"}
        self.changed_at[(cal_id, event_id)] = self.seq

    def expire_tokens(self, cal_id: Optional[str] = None) -> None:
        for cid in ([cal_id] if cal_id else list(self.calendars)):
            self.oldest_token[cid] = self.seq + 1

    # --- CalendarAPI interface --------------------------------------------------
    def _check(self) -> None:
        if self.revoked:
            raise NeedsReconnect("revoked_or_expired")

    def get_primary(self) -> dict:
        self._check()
        return {"id": self.email, "summary": self.email, "timeZone": "UTC"}

    def list_calendars(self) -> List[dict]:
        self._check()
        self.calls["calendars"] += 1
        return [dict(c) for c in self.calendars.values()]

    def list_events(self, calendar_id, sync_token, page_token):
        self._check()
        self.calls["events"] += 1
        if self.fail_list_call == self.calls["events"]:
            raise TransientError("simulated network drop")
        evs = self.events[calendar_id]
        if sync_token:
            cid, _, since = sync_token.partition("|")
            if cid != calendar_id or int(since) < self.oldest_token[calendar_id]:
                raise SyncTokenExpired()
            items = [e for k, e in evs.items() if self.changed_at[(calendar_id, k)] > int(since)]
        else:
            items = [e for e in evs.values() if e.get("status") != "cancelled"]
        items.sort(key=lambda e: self.changed_at[(calendar_id, e["id"])])
        start = int(page_token or 0)
        page = {"items": [dict(e) for e in items[start:start + self.page_size]]}
        if start + self.page_size < len(items):
            page["nextPageToken"] = str(start + self.page_size)
        else:
            page["nextSyncToken"] = f"{calendar_id}|{self.seq}"
        return page


    def get_event(self, calendar_id, event_id):
        self._check()
        self.calls["get"] += 1
        ev = self.events.get(calendar_id, {}).get(event_id)
        return None if ev is None or ev.get("status") == "cancelled" else dict(ev)


def event(eid: str, summary: str, start: str, end: str = "", *, description: str = "", location: str = "",
          organizer: str = "alex@example.org", attendees: Tuple[str, ...] = (), meet: str = "", **more) -> dict:
    """A Calendar API event resource. Dates 'YYYY-MM-DD' make all-day events."""
    def when(t: str) -> dict:
        return {"date": t} if len(t) == 10 else {"dateTime": t}
    ev = {"id": eid, "summary": summary, "start": when(start), "end": when(end or start),
          "organizer": {"email": organizer}, "updated": "2026-11-01T00:00:00Z"}
    if description:
        ev["description"] = description
    if location:
        ev["location"] = location
    if attendees:
        ev["attendees"] = [{"email": a, "responseStatus": "accepted"} for a in attendees]
    if meet:
        ev["hangoutLink"] = meet
        ev["conferenceData"] = {"entryPoints": [
            {"entryPointType": "video", "uri": meet, "label": meet.split("//")[-1]},
            {"entryPointType": "phone", "uri": "tel:+1-555-0100", "pin": "987654321"}]}
    ev.update(more)
    return ev


def demo_events() -> List[dict]:
    """Synthetic events: ordinary ones to find, and ones the scrubber must drop or redact."""
    return [
        event("ev01", "Dinner with Jamie", "2026-11-28T19:00:00-08:00", "2026-11-28T21:00:00-08:00",
              description="Table for 4 at Nopa, booked under Rivera.", location="Nopa, San Francisco",
              organizer="jamie@example.com", attendees=("jamie@example.com", "alex@example.org")),
        event("ev02", "Q1 planning", "2026-12-02T10:00:00-08:00", "2026-12-02T11:00:00-08:00",
              description="<p>Agenda: <b>hiring plan</b> and the budget review on Jan 15.</p>",
              organizer="taylor@example.org", attendees=("taylor@example.org", "alex@example.org", "sam@example.dev"),
              meet="https://meet.google.com/abc-defg-hij"),
        event("ev03", "Weekly 1:1 with Taylor", "2026-11-03T09:30:00-08:00", "2026-11-03T10:00:00-08:00",
              organizer="taylor@example.org", attendees=("taylor@example.org", "alex@example.org"),
              recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"]),
        event("ev04", "Vendor sync", "2026-12-03T15:00:00-08:00", "2026-12-03T15:30:00-08:00",
              description="Zoom: https://example.zoom.us/j/123456789?pwd=Sup3rSecretPwd\nCall Sam at (415) 555-0134.",
              organizer="sam@example.dev",
              conferenceData={"entryPoints": [{"entryPointType": "video",
                                               "uri": "https://example.zoom.us/j/123456789?pwd=Sup3rSecretPwd",
                                               "passcode": "884422"}]}),
        event("ev05", "Lab results review", "2026-12-04T08:00:00-08:00", "2026-12-04T08:30:00-08:00",
              description="Bring insurance card."),
        event("ev06", "Cleaning", "2026-12-05T13:00:00-08:00", "2026-12-05T14:00:00-08:00",
              organizer="appointments@brightsmiledental.com"),
        event("ev07", "Pay rent", "2026-12-01", "2026-12-02",
              description="Transfer to landlord, routing number 021000021, account number 123456789012."),
        event("ev08", "Book club (Meetup)", "2026-12-06T18:00:00-08:00", "2026-12-06T20:00:00-08:00",
              organizer="no-reply@meetup.com", description="This month: The Left Hand of Darkness."),
        event("ev09", "Office", "2026-12-01", "2026-12-02", eventType="workingLocation"),
    ]


def make_fake_calendar(email: str = "alex@example.org", page_size: int = 3) -> FakeCalendar:
    cal = FakeCalendar(email=email, page_size=page_size)
    for ev in demo_events():
        cal.put(ev)
    return cal
