"""Google Calendar: event mapping, scrubbing, incremental sync with sync tokens,
and its place among the other sources. All against FakeCalendar; no network."""
import json

import pytest

from safecontext.demo.fake_calendar import FakeCalendar, event, make_fake_calendar
from safecontext.sources.gcal import GCAL, RESCRUB_META, CalendarSyncer, GoogleCalendarSource, event_to_item
from safecontext.store import Store
from safecontext.tools import run_tool

ME = "alex@example.org"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data" / "mail.db")
    yield s
    s.close()


@pytest.fixture
def cal():
    return make_fake_calendar(ME, page_size=3)


def run(store, cal):
    return CalendarSyncer(store, cal).run()


def items(store):
    return {r[0]: dict(r) for r in store._db.execute("SELECT id, * FROM items WHERE source='gcal'")}


def dropped(store):
    return dict(store._db.execute("SELECT id, reason FROM dropped WHERE source='gcal'").fetchall())


def everything(store):
    """Every byte of text stored for calendar items."""
    return json.dumps([dict(r) for r in store._db.execute("SELECT * FROM items")], ensure_ascii=False)


# --- mapping --------------------------------------------------------------------------

def test_event_mapping():
    ev = event("e1", "Q1 planning", "2026-12-02T10:00:00-08:00", "2026-12-02T11:00:00-08:00",
               organizer="taylor@example.org", attendees=("taylor@example.org", ME),
               meet="https://meet.google.com/abc-defg-hij", recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"],
               htmlLink="https://www.google.com/calendar/event?eid=ZTE")
    ev["attendees"][1].update(responseStatus="tentative", self=True, displayName="Alex")
    ev["attendees"].append({"email": "room-4@resource.calendar.google.com", "displayName": "Room 4",
                            "resource": True})
    it = event_to_item(ME, ev)
    assert it.id == f"{ME}/e1" and it.thread_id == f"{ME}/e1" and it.kind == "event"
    assert it.date_ms == 1796234400000 and it.end_ms == 1796238000000
    assert it.sender == "taylor@example.org"
    assert it.participants == ["taylor@example.org", "Alex <alex@example.org> (maybe)"]
    assert it.extra == {"Room": "Room 4", "Video call": "https://meet.google.com/abc-defg-hij",
                        "Repeats": "weekly", "Your response": "maybe"}
    assert it.link.startswith("https://www.google.com/calendar/event?eid=")


def test_meeting_passcodes_never_read():
    ev = event("e2", "Vendor", "2026-12-03T15:00:00Z", conferenceData={"entryPoints": [
        {"entryPointType": "video", "uri": "https://x.zoom.us/j/1?pwd=Secr3t", "passcode": "884422"},
        {"entryPointType": "phone", "uri": "tel:+1-555-0100", "pin": "987654321"}]})
    it = event_to_item(ME, ev)
    assert it.extra["Video call"] == "https://x.zoom.us/j/1"
    assert "884422" not in repr(it.__dict__) and "987654321" not in repr(it.__dict__)
    assert "Secr3t" not in repr(it.__dict__)


def test_all_day_and_html_and_series():
    it = event_to_item(ME, event("e3", "Pay rent", "2026-12-01", "2026-12-02", description="<p>hi <b>there</b></p>"))
    assert it.extra["When"] == "All day" and it.html and not it.text
    child = event_to_item(ME, event("e3_20261208", "Pay rent", "2026-12-08", recurringEventId="e3"))
    assert child.thread_id == f"{ME}/e3"


@pytest.mark.parametrize("ev", [{"id": "x", "status": "cancelled"},
                                event("w", "Office", "2026-12-01", eventType="workingLocation")])
def test_skipped(ev):
    assert event_to_item(ME, ev) is None


# --- first sync ------------------------------------------------------------------------

def test_first_sync_scrubs_everything(store, cal):
    r = run(store, cal)
    assert r.status == "ok" and r.phase == "backfill"
    got = items(store)
    titles = {v["title"] for v in got.values()}
    assert titles == {"Dinner with Jamie", "Q1 planning", "Weekly 1:1 with Taylor", "Vendor sync", "Pay rent",
                      "Book club (Meetup)"}
    assert dropped(store) == {f"{ME}/ev05": "sensitive:subject",          # Lab results review
                              f"{ME}/ev06": "sensitive:sender_keyword"}   # organized by a dental office
    assert f"{ME}/ev09" not in got and f"{ME}/ev09" not in dropped(store)  # working location: not an event
    blob = everything(store)
    for secret in ("Sup3rSecretPwd", "884422", "987654321", "555-0134", "021000021", "123456789012",
                   "Lab results", "brightsmiledental"):
        assert secret not in blob
    rent = got[f"{ME}/ev07"]
    assert json.loads(rent["redactions"]) == {"ROUTING_NUMBER": 1, "BANK_ACCOUNT": 1}
    assert store.state(GCAL).get("last_sync_ms") and store.state(GCAL).get("auth_state") == "ok"


def test_meetup_from_no_reply_is_kept(store, cal):
    run(store, cal)
    assert items(store)[f"{ME}/ev08"]["sender"] == "no-reply@meetup.com"


def test_search_finds_event_details(store, cal):
    run(store, cal)
    (h,) = store.search("hiring", source=GCAL)
    assert h.kind == "event" and h.title == "Q1 planning" and h.end
    (h,) = store.search("Nopa")  # location is searchable
    assert h.source == GCAL


# --- incremental sync ----------------------------------------------------------------------

def test_incremental_fetches_only_changes(store, cal):
    run(store, cal)
    before = cal.calls["events"]
    cal.put(event("ev10", "Coffee with Sam", "2026-12-10T09:00:00Z", organizer="sam@example.dev"))
    cal.put(event("ev01", "Dinner with Jamie (moved)", "2026-11-29T19:00:00-08:00"))
    cal.cancel("ev03")
    r = run(store, cal)
    assert r.status == "ok" and r.phase == "incremental"
    assert cal.calls["events"] - before == 1  # one page of changes
    got = items(store)
    assert got[f"{ME}/ev10"]["title"] == "Coffee with Sam"
    assert got[f"{ME}/ev01"]["title"] == "Dinner with Jamie (moved)"
    assert f"{ME}/ev03" not in got and r.deleted == 1 and r.fetched == 2


def test_nothing_changed_is_cheap(store, cal):
    run(store, cal)
    r = run(store, cal)
    assert r.fetched == 0 and r.deleted == 0


def test_expired_sync_token_resyncs_and_prunes(store, cal):
    run(store, cal)
    cal.cancel("ev02")
    cal.expire_tokens()
    r = run(store, cal)
    assert r.status == "ok"
    assert f"{ME}/ev02" not in items(store) and len(items(store)) == 5


def test_events_stored_before_dial_in_rules_are_rescrubbed_once(store, cal):
    cal.put(event("ev20", "Vendor call", "2026-12-02T17:00:00Z",
                  description="Join by phone +1-415-555-0199 Access code: 2633 145 1234"))
    run(store, cal)
    iid = f"{ME}/ev20"
    assert "2633 145 1234" not in everything(store)
    # An older build stored the code as-is and kept a sync token, so the event won't come back
    # by itself: put the leaked text back and forget the one-off re-scrub.
    leaked = "Join by phone [REDACTED:PHONE] Access code: 2633 145 1234"
    row = dict(store._db.execute("SELECT * FROM items WHERE source='gcal' AND id=?", (iid,)).fetchone())
    store._db.execute("DELETE FROM items WHERE source='gcal' AND id=?", (iid,))  # keeps the index in step
    row["body"] = leaked
    store._db.execute(f"INSERT INTO items ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    store.set_meta(RESCRUB_META, None)
    assert store.search("2633", source=GCAL)  # AI apps can find the leaked code
    r = run(store, cal)
    assert r.status == "ok" and r.phase == "backfill"
    assert "2633 145 1234" not in everything(store) and "REDACTED" in items(store)[iid]["body"]
    assert not store.search("2633", source=GCAL)
    assert store.get_meta(RESCRUB_META) == "1"
    before = cal.calls["events"]
    r = run(store, cal)  # remembered: back to cheap incremental syncs
    assert r.phase == "incremental" and r.fetched == 0 and cal.calls["events"] - before == 1


def test_event_can_move_from_kept_to_dropped(store, cal):
    run(store, cal)
    cal.put(event("ev01", "Dinner with Jamie", "2026-11-28T19:00:00-08:00",
                  description="Your verification code is 482913"))
    run(store, cal)
    assert f"{ME}/ev01" not in items(store) and dropped(store)[f"{ME}/ev01"] == "security:body"


def test_secondary_calendars_added_and_removed(store, cal):
    cal.add_calendar("team@group.calendar.google.com", "Team")
    cal.put(event("t1", "Team offsite", "2026-12-12"), "team@group.calendar.google.com")
    cal.add_calendar("hidden@group.calendar.google.com", "Hidden", selected=False)
    cal.put(event("h1", "Hidden thing", "2026-12-12"), "hidden@group.calendar.google.com")
    run(store, cal)
    got = items(store)
    team = got["team@group.calendar.google.com/t1"]
    assert json.loads(team["extra"])["Calendar"] == "Team"
    assert not any(k.startswith("hidden@") for k in got)
    cal.remove_calendar("team@group.calendar.google.com")
    r = run(store, cal)
    assert "team@group.calendar.google.com/t1" not in items(store) and r.deleted == 1


def test_paging(store):
    cal = FakeCalendar(ME, page_size=2)
    for i in range(7):
        cal.put(event(f"p{i}", f"Standup {i}", f"2026-12-{10 + i}T09:00:00Z"))
    run(store, cal)
    assert len(items(store)) == 7 and cal.calls["events"] == 4


# --- failures ---------------------------------------------------------------------------------

def test_revoked_means_reconnect(store, cal):
    cal.revoked = True
    r = run(store, cal)
    assert r.status == "needs_reconnect" and store.state(GCAL).get("auth_state") == "needs_reconnect"


def test_network_drop_is_retried_next_time(store, cal):
    cal.fail_list_call = 2
    r = run(store, cal)
    assert r.status == "retry_later" and "Google Calendar" in r.message
    r = run(store, cal)
    assert r.status == "ok" and len(items(store)) == 6


def test_one_bad_event_is_dropped_not_stored_raw(store, cal, monkeypatch):
    import safecontext.sources.gcal as gcal
    real = gcal.event_to_item

    def flaky(cid, ev, name=""):
        if ev["id"] == "ev02":
            raise ValueError("odd event")
        return real(cid, ev, name)

    monkeypatch.setattr(gcal, "event_to_item", flaky)
    r = run(store, cal)
    assert r.status == "ok" and r.errors == {"ValueError": 1}
    assert dropped(store)[f"{ME}/ev02"] == "error:ValueError"


# --- the source --------------------------------------------------------------------------------

def make_source(store, cal, revoked=None):
    revoked = [] if revoked is None else revoked
    return GoogleCalendarSource(store, api_factory=lambda a: cal, connect_fn=lambda email: ME,
                                revoke_fn=lambda account, revoke=True: revoked.append((account, revoke)))


def test_source_status_lifecycle(store, cal):
    src = make_source(store, cal)
    assert src.status().state == "not_connected"
    assert src.connect() == ME
    assert src.sync().status == "ok"
    st = src.status()
    assert (st.state, st.account, st.item_count, st.dropped_count, st.first_sync_done) == ("connected", ME, 6, 2, True)
    cal.revoked = True
    src.sync()
    st = src.status()
    assert st.state == "needs_reconnect" and "Calendar" in st.message


def test_disconnect_removes_only_calendar_data(store, cal):
    from safecontext.demo.synthetic import mailbox
    from safecontext.scrub import scrub
    store.put_many((scrub(m) for m in mailbox()), "gmail")
    gmail_before = store.counts("gmail")
    src = make_source(store, cal)
    src.connect()
    src.sync()
    src.disconnect()
    assert store.counts(GCAL) == (0, 0) and store.counts("gmail") == gmail_before
    assert not src.connected and store.state(GCAL).get("sync_tokens") is None


def test_factory_failure_is_a_status_not_an_exception(store):
    from safecontext import auth

    def factory(account):
        raise auth.NeedsReconnect("revoked_or_expired", auth.CALENDAR_RECONNECT_MESSAGE)

    src = GoogleCalendarSource(store, api_factory=factory, connect_fn=lambda e: ME, revoke_fn=lambda *a, **k: None)
    src.connect()
    assert src.sync().status == "needs_reconnect" and src.status().state == "needs_reconnect"


# --- tools across sources -----------------------------------------------------------------------

@pytest.fixture
def both(store, cal):
    from safecontext.demo.synthetic import mailbox
    from safecontext.scrub import scrub
    store.state("gmail").set("account", ME)
    store.state("gmail").set("history_id", "1")
    store.put_many((scrub(m) for m in mailbox()), "gmail")
    store.state(GCAL).set("account", ME)
    run(store, cal)
    return store


def test_search_spans_sources_with_filter_and_citations(both):
    r = run_tool(both, "search", {"query": "rent"})
    assert {x["source"] for x in r["results"]} == {"gmail", "gcal"}
    for x in r["results"]:
        assert x["link"] and x["citation"].endswith("Gmail" if x["source"] == "gmail" else "Google Calendar")
    cal_only = run_tool(both, "search", {"query": "rent", "source": "gcal"})["results"]
    assert [x["title"] for x in cal_only] == ["Pay rent"] and "gmail_link" not in cal_only[0]
    assert all(x["source"] == "gmail" for x in run_tool(both, "search_mail", {"query": "rent"})["results"])


def test_read_an_event(both):
    hit = run_tool(both, "search", {"query": "Nopa", "source": "gcal"})["results"][0]
    r = run_tool(both, "read", {"thread_id": hit["thread_id"], "source": "gcal"})
    (ev,) = r["messages"]
    assert ev["kind"] == "event" and ev["details"]["Location"] == "Nopa, San Francisco"
    assert "jamie@example.com" in ev["participants"] and ev["end"] > ev["date"]
    assert run_tool(both, "read_thread", {"thread_id": hit["thread_id"]})["messages"][0]["id"] == ev["id"]


def test_upcoming_events_oldest_first(both):
    r = run_tool(both, "list_items", {"source": "gcal", "after": "2026-12-01", "order": "oldest", "limit": 3})
    dates = [x["date"] for x in r["results"]]
    assert dates == sorted(dates) and r["results"][0]["title"] == "Pay rent"


def test_list_messages_is_still_gmail_only(both):
    assert {x["source"] for x in run_tool(both, "list_messages", {"limit": 100})["results"]} == {"gmail"}


def test_list_sources(both):
    r = run_tool(both, "list_sources", {})
    by = {s["source"]: s for s in r["sources"]}
    assert by["gcal"]["name"] == "Google Calendar" and by["gcal"]["items"] == 6 and by["gcal"]["connected"]
    assert by["gmail"]["items"] == 7


def test_kind_filter_and_bad_values(both):
    assert {x["kind"] for x in run_tool(both, "list_items", {"kind": "event"})["results"]} == {"event"}
    assert "kind" in run_tool(both, "search", {"query": "x", "kind": "fax"})["error"]
    assert "order" in run_tool(both, "list_items", {"order": "sideways"})["error"]


def test_calendar_reconnect_notice(both):
    both.state(GCAL).set("auth_state", "needs_reconnect")
    r = run_tool(both, "search", {"query": "rent"})
    assert "Google Calendar needs reconnecting" in r["notice"]
    assert "notice" not in run_tool(both, "search", {"query": "rent", "source": "gmail"})
