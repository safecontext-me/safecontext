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
    assert ids_in(store, "messages") == {"m0001", "m0002", "m0003", "m0004", "m0005", "m0006"}
    assert store.get_meta("history_id") == "5000"
    assert store.get_meta("account_email") == "alex@example.org"
    assert store.get_meta("auth_state") == "ok"
    assert store.get_meta("backfill_page_token") is None


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
    assert store.get_meta("history_id") is None and store.get_meta("backfill_page_token")
    r2 = run(store, gmail)
    assert r2.status == "ok"
    assert ids_in(store, "messages") == {"m0001", "m0002", "m0003", "m0004", "m0005", "m0006"}
    assert max(gmail.fetched.values()) == 1  # nothing downloaded twice


def test_resume_after_failed_batch_mid_page(store, gmail):
    gmail.fail_get_call = 2
    assert run(store, gmail).status == "retry_later"
    assert run(store, gmail).status == "ok"
    assert len(ids_in(store, "messages")) == 6 and max(gmail.fetched.values()) == 1


def test_mail_arriving_during_backfill_is_caught(store, gmail):
    gmail.after_list = lambda n: n == 1 and gmail.add(msg(900, subject="new during backfill", text="hello"))
    run(store, gmail)
    gmail.after_list = None
    r = run(store, gmail)
    assert r.phase == "incremental" and store.read_message("m0900") is not None


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
    assert store.get_meta("history_id") == str(gmail.history_id)


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
    store.set_meta("history_id", "4000")
    r = run(store, gmail)
    assert r.status == "ok" and r.phase == "resync"
    assert store.read_message("m0003") is None and store.read_message("m0102") is not None
    assert store.get_meta("resync") is None and store.get_meta("history_id") == str(gmail.history_id)


# --- auth loss ----------------------------------------------------------------

def test_revoked_token_asks_to_reconnect_and_keeps_progress(store, gmail):
    gmail.after_list = lambda n: n == 1 and setattr(gmail, "revoked", True)
    r = run(store, gmail)
    assert r.status == "needs_reconnect" and "Reconnect Gmail" in r.message
    assert store.get_meta("auth_state") == "needs_reconnect"
    gmail.after_list, gmail.revoked = None, False
    r = run(store, gmail)
    assert r.status == "ok" and store.get_meta("auth_state") == "ok"
    assert len(ids_in(store, "messages")) == 6


def test_revoked_during_incremental(store, gmail):
    run(store, gmail)
    gmail.revoked = True
    assert run(store, gmail).status == "needs_reconnect"


# --- robustness -------------------------------------------------------------

def test_scrubber_version_change_refetches(store, gmail):
    run(store, gmail)
    store._db.execute("UPDATE messages SET scrubber_version='0' WHERE id='m0002'")
    run(store, gmail)
    assert gmail.fetched["m0002"] == 2
    assert store._db.execute("SELECT scrubber_version FROM messages WHERE id='m0002'").fetchone()[0] == "1"


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
    assert len(ids_in(store, "messages")) == 5


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
