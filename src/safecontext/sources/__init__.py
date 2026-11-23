"""Every source SafeContext can copy data from, and the registry the app,
CLI and tools use to reach them. See base.py for the connector interface."""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Sequence

from .. import auth
from ..store import Store
from .base import (FOLDER, MACOS_PERMISSION, OAUTH_SCOPE, BaseSource, ReaderSource, Requirement, Source,
                   SourceStatus)
from .gcal import GCAL, GoogleCalendarSource
from .gmail import GmailSource

# Shown on the Sources screen as disabled cards until their connectors land.
# Their readers live in safecontext/readers/ and are wrapped with ReaderSource.
COMING_SOON: List[Dict[str, Any]] = [
    {"id": "imessage", "name": "iMessage", "kinds": ["chat"],
     "description": "Your Messages conversations on this Mac, scrubbed before they're stored.",
     "requirements": [
         Requirement(MACOS_PERMISSION, "full_disk_access", "Full Disk Access",
                     "macOS keeps Messages history in a protected folder; SafeContext reads it there, "
                     "read-only, and never sends a message."),
         Requirement(MACOS_PERMISSION, "contacts", "Contacts",
                     "To show names instead of phone numbers. Contacts stay on this Mac.")]},
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
        self._by_id = {s.id: s for s in sources}

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
            d.update(description=s.description, kinds=list(s.kinds), available=True,
                     requirements=[r.__dict__ for r in s.requirements])
            out.append(d)
        for c in COMING_SOON:
            if c["id"] in self._by_id:
                continue
            out.append({**SourceStatus(id=c["id"], name=c["name"], state="coming_soon").to_dict(),
                        "description": c["description"], "kinds": c["kinds"], "available": False,
                        "requirements": [r.__dict__ for r in c["requirements"]]})
        return out


def default_sources(store: Store, *, gmail_factory=None, gmail_connect=None, calendar_factory=None,
                    calendar_connect=None, revoke_fn=None) -> Sources:
    return Sources(store, [
        GmailSource(store, api_factory=gmail_factory, connect_fn=gmail_connect, revoke_fn=revoke_fn),
        GoogleCalendarSource(store, api_factory=calendar_factory, connect_fn=calendar_connect, revoke_fn=revoke_fn),
    ])


# id -> display name, for every source that may have items in the store.
SOURCE_NAMES: Dict[str, str] = {"gmail": "Gmail", GCAL: "Google Calendar",
                                **{c["id"]: c["name"] for c in COMING_SOON}}


__all__ = ["BaseSource", "COMING_SOON", "FOLDER", "MACOS_PERMISSION", "OAUTH_SCOPE", "ReaderSource", "Requirement",
           "Source", "SourceStatus", "Sources", "default_sources", "SOURCE_NAMES"]
