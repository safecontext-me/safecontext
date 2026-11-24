"""Every connector passes the conformance kit (tests/connector_kit.py), and the
registry rejects connectors that break the interface.

A new connector adds its fixture class here and to FIXTURES."""
import json
import os
from pathlib import Path
from typing import Sequence

import pytest

from safecontext.demo.fake_messages import FakeFullDiskAccess, add_message, demo_names, make_messages_db
from safecontext.scrub import RawItem, scrub_item
from safecontext.sources import CONNECTORS, ConnectorError, Sources, default_sources, validate_connector
from safecontext.sources.base import FOLDER, ReaderSource, Requirement
from safecontext.sources.imessage import IMessageSource
from safecontext.store import Store
from tests.connector_kit import CHECKS, ConnectorFixture
from tests.example_connector import ChatExportSource


class IMessageFixture(ConnectorFixture):
    def make_source(self, store, folder):
        self.db = make_messages_db(folder / "chat.db")
        self.access = FakeFullDiskAccess(granted=True)
        return IMessageSource(store, db_path=self.db, access_fn=self.access.check,
                              settings_fn=self.access.open_settings, names_fn=demo_names)

    def add_records(self, texts: Sequence[str]) -> None:
        for t in texts:
            add_message(self.db, "+14155550134", "+14155550134", t)

    def deny_access(self):
        self.access.granted = False

    def allow_access(self):
        self.access.granted = True


class ChatExportFixture(ConnectorFixture):
    registered = False  # the example in tests/, not shipped

    def make_source(self, store, folder):
        self.path = folder / "export.jsonl"
        self.n = 0
        self.add_records(["Dinner Saturday?", "Yes! 7pm works", "I'll book the table"])
        return ChatExportSource(store, path=self.path)

    def add_records(self, texts: Sequence[str]) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            for t in texts:
                self.n += 1
                f.write(json.dumps({"id": f"m{self.n}", "chat": "family", "from": "Alex", "text": t,
                                    "ts": 1_700_000_000_000 + self.n}) + "\n")

    def deny_access(self):
        os.chmod(self.path, 0)

    def allow_access(self):
        os.chmod(self.path, 0o600)


# Gmail and Google Calendar predate the kit and sync through their own API syncers;
# tests/test_sync.py and tests/test_gcal.py cover them.
FIXTURES = {"imessage": IMessageFixture, "chat_export": ChatExportFixture}
API_SOURCES = {"gmail", "gcal"}


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
@pytest.mark.parametrize("source_id", sorted(FIXTURES))
def test_connector_conforms(source_id, check, tmp_path):
    check(FIXTURES[source_id](), tmp_path)


def test_every_registered_connector_has_a_conformance_fixture():
    missing = {cls.id for cls in CONNECTORS} - set(FIXTURES) - API_SOURCES
    assert not missing, f"add a ConnectorFixture for {sorted(missing)} to FIXTURES"


def test_built_in_connectors_are_valid(tmp_path):
    store = Store(tmp_path / "mail.db")
    sources = default_sources(store)
    assert sources.ids() == [cls.id for cls in CONNECTORS]


def test_options_reach_a_connector_by_id(tmp_path):
    store = Store(tmp_path / "mail.db")
    db = make_messages_db(tmp_path / "chat.db")
    src = default_sources(store, options={"imessage": {"db_path": db}}).get("imessage")
    assert src.db_path == db


# --- what the registry rejects -------------------------------------------------

class Good(ReaderSource):
    id = "good_one"
    name = "Good"
    description = "A valid connector."
    kinds = ("chat",)
    api_version = 1
    requirements = (Requirement(FOLDER, "", "A folder you choose", "To read only that folder."),)


def bad(**attrs):
    return type("Bad", (Good,), attrs)


@pytest.mark.parametrize("attrs, message", [
    ({"id": "Bad Id"}, "id must be"),
    ({"name": ""}, "name is empty"),
    ({"description": ""}, "description is empty"),
    ({"api_version": 0}, "api_version is 0"),
    ({"api_version": 2}, "api_version is 2"),
    ({"kinds": ()}, "kinds is empty"),
    ({"kinds": ("note",)}, "unknown kinds ['note']"),
    ({"link_prefixes": ("file://",)}, "link prefix 'file://'"),
    ({"link_prefixes": ("https://",)}, "link prefix 'https://'"),
    ({"requirements": (Requirement("keychain", "x", "Keychain", "why"),)}, "requirement kind 'keychain'"),
    ({"requirements": (Requirement(FOLDER, "", "", ""),)}, "needs a label and a why"),
    ({"sync": lambda self, progress=None: None}, "must not override sync()"),
    ({"_syncer": lambda self, progress: None}, "must not override _syncer()"),
])
def test_validate_rejects(attrs, message, tmp_path):
    store = Store(tmp_path / "mail.db")
    validate_connector(Good(store))
    with pytest.raises(ConnectorError, match=message.replace("(", r"\(").replace(")", r"\)")
                       .replace("[", r"\[").replace("]", r"\]")):
        validate_connector(bad(**attrs)(store))


def test_registry_rejects_invalid_and_duplicate_connectors(tmp_path):
    store = Store(tmp_path / "mail.db")
    with pytest.raises(ConnectorError):
        Sources(store, [bad(kinds=("note",))(store)])
    with pytest.raises(ConnectorError, match="Two connectors use the id 'good_one'"):
        Sources(store, [Good(store), Good(store)])


def test_scrubber_refuses_unknown_kinds():
    with pytest.raises(ValueError, match="unknown item kind"):
        scrub_item(RawItem(id="1", kind="note", thread_id="t", date_ms=0, text="hello"))
