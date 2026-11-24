import pytest

from safecontext.gmail_api import FULL_MODE_QUERY
from safecontext.store import Store
from safecontext.sync import Syncer
from safecontext.demo.fake_gmail import FakeGmail
from safecontext.demo.synthetic import all_needles, leak_matrix, mailbox, msg


@pytest.fixture
def gmail():
    g = FakeGmail(page_size=4)
    for m in mailbox():
        g.add(m, record=False)
    return g


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data" / "mail.db")
    yield s
    s.close()


def run(store, gmail, **kw):
    return Syncer(store, gmail, batch_size=3, **kw).run()


def ids_in(store, table):
    return {r[0] for r in store._db.execute(f"SELECT id FROM {table}")}


# --- backfill ---------------------------------------------------------------

def test_backfill(store, gmail):
    r = run(store, gmail)
    assert r.status == "ok" and r.phase == "backfill"
    assert ids_in(store, "items") == {"m0001", "m0002", "m0003", "m0004", "m0005", "m0006", "m0012"}
    assert store.get_state("gmail", "history_id") == "5000"
    assert store.get_state("gmail", "account") == "alex@example.org"
    assert store.get_state("gmail", "auth_state") == "ok"
    assert store.get_state("gmail", "backfill_page_token") is None


def test_full_mode_never_downloads_excluded_categories(store, gmail):
    run(store, gmail)
    assert set(gmail.queries) == {FULL_MODE_QUERY}
    assert "m0009" not in gmail.fetched and "m0010" not in gmail.fetched  # promotions, social
    assert "m0015" not in gmail.fetched                                    # spam


def test_headers_only_mode(store, gmail):
    r = run(store, gmail, mode="headers")
    assert r.status == "ok"
    assert set(gmail.queries) == {None} and set(gmail.formats) == {"metadata"}
    v = store.read_message("m0001")
    assert v.body == "" and v.subject == "Dinner Saturday?"
    assert {"m0009", "m0010"} <= ids_in(store, "dropped")  # labels still checked locally


def test_resume_after_interrupted_listing(store, gmail):
    gmail.fail_list_call = 2
    r1 = run(store, gmail)
    assert r1.status == "retry_later" and "resume" in r1.message
    assert store.get_state("gmail", "history_id") is None and store.get_state("gmail", "backfill_page_token")
    r2 = run(store, gmail)
    assert r2.status == "ok"
    assert ids_in(store, "items") == {"m0001", "m0002", "m0003", "m0004", "m0005", "m0006", "m0012"}
    assert max(gmail.fetched.values()) == 1  # nothing downloaded twice


def test_resume_after_failed_batch_mid_page(store, gmail):
    gmail.fail_get_call = 2
    assert run(store, gmail).status == "retry_later"
    assert run(store, gmail).status == "ok"
    assert len(ids_in(store, "items")) == 7 and max(gmail.fetched.values()) == 1


def test_mail_arriving_during_backfill_is_caught(store, gmail):
    gmail.after_list = lambda n: n == 1 and gmail.add(msg(900, subject="new during backfill", text="hello"))
    run(store, gmail)
    gmail.after_list = None
    r = run(store, gmail)
    assert r.phase == "incremental" and store.read_message("m0900") is not None


def _catching_up(store, gmail):
    """A Syncer that catches up after every backfill page (as if each page took minutes)."""
    sy = Syncer(store, gmail, batch_size=3)
    sy.catch_up_seconds = 0
    return sy


def test_new_mail_shows_up_while_backfill_still_runs(store, gmail):
    # Regression: a long first sync didn't fetch mail that arrived after it started until it
    # finished the whole mailbox (days for a big one), so the newest mail looked missing.
    seen_mid_backfill = []

    def after_list(n):
        if n == 1:
            gmail.add(msg(900, subject="new during backfill", text="hello"))
            gmail.delete("m0001")
        elif n == 2:
            seen_mid_backfill.append(store.read_message("m0900") is not None)

    gmail.after_list = after_list
    r = _catching_up(store, gmail).run()
    assert r.status == "ok" and r.phase == "backfill"
    assert seen_mid_backfill == [True]                        # stored before the backfill ended
    assert store.read_message("m0900") is not None            # and the final prune kept it
    assert store.read_message("m0001") is None                # deletion applied
    assert store.get_state("gmail", "history_id") == str(gmail.history_id)
    gmail.after_list = None
    r2 = run(store, gmail)
    assert r2.phase == "incremental" and r2.fetched == 0      # nothing left over for the next sync


def test_catch_up_survives_history_expiring_mid_backfill(store, gmail):
    def after_list(n):
        if n == 1:
            gmail.add(msg(901, subject="new while history expired", text="hi"))
            gmail.expire_history()  # the backfill's start id is now older than Gmail keeps

    gmail.after_list = after_list
    r = _catching_up(store, gmail).run()
    gmail.after_list = None
    assert r.status == "ok"
    assert store.read_message("m0901") is not None
    assert store.get_state("gmail", "history_id") == str(gmail.history_id)
    assert run(store, gmail).phase == "incremental"            # no resync loop afterwards


def test_backfill_resumed_after_catch_up_keeps_the_newer_start(store, gmail):
    gmail.after_list = lambda n: n == 1 and gmail.add(msg(902, text="hey"))
    gmail.fail_list_call = 3
    assert _catching_up(store, gmail).run().status == "retry_later"
    assert store.get_state("gmail", "backfill_start_history_id") == str(gmail.history_id)
    gmail.after_list = None
    assert run(store, gmail).status == "ok"
    assert store.read_message("m0902") is not None and max(gmail.fetched.values()) == 1


def test_deleted_between_list_and_get(store, gmail):
    gmail.before_get = lambda ids: "m0002" in ids and gmail.delete("m0002", record=False)
    r = run(store, gmail)
    assert r.status == "ok" and store.read_message("m0002") is None


# --- incremental --------------------------------------------------------------

def test_incremental_add_delete_relabel(store, gmail):
    run(store, gmail)
    gmail.add(msg(100, subject="Fresh news", text="brand new message"))
    gmail.delete("m0004")
    gmail.relabel("m0001", add=["TRASH"])
    gmail.relabel("m0006", add=["CATEGORY_PROMOTIONS"])
    r = run(store, gmail)
    assert r.status == "ok" and r.phase == "incremental"
    assert store.read_message("m0100").subject == "Fresh news"
    assert store.read_message("m0004") is None
    assert store.read_message("m0001") is None and "m0001" in ids_in(store, "dropped")
    assert store.read_message("m0006") is None
    assert store.get_state("gmail", "history_id") == str(gmail.history_id)


def test_restored_from_trash_comes_back(store, gmail):
    run(store, gmail)
    gmail.relabel("m0001", add=["TRASH"])
    run(store, gmail)
    gmail.relabel("m0001", remove=["TRASH"])
    run(store, gmail)
    assert store.read_message("m0001") is not None


def test_new_2fa_mail_is_dropped_on_incremental(store, gmail):
    run(store, gmail)
    gmail.add(msg(101, frm="Acct <accounts@example-app.com>", subject="Hello",
                  text="593017 is your Example code"))
    run(store, gmail)
    assert "m0101" in ids_in(store, "dropped") and store.search("593017") == []


def test_nothing_new_is_cheap(store, gmail):
    run(store, gmail)
    before = sum(gmail.fetched.values())
    assert run(store, gmail).fetched == 0 and sum(gmail.fetched.values()) == before


def test_expired_history_triggers_resync_and_prunes(store, gmail):
    run(store, gmail)
    gmail.delete("m0003", record=False)   # deletion we never hear about via history
    gmail.add(msg(102, text="arrived while offline"), record=False)
    gmail.expire_history()
    store.set_state("gmail", "history_id", "4000")
    r = run(store, gmail)
    assert r.status == "ok" and r.phase == "resync"
    assert store.read_message("m0003") is None and store.read_message("m0102") is not None
    assert store.get_state("gmail", "resync") is None and store.get_state("gmail", "history_id") == str(gmail.history_id)


# --- auth loss ----------------------------------------------------------------

def test_revoked_token_asks_to_reconnect_and_keeps_progress(store, gmail):
    gmail.after_list = lambda n: n == 1 and setattr(gmail, "revoked", True)
    r = run(store, gmail)
    assert r.status == "needs_reconnect" and "Reconnect Gmail" in r.message
    assert store.get_state("gmail", "auth_state") == "needs_reconnect"
    gmail.after_list, gmail.revoked = None, False
    r = run(store, gmail)
    assert r.status == "ok" and store.get_state("gmail", "auth_state") == "ok"
    assert len(ids_in(store, "items")) == 7


def test_revoked_during_incremental(store, gmail):
    run(store, gmail)
    gmail.revoked = True
    assert run(store, gmail).status == "needs_reconnect"


# --- robustness -------------------------------------------------------------

def test_scrubber_version_change_refetches(store, gmail):
    run(store, gmail)
    store._db.execute("UPDATE items SET scrubber_version='0' WHERE id='m0002'")
    run(store, gmail)
    assert gmail.fetched["m0002"] == 2
    assert store._db.execute("SELECT scrubber_version FROM items WHERE id='m0002'").fetchone()[0] == "1"


def test_one_bad_message_does_not_stop_sync(store, gmail):
    from safecontext.scrub import scrub

    def flaky(raw):
        if raw.id == "m0003":
            raise ValueError("boom")
        return scrub(raw)

    r = Syncer(store, gmail, batch_size=3, scrub_fn=flaky).run()
    assert r.status == "ok" and r.errors == {"ValueError": 1}
    assert store.read_message("m0003") is None
    assert store._db.execute("SELECT reason FROM dropped WHERE id='m0003'").fetchone()[0] == "error:ValueError"
    assert len(ids_in(store, "items")) == 6


def test_progress_callback(store, gmail):
    seen = []
    Syncer(store, gmail, batch_size=3, progress=lambda r: seen.append(r.fetched)).run()
    assert seen and seen == sorted(seen)


# --- end to end leak check through Gmail's wire format ------------------------

def test_full_sync_leaks_nothing_to_disk(tmp_path):
    g = FakeGmail(page_size=25)
    for m in mailbox() + leak_matrix():
        g.add(m, record=False)
    data = tmp_path / "data"
    s = Store(data / "mail.db")
    assert Syncer(s, g).run().status == "ok"
    s.close()
    blob = b"".join(p.read_bytes() for p in data.rglob("*") if p.is_file())
    leaks = [n for n in all_needles() if n.encode() in blob]
    assert not leaks, leaks
    assert b"BENIGN-MARKER" in blob


# --- a removed drop rule: mail it hid comes back on the next sync ----------------

def test_mail_dropped_by_retired_no_reply_rule_is_resynced(tmp_path, gmail):
    from safecontext.scrub import Dropped
    github = msg(950, frm="GitHub <noreply@github.com>",
                 subject="[GitHub] A third-party OAuth application has been added to your account",
                 text="A third-party OAuth application (Example CI) was recently authorized to access your account.")
    united = msg(951, frm="United Airlines <no-reply@united.example>", subject="Your trip to Denver",
                 text="Confirmation ABC123. Passenger Alex Kim. Seat 22C.")
    for m in (github, united):
        gmail.add(m, record=False)
    path = tmp_path / "data" / "mail.db"
    store = Store(path)
    assert run(store, gmail).status == "ok"
    # What an older SafeContext left behind: the no-reply rule dropped all three.
    old = ["m0012", "m0950", "m0951"]
    store.put_many(Dropped(i, f"t{i[1:]}", "bulk:no_reply_sender") for i in old)
    store.close()

    store = Store(path)  # the update's first start
    assert sorted(store.stale_ids()) == old
    gmail.fetched.clear()
    assert run(store, gmail).status == "ok"
    assert set(gmail.fetched) == set(old)  # only the retired rule's mail is downloaded again
    assert store.read_message("m0012") is not None  # shipping receipt: kept
    trip = store.read_message("m0951")
    assert trip is not None and "ABC123" not in trip.body and "[REDACTED:BOOKING_CODE]" in trip.body
    reasons = dict(store._db.execute("SELECT id, reason FROM dropped WHERE id IN ('m0950','m0951','m0012')"))
    assert reasons == {"m0950": "security:subject"}  # the OAuth notice: dropped under its new reason
    assert store.stale_ids() == []
    store.close()
