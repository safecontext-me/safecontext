"""The Security Dashboard: its period summary and the "Recently protected" list.

Fixtures span Gmail, Calendar, Messages and Dropbox, with skipped items and kept items that had
details blanked out. Covers which rows show and in what order, keyset paging, the period counts,
that a removed value can never come back out (it was never stored), the indexes the queries use,
and the page's wiring.
"""
import json
import re
from pathlib import Path

import pytest

from safecontext.app.backend import DASHBOARD_PERIODS, Backend
from safecontext.scrub import Dropped, ScrubbedItem, ScrubbedMessage
from safecontext.scrub.redact import redact
from safecontext.store import Store

ACCOUNT = "alex@example.org"
DAY = 86_400_000
NOW = 1_800_000_000_000
UI = Path(__file__).resolve().parents[1] / "src" / "safecontext" / "app" / "ui"

# Values the scrubber removes. None of them may appear in anything the dashboard returns.
SECRETS = ["415-555-0134", "4111 1111 1111 1111", "551902", "12 Oak Street"]
RAW = "Call me at 415-555-0134 or card 4111 1111 1111 1111, code 551902, at 12 Oak Street, Springfield, IL 62704"


def mail(i, date_ms, text="Lunch on Friday?", thread=None):
    body, red = redact(text)
    return ScrubbedMessage(f"g{i}", thread or f"t{i}", date_ms, "Jamie <jamie@example.com>", ACCOUNT, "",
                           f"Subject {i}", body, f"<{i}@example.com>", [], red, "1")


def item(i, kind, date_ms, text):
    body, red = redact(text)
    return ScrubbedItem(f"x{i}", kind, f"x{i}", date_ms, None, "Sam", [], f"Title {i}", body, "", [], {}, red, "1")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "mail.db", account=ACCOUNT)
    s.put_many([mail(1, NOW - 1 * DAY, RAW), mail(2, NOW - 2 * DAY),  # g2: nothing blanked, not in the list
                mail(3, NOW - 40 * DAY, RAW, thread="shared"),
                Dropped("g4", "shared", "sensitive:sender_domain", NOW - 3 * DAY),  # thread has a kept subject
                Dropped("g5", "t5", "security:subject", NOW - 10 * DAY),
                Dropped("g6", "t6", "draft", 0)])  # date unknown: not in the list
    s.put_many([item(1, "event", NOW - 4 * DAY, "Dial in 415-555-0134"),
                item(2, "event", NOW + 5 * DAY, "Call 415-555-0134")], source="gcal")  # upcoming: left out
    s.put_many([item(3, "chat", NOW - 5 * DAY, "code 551902"), Dropped("m4", "m4", "business_sender", NOW - 6 * DAY)],
               source="imessage")
    s.put_many([item(5, "message", NOW - 8 * DAY, "card 4111 1111 1111 1111")], source="dropbox")
    return s


def ids(page):
    return [r["id"] for r in page["rows"]]


# --- the list ---------------------------------------------------------------------------------

def test_feed_interleaves_sources_newest_first(store):
    page = store.protected_feed(until_ms=NOW)
    assert ids(page) == ["g1", "g4", "x1", "x3", "m4", "x5", "g5", "g3"]
    assert page["next"] is None
    by = {r["id"]: r for r in page["rows"]}
    assert {by[i]["source"] for i in by} == {"gmail", "gcal", "imessage", "dropbox"}
    assert by["g1"]["type"] == "thread" and by["g1"]["redactions"]["PHONE"] == 1
    assert by["g4"]["type"] == "dropped" and by["g4"]["reason"] == "sensitive:sender_domain"
    assert by["g4"]["thread_title"] == "Subject 3"  # another message of its thread was kept
    assert by["m4"]["thread_title"] == "" and "link" in by["g4"]


def test_feed_filters(store):
    assert ids(store.protected_feed(until_ms=NOW, view="skipped")) == ["g4", "m4", "g5"]
    assert ids(store.protected_feed(until_ms=NOW, view="scrubbed")) == ["g1", "x1", "x3", "x5", "g3"]
    with pytest.raises(ValueError):
        store.protected_feed(until_ms=NOW, view="everything")


def test_feed_pages_without_gaps_or_repeats(store):
    seen, cursor = [], None
    for _ in range(10):
        page = store.protected_feed(until_ms=NOW, cursor=cursor, limit=3)
        seen += ids(page)
        cursor = page["next"]
        if not cursor:
            break
    assert seen == ids(store.protected_feed(until_ms=NOW))


def test_feed_pages_across_equal_dates(tmp_path):
    s = Store(tmp_path / "mail.db", account=ACCOUNT)
    s.put_many([mail(i, NOW - DAY, RAW) for i in range(5)] + [Dropped(f"d{i}", f"d{i}", "draft", NOW - DAY) for i in range(5)])
    seen, cursor = [], None
    while True:
        page = s.protected_feed(until_ms=NOW, cursor=cursor, limit=4)
        seen += ids(page)
        if not (cursor := page["next"]):
            break
    assert sorted(seen) == sorted([f"g{i}" for i in range(5)] + [f"d{i}" for i in range(5)])
    assert len(seen) == len(set(seen))


def test_feed_never_shows_a_removed_value(store):
    out = json.dumps([store.protected_feed(until_ms=NOW, view=v) for v in ("all", "skipped", "scrubbed")])
    for secret in SECRETS:
        assert secret not in out
    assert "[REDACTED:PHONE]" in out  # the placeholder the stored copy has, nothing more


# --- the period summary ------------------------------------------------------------------------

def test_summary_counts_only_the_period(store):
    week = store.protected_summary(NOW - 7 * DAY, NOW)
    assert week["dropped_by_reason"] == {"sensitive:sender_domain": 1, "business_sender": 1}
    assert week["kept_by_source"] == {"gmail": 2, "gcal": 1, "imessage": 1}
    assert week["redactions"] == {"PHONE": 2, "CREDIT_CARD": 1, "ONE_TIME_CODE": 2, "STREET_ADDRESS": 1}
    assert week["redacted_items"] == 3
    every = store.protected_summary(None, NOW)
    assert sum(every["dropped_by_reason"].values()) == 4 and every["redacted_items"] == 6


def test_dashboard_queries_use_their_indexes(store):
    def plan(sql, args):
        return " ".join(r[3] for r in store._db.execute("EXPLAIN QUERY PLAN " + sql, args))
    p = plan(f"SELECT {Store._FEED_COLS} FROM items m INDEXED BY ix_items_redacted_date WHERE m.redactions != '{{}}' "
             "AND m.date_ms <= ? ORDER BY m.date_ms DESC, m.rowid DESC LIMIT 25", [NOW])
    assert "ix_items_redacted_date" in p and "TEMP B-TREE" not in p
    p = plan("SELECT d.rowid FROM dropped d INDEXED BY ix_dropped_date WHERE d.date_ms > 0 AND d.date_ms <= ? "
             "ORDER BY d.date_ms DESC, d.rowid DESC LIMIT 25", [NOW])
    assert "ix_dropped_date" in p and "TEMP B-TREE" not in p


def test_existing_store_gains_the_dashboard_index(tmp_path):
    p = tmp_path / "mail.db"
    s = Store(p)
    s._db.execute("DROP INDEX ix_items_redacted_date")
    s.close()
    s = Store(p)
    assert s._db.execute("SELECT 1 FROM sqlite_master WHERE name='ix_items_redacted_date'").fetchone()


# --- backend --------------------------------------------------------------------------------------

@pytest.fixture
def backend(store, monkeypatch):
    b = Backend.__new__(Backend)  # only the store and the source names are needed here
    b._store = store
    b._sources = [type("S", (), {"id": sid, "name": name})() for sid, name in
                  (("gmail", "Gmail"), ("gcal", "Google Calendar"), ("imessage", "Messages"), ("dropbox", "Dropbox"))]
    monkeypatch.setattr(Backend, "_minute_now", staticmethod(lambda: NOW))
    return b


def test_dashboard_uses_plain_labels_and_periods(backend):
    r = backend.dashboard("week")
    assert r["ok"] and r["in_period"] == {"kept": 4, "dropped": 2, "blanked": 6, "blanked_items": 3}
    assert r["totals"] == {"kept": 7, "dropped": 4, "blanked": 12}
    assert dict(r["skipped"]) == {"Banks, tax & health senders": 1, "Texts from businesses & shortcodes": 1}
    assert r["kept_by_source"][0] == ["Gmail", 2]
    assert "Phone numbers" in dict(r["blanked"]) and not any(re.fullmatch(r"[A-Z_]+", k) for k, _ in r["blanked"])
    assert backend.dashboard("all")["in_period"]["dropped"] == 4
    assert backend.dashboard("year") == {"ok": False, "error": "Something went wrong (ValueError)."}


def test_dashboard_feed_explains_skipped_rows(backend):
    r = backend.dashboard_feed("skipped")
    assert r["ok"] and [x["reason_info"]["label"] for x in r["rows"]] == [
        "Bank/financial, tax or health sender", "Business or automated text", "Security code or sign-in"]
    out = json.dumps(backend.dashboard_feed("all", None, 50))
    assert not any(s in out for s in SECRETS)


# --- the page ---------------------------------------------------------------------------------

def test_page_wiring():
    html, js = (UI / "index.html").read_text(), (UI / "app.js").read_text()
    page = html[html.index('<section id="view-removed"'):]
    page = page[:page.index("</section>")]
    for el_id in ("dash-title", "dash-sub", "dash-period", "dash-filter", "dash-rows", "dash-more",
                  "t-dropped", "t-kept", "t-redacted", "m-dropped", "m-kept", "m-redacted", "dropped-bars"):
        assert f'id="{el_id}"' in page
    assert set(re.findall(r'data-period="(\w+)"', page)) == set(DASHBOARD_PERIODS)
    assert set(re.findall(r'data-view="(\w+)"', page)) == {"all", "skipped", "scrubbed"}
    assert "api().dashboard(" in js and "api().dashboard_feed(" in js
    # the stat cards come before the list, which comes before the full lists
    assert page.index('id="t-dropped"') < page.index('id="dash-rows"') < page.index('id="dropped-bars"')
    # a row opens the item in the reader, the way a search result does
    row = js[js.index("function dashOpen"):]
    assert "mailOpen(r, true)" in row[:row.index("\n}\n")]


def test_rows_never_use_bold_for_skipped_or_scrubbed():
    """Bold means unread in the Mail view; the label at the front is the only difference."""
    css = (UI / "app.css").read_text()
    dash = "\n".join(line for line in css.splitlines() if line.startswith((".dash-row", ".dash-who", ".dash-line")))
    assert "font-weight" not in dash


def test_skipped_rows_fetch_live_and_are_forgotten_on_leaving():
    """Skipped rows ask app/skipped.py for a preview (memory only), and leaving the dashboard drops it."""
    js = (UI / "app.js").read_text()
    assert "api().skipped_previews(" in js
    show = js[js.index("function show(view)"):]
    show = show[:show.index("\n}\n")]
    assert 'if (view !== "removed") dashForgetSkipped();' in show
    forget = js[js.index("function dashForgetSkipped"):]
    assert "api().skipped_forget()" in forget[:forget.index("\n}\n")]
