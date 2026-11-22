"""Sync status visible across processes: heartbeat, single-sync lock, quota waits."""
import json
import os

import pytest

from mycontext.app.backend import Backend
from mycontext.app.demo import DEMO_ACCOUNT, DemoGrok
from mycontext.demo.fake_gmail import FakeGmail
from mycontext.demo.synthetic import mailbox
from mycontext.store import Store
from mycontext.sync import HEARTBEAT_KEY, Syncer, _SyncLock, read_heartbeat


@pytest.fixture
def gmail():
    g = FakeGmail(page_size=5, email=DEMO_ACCOUNT)
    for m in mailbox():
        g.add(m, record=False)
    return g


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "mail.db")
    s.set_meta("account_email", DEMO_ACCOUNT)
    return s


def test_heartbeat_written_during_sync_and_cleared_after(store, gmail):
    seen = []
    Syncer(store, gmail, batch_size=3, progress=lambda r: seen.append(read_heartbeat(store))).run()
    beats = [b for b in seen if b]
    assert beats and beats[-1]["pid"] == os.getpid()
    assert beats[-1]["fetched"] > 0 and beats[-1]["phase"] == "backfill"
    assert store.get_meta(HEARTBEAT_KEY) is None
    assert read_heartbeat(store) is None


def test_heartbeat_holds_no_message_content(store, gmail):
    seen = []
    Syncer(store, gmail, batch_size=3, progress=lambda r: seen.append(store.get_meta(HEARTBEAT_KEY))).run()
    for raw in filter(None, seen):
        assert set(json.loads(raw)) <= {"pid", "phase", "fetched", "kept", "dropped", "deleted",
                                        "started_ms", "updated_ms", "per_min", "waiting_until_ms"}


def test_heartbeat_from_dead_process_is_ignored(store):
    store.set_meta(HEARTBEAT_KEY, json.dumps({"pid": 2 ** 22 + 12345, "fetched": 5}))
    assert read_heartbeat(store) is None


def test_second_sync_is_refused_while_one_runs(store, gmail):
    lock = _SyncLock(store.path.parent / "sync.lock")
    assert lock.acquire()
    try:
        r = Syncer(store, gmail).run()
        assert r.status == "busy" and r.fetched == 0
        assert store.known_ids([m.id for m in mailbox()]) == set()
    finally:
        lock.release()
    assert Syncer(store, gmail).run().status == "ok"


def test_quota_wait_is_reported(store, gmail):
    waits = []

    class WaitingGmail:
        wait_hook = None

        def __getattr__(self, name):
            return getattr(gmail, name)

        def get_messages(self, ids, fmt):
            self.wait_hook(65.0)
            waits.append(read_heartbeat(store)["waiting_until_ms"])
            self.wait_hook(None)
            waits.append(read_heartbeat(store)["waiting_until_ms"])
            return gmail.get_messages(ids, fmt)

    api = WaitingGmail()
    Syncer(store, api, batch_size=50).run()
    assert waits[0] and waits[1] is None
    assert api.wait_hook is None  # unhooked after the run


def test_app_shows_a_sync_running_in_another_process(tmp_path, gmail):
    b = Backend(tmp_path / "data", demo=True, gmail_factory=lambda a, m: gmail,
                connect_fn=lambda mode, email: DEMO_ACCOUNT, grok_client_factory=DemoGrok, auto_sync=False)
    try:
        b._store.set_meta("account_email", DEMO_ACCOUNT)
        parent = os.getppid()  # a live process that isn't us, standing in for the CLI
        b._store.set_meta(HEARTBEAT_KEY, json.dumps(
            {"pid": parent, "phase": "backfill", "fetched": 120, "kept": 70, "dropped": 50,
             "started_ms": 1, "per_min": 55.0, "waiting_until_ms": None}))
        s = b.status()
        assert s["syncing"] and s["progress"]["elsewhere"] and s["progress"]["per_min"] == 55.0
        assert s["progress"]["backfill"] is True
        assert b.sync_now()["started"] is False  # never double-fetch against Gmail's quota
    finally:
        b._store.set_meta(HEARTBEAT_KEY, None)
        b._shutdown()
