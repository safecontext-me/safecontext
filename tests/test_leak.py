"""End-to-end: scrub a synthetic mailbox, write it through the real Store
(SQLite + FTS5, WAL mode) plus a debug log, then scan every byte in the
data directory for every planted secret, both while the database is open
(WAL and SHM side files present) and after it is closed and checkpointed."""
import dataclasses
import json
import logging

from mycontext.scrub import Dropped, ScrubbedMessage, scrub
from mycontext.store import Store
from mycontext.demo.synthetic import BENIGN_MARKER, all_needles, leak_matrix, mailbox


def _scan(data_dir):
    blobs = {p.name: p.read_bytes() for p in data_dir.rglob("*") if p.is_file()}
    leaks = []
    for needle in all_needles():
        for enc in ("utf-8", "utf-16-le"):
            pat = needle.encode(enc)
            leaks += [(needle, enc, name) for name, blob in blobs.items() if pat in blob]
    return blobs, leaks


def _log_results(data_dir, results):
    log = logging.getLogger("mycontext.leaktest")
    handler = logging.FileHandler(str(data_dir / "sync.log"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    for r in results:
        log.debug("result %s", json.dumps(dataclasses.asdict(r)) if isinstance(r, ScrubbedMessage) else repr(r))
    handler.close()
    log.removeHandler(handler)


def test_no_secret_reaches_disk(tmp_path):
    data_dir = tmp_path / "data"
    messages = mailbox() + leak_matrix()
    results = [scrub(m) for m in messages]

    store = Store(data_dir / "mail.db", account="alex@example.org")
    store.put_many(results)
    _log_results(data_dir, results)

    blobs, leaks = _scan(data_dir)
    assert any(name.endswith("-wal") for name in blobs)
    assert not leaks, f"secrets found on disk (db open): {leaks}"

    # Guard against a vacuous pass: real content was stored and is searchable.
    kept = [r for r in results if isinstance(r, ScrubbedMessage)]
    assert len(kept) >= len(messages) * 0.8
    assert store.search("BENIGN MARKER kept content")
    assert BENIGN_MARKER.encode() in b"".join(blobs.values())

    store.close()
    _, leaks = _scan(data_dir)
    assert not leaks, f"secrets found on disk (after checkpoint): {leaks}"


def test_no_secret_in_scrubbed_objects():
    """Same check one layer up, with a precise message on failure."""
    for m in mailbox() + leak_matrix():
        text = repr(scrub(m))
        for needle in all_needles():
            assert needle not in text, (m.id, needle, text)


def test_dropped_results_only_store_reason():
    for m in mailbox() + leak_matrix():
        r = scrub(m)
        if isinstance(r, Dropped):
            assert set(dataclasses.asdict(r)) == {"id", "thread_id", "reason", "date_ms", "detail"}
            assert r.detail is None
