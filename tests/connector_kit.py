"""The conformance checks every reader connector must pass before it ships.

A connector's test file subclasses `ConnectorFixture` (how to build the
connector over fixture data) and runs every check in `CHECKS`:

    from tests.connector_kit import CHECKS, ConnectorFixture

    class MyFixture(ConnectorFixture):
        ...

    @pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
    def test_my_connector_conforms(check, tmp_path):
        check(MyFixture(), tmp_path)

tests/test_connector_conformance.py runs them for every connector in the app.
The checks drive the connector through connect, sync, resume, failure,
permission and disconnect, and look at the store file itself to confirm that
sensitive text never reaches disk. Fixture data only: never real user data.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional, Sequence

import pytest

from safecontext.errors import NeedsPermission
from safecontext.sources import CONNECTORS
from safecontext.sources.base import (CONNECTED, NEEDS_PERMISSION, NOT_CONNECTED, ReaderSource,
                                      validate_connector)
from safecontext.store import Store

# Text that the scrubber must keep off disk, whatever the connector. Each is a
# whole record's text; the item may be dropped or redacted, but these strings
# must not appear anywhere in the store's files.
CANARIES = (
    "my card is 4111 1111 1111 1111 exp 04/29",
    "Your verification code is 482913. Don't share it.",
)
CANARY_SECRETS = (b"4111 1111 1111 1111", b"4111111111111111", b"482913")


class ConnectorFixture:
    """How the checks build and drive one connector. Subclass it in your test."""

    #: Set False only for an example connector that isn't in CONNECTORS.
    registered = True

    def make_source(self, store: Store, folder: Path) -> ReaderSource:
        """Your connector over fixture data created under `folder`: at least three
        ordinary records, with access granted. Called once per check."""
        raise NotImplementedError

    def add_records(self, texts: Sequence[str]) -> None:
        """Append one new record per text to the fixture data, with that text as
        its body, so the next sync picks them up."""
        raise NotImplementedError

    def deny_access(self) -> None:
        """Take away access (e.g. Full Disk Access) so check_access raises
        NeedsPermission. Leave unimplemented if the connector needs no permission."""
        raise NotImplementedError

    def allow_access(self) -> None:
        raise NotImplementedError


Check = Callable[[ConnectorFixture, Path], None]


def _setup(fx: ConnectorFixture, tmp_path: Path, connect: bool = True):
    store = Store(tmp_path / "data" / "mail.db")
    folder = tmp_path / "fixture"
    folder.mkdir(exist_ok=True)
    src = fx.make_source(store, folder)
    if connect:
        account = src.connect()
        assert account, "connect() must return an account label (e.g. 'This Mac')"
    return store, src


def check_declaration(fx: ConnectorFixture, tmp_path: Path) -> None:
    """The declaration is valid and, for a shipping connector, registered."""
    store, src = _setup(fx, tmp_path, connect=False)
    validate_connector(src)
    assert isinstance(src, ReaderSource), "new connectors are ReaderSources (see CONNECTORS.md)"
    if fx.registered:
        assert type(src) in CONNECTORS, f"add {type(src).__name__} to CONNECTORS in sources/__init__.py"
    assert src.status().state == NOT_CONNECTED


def check_first_sync(fx: ConnectorFixture, tmp_path: Path) -> None:
    """Connect, then a full sync: counts add up and every item has a declared kind."""
    store, src = _setup(fx, tmp_path)
    assert src.status().state == CONNECTED
    r = src.sync()
    assert r.status == "ok", r.message
    assert r.phase == "backfill"
    assert r.fetched >= 3, "make_source should give at least three records"
    assert r.kept + r.dropped == r.fetched and not r.errors
    assert store.counts(src.id) == (r.kept, r.dropped)
    for hit in store.list_items(source=src.id, limit=200):
        assert hit.kind in src.kinds
    st = src.status()
    assert st.state == CONNECTED and st.first_sync_done and st.last_sync_ms


def check_resume(fx: ConnectorFixture, tmp_path: Path) -> None:
    """A second sync fetches nothing; new records come in once, without duplicates."""
    store, src = _setup(fx, tmp_path)
    first = src.sync()
    again = src.sync()
    assert again.status == "ok" and again.phase == "incremental"
    assert again.fetched == 0, "a sync with no new records must fetch nothing (is the cursor saved?)"
    fx.add_records(["Lunch on Thursday?", "Sounds good, noon works"])
    more = src.sync()
    assert more.status == "ok" and more.fetched == 2
    kept, dropped = store.counts(src.id)
    assert kept + dropped == first.fetched + 2
    assert src.sync().fetched == 0


def check_bad_record(fx: ConnectorFixture, tmp_path: Path) -> None:
    """One record that to_item can't handle is stored as dropped; the sync goes on."""
    store, src = _setup(fx, tmp_path)
    original, seen = src.to_item, []

    def flaky(record):
        seen.append(record)
        if len(seen) == 2:
            raise ValueError("odd record")
        return original(record)

    src.to_item = flaky  # type: ignore[method-assign]
    r = src.sync()
    assert r.status == "ok" and r.errors == {"ValueError": 1}
    assert r.kept + r.dropped == r.fetched
    src.to_item = original  # type: ignore[method-assign]
    assert src.sync().fetched == 0, "the cursor must move past a record that failed"


def check_read_failure(fx: ConnectorFixture, tmp_path: Path) -> None:
    """A crash while reading never escapes sync(); it's reported and retried later."""
    store, src = _setup(fx, tmp_path)

    def broken(cursor: Optional[str]):
        raise RuntimeError("disk went away")
        yield  # pragma: no cover

    src.read = broken  # type: ignore[method-assign]
    r = src.sync()
    assert r.status == "retry_later" and r.message
    assert src.status().message
    assert store.counts(src.id) == (0, 0)


def check_undeclared_kind(fx: ConnectorFixture, tmp_path: Path) -> None:
    """An item whose kind the connector didn't declare is never stored as content."""
    store, src = _setup(fx, tmp_path)
    original = src.to_item
    other = next(k for k in ("message", "event", "chat", "transcript") if k not in src.kinds)

    def wrong_kind(record):
        item = original(record)
        if item is not None:
            item.kind = other
        return item

    src.to_item = wrong_kind  # type: ignore[method-assign]
    r = src.sync()
    assert r.status == "ok" and r.kept == 0 and set(r.errors) <= {"UndeclaredKind"}
    assert store.counts(src.id)[0] == 0


def check_permission(fx: ConnectorFixture, tmp_path: Path) -> None:
    """Missing access stops connect and sync with a NeedsPermission the app can explain."""
    store, src = _setup(fx, tmp_path, connect=False)
    try:
        fx.deny_access()
    except NotImplementedError:
        pytest.skip("this connector needs no permission")
    with pytest.raises(NeedsPermission):
        src.connect()
    assert not src.connected and src.status().state == NEEDS_PERMISSION
    assert src.missing_permission() is not None, "set permission_help for every permission you can raise"
    fx.allow_access()
    src.connect()
    fx.deny_access()
    r = src.sync()
    assert r.status == "needs_permission" and src.status().state == NEEDS_PERMISSION
    fx.allow_access()
    assert src.sync().status == "ok" and src.status().state == CONNECTED


def check_disconnect(fx: ConnectorFixture, tmp_path: Path) -> None:
    """Disconnecting deletes this connector's data and cursor; reconnecting starts over."""
    store, src = _setup(fx, tmp_path)
    first = src.sync()
    src.disconnect()
    assert store.counts(src.id) == (0, 0) and not src.connected
    src.connect()
    again = src.sync()
    assert again.phase == "backfill" and again.fetched == first.fetched


def check_secrets_never_stored(fx: ConnectorFixture, tmp_path: Path) -> None:
    """Card numbers and one-time codes in records never reach the store's files."""
    store, src = _setup(fx, tmp_path)
    src.sync()
    fx.add_records(list(CANARIES))
    r = src.sync()
    assert r.status == "ok" and r.fetched == len(CANARIES)
    store.close()
    files: List[Path] = [p for p in (tmp_path / "data").iterdir() if p.is_file()]
    assert files
    for p in files:
        data = p.read_bytes()
        for secret in CANARY_SECRETS:
            assert secret not in data, f"{secret!r} reached {p.name}"


CHECKS: List[Check] = [
    check_declaration, check_first_sync, check_resume, check_bad_record, check_read_failure,
    check_undeclared_kind, check_permission, check_disconnect, check_secrets_never_stored,
]
