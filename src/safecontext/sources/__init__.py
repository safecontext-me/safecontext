"""Every source SafeContext can copy data from, and the registry the app,
CLI and tools use to reach them. See base.py for the connector interface
and CONNECTORS.md for how to add one."""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Type

from .. import auth
from ..store import Store
from .base import (CONNECTOR_API_VERSION, FOLDER, MACOS_PERMISSION, NEEDS_PERMISSION, OAUTH_SCOPE, BaseSource,
                   ConnectorError, ReaderSource, Requirement, Source, SourceStatus, validate_connector)
from .gcal import GCAL, GoogleCalendarSource
from .gmail import GmailSource
from .imessage import IMESSAGE, IMessageSource

# Every connector that ships with SafeContext, in the order the Sources screen
# shows them. Adding a connector is one line here. Each is built as
# `cls(store, **options[cls.id])`, so tests and demo mode can point it at
# fixture data; with no options it reads the real thing.
CONNECTORS: Tuple[Type[BaseSource], ...] = (
    GmailSource,
    GoogleCalendarSource,
    IMessageSource,
)

# Shown on the Sources screen as disabled cards until their connectors land.
# Their readers live in safecontext/readers/ and are wrapped with ReaderSource.
COMING_SOON: List[Dict[str, Any]] = [
    {"id": "whatsapp", "name": "WhatsApp", "kinds": ["chat"],
     "description": "Chats from WhatsApp for Mac, read from its local database and scrubbed.",
     "requirements": [
         Requirement(MACOS_PERMISSION, "full_disk_access", "Full Disk Access",
                     "WhatsApp keeps its chat history in a protected app folder; SafeContext reads it there, "
                     "read-only.")]},
    {"id": "recordings", "name": "Call recordings", "kinds": ["transcript"],
     "description": "Transcripts of call recordings you choose, scrubbed before they're stored.",
     "requirements": [
         Requirement(FOLDER, "", "A folder you choose",
                     "SafeContext reads only the recordings in the folder you pick.")]},
]


class Sources:
    """The connected-or-connectable sources, in display order."""

    def __init__(self, store: Store, sources: Sequence[BaseSource]) -> None:
        self.store = store
        self._by_id: Dict[str, BaseSource] = {}
        for s in sources:
            validate_connector(s)
            if s.id in self._by_id:
                raise ConnectorError(f"Two connectors use the id '{s.id}'.")
            self._by_id[s.id] = s

    def __iter__(self) -> Iterator[BaseSource]:
        return iter(self._by_id.values())

    def get(self, source_id: str) -> BaseSource:
        src = self._by_id.get(source_id)
        if src is None:
            raise auth.NotConfigured(f"Unknown source '{source_id}'.")
        return src

    def ids(self) -> List[str]:
        return list(self._by_id)

    def connected(self) -> List[BaseSource]:
        return [s for s in self if s.connected]

    def link_allowed(self, url: str) -> bool:
        """Deep links the app may open: only the kinds of link our sources generate."""
        return isinstance(url, str) and any(url.startswith(p) for s in self for p in s.link_prefixes)

    def disconnect(self, source_id: str, wipe: bool = True) -> None:
        src = self.get(source_id)
        # One Google grant covers Gmail and Calendar: revoking it would cut off the other too.
        shared = any(o is not src and o.google and o.connected and o.account == src.account for o in self)
        src.disconnect(wipe=wipe, revoke=not (src.google and shared))

    def statuses(self) -> List[Dict[str, Any]]:
        out = []
        for s in self:
            d = s.status().to_dict()
            help_ = s.missing_permission() if d["state"] == NEEDS_PERMISSION else None
            d.update(description=s.description, kinds=list(s.kinds), available=True,
                     requirements=[r.__dict__ for r in s.requirements],
                     permission=help_.to_dict() if help_ else None)
            out.append(d)
        for c in COMING_SOON:
            if c["id"] in self._by_id:
                continue
            out.append({**SourceStatus(id=c["id"], name=c["name"], state="coming_soon").to_dict(),
                        "description": c["description"], "kinds": c["kinds"], "available": False,
                        "requirements": [r.__dict__ for r in c["requirements"]], "permission": None})
        return out


def default_sources(store: Store, *, gmail_factory=None, gmail_connect=None, calendar_factory=None,
                    calendar_connect=None, revoke_fn=None,
                    imessage_options: Optional[Dict[str, Any]] = None,
                    options: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Sources:
    """Every connector in CONNECTORS, built with `options[<source id>]` as keyword arguments.
    The named arguments are shorthands for the built-in connectors: `imessage_options` go to
    IMessageSource (db_path, access_fn, settings_fn, names_fn), so tests and demo mode point
    it at a fixture database instead of ~/Library/Messages."""
    opts: Dict[str, Dict[str, Any]] = {
        GmailSource.id: {"api_factory": gmail_factory, "connect_fn": gmail_connect, "revoke_fn": revoke_fn},
        GoogleCalendarSource.id: {"api_factory": calendar_factory, "connect_fn": calendar_connect,
                                  "revoke_fn": revoke_fn},
        IMessageSource.id: dict(imessage_options or {}),
    }
    for sid, o in (options or {}).items():
        opts.setdefault(sid, {}).update(o)
    return Sources(store, [cls(store, **opts.get(cls.id, {})) for cls in CONNECTORS])


# id -> display name, for every source that may have items in the store.
SOURCE_NAMES: Dict[str, str] = {**{cls.id: cls.name for cls in CONNECTORS},
                                **{c["id"]: c["name"] for c in COMING_SOON}}


__all__ = ["BaseSource", "COMING_SOON", "CONNECTORS", "CONNECTOR_API_VERSION", "ConnectorError", "FOLDER",
           "GCAL", "IMESSAGE", "MACOS_PERMISSION", "OAUTH_SCOPE", "ReaderSource", "Requirement", "Source",
           "SourceStatus", "Sources", "default_sources", "SOURCE_NAMES", "validate_connector"]
