"""iMessage (and SMS/RCS) from the Messages database on this Mac, through readers/imessage.py.

Reading ~/Library/Messages/chat.db needs Full Disk Access. macOS has no prompt
for it, so nothing is asked for silently: `check_access` only looks (reading
16 bytes at most), and when access is missing the Sources screen explains it
and offers a button that opens the right System Settings pane.

Every message is scrubbed (scrub.scrub_item, kind "chat") before it's stored,
like mail and calendar events. Tapbacks, group events and unsent messages
are skipped by the reader.
"""
from __future__ import annotations

import logging
import os
from datetime import timezone
from typing import Callable, Dict, Iterator, Optional

from .. import auth
from ..errors import NeedsPermission
from ..readers import imessage as reader
from ..scrub import RawItem
from ..scrub.models import Attachment
from ..store import Store
from .base import FULL_DISK_ACCESS, MACOS_PERMISSION, ReaderSource, Requirement, full_disk_access_help

log = logging.getLogger("safecontext.sources.imessage")

IMESSAGE = "imessage"
ME = "Me"

AccessFn = Callable[[Optional[os.PathLike]], reader.AccessStatus]
NamesFn = Callable[[], Callable[[str], Optional[str]]]


def _contact_names() -> Callable[[str], Optional[str]]:
    """Names from the Contacts database files on this Mac (covered by Full Disk Access).
    Never the Contacts framework, which would show a Contacts prompt."""
    try:
        from ..readers.contacts import load_contacts
        return load_contacts(use_framework=False).lookup
    except Exception:  # no Contacts data, unreadable, odd format: fall back to handles
        log.info("contacts unavailable; showing handles", exc_info=True)
        return lambda handle: None


class IMessageSource(ReaderSource):
    id = IMESSAGE
    name = "iMessage"
    kinds = ("chat",)
    api_version = 1
    description = "Your Messages conversations on this Mac (iMessage, SMS and RCS), scrubbed before they're stored."
    requirements = (
        Requirement(MACOS_PERMISSION, FULL_DISK_ACCESS, "Full Disk Access",
                    "macOS keeps your Messages history in a protected folder. SafeContext reads it there, "
                    "read-only, and never sends or changes a message. It also lets SafeContext show names "
                    "from Contacts instead of phone numbers."),
    )

    def __init__(self, store: Store, *, db_path: Optional[os.PathLike] = None,
                 access_fn: Optional[AccessFn] = None, settings_fn: Optional[Callable[[], bool]] = None,
                 names_fn: Optional[NamesFn] = None) -> None:
        super().__init__(store)
        self.db_path = db_path            # None: ~/Library/Messages/chat.db
        self._access = access_fn or reader.check_access
        self._open_settings = settings_fn or reader.open_settings
        self._names_fn = names_fn or _contact_names
        self.permission_help = {FULL_DISK_ACCESS: full_disk_access_help("your Messages history")}
        self._convs: Dict[str, reader.Conversation] = {}
        self._name: Callable[[str], Optional[str]] = lambda h: None

    # --- access ---------------------------------------------------------------
    def check_access(self) -> None:
        try:
            st = self._access(self.db_path)
        except PermissionError:  # even stat() refused: treat it like missing Full Disk Access
            st = reader.AccessStatus(reader.AccessState.NO_PERMISSION, "", str(self.db_path or ""))
        self._raise_for(st)

    def _raise_for(self, st: reader.AccessStatus) -> None:
        if st.state is reader.AccessState.OK:
            return
        if st.state is reader.AccessState.NO_PERMISSION:
            raise NeedsPermission("SafeContext doesn't have Full Disk Access yet, so it can't read your "
                                  "Messages history.", FULL_DISK_ACCESS)
        raise auth.NotConfigured(st.message)  # no Messages data on this Mac, or an unreadable database

    def open_settings(self, requirement: str) -> bool:
        return requirement == FULL_DISK_ACCESS and bool(self._open_settings())

    # --- reading ----------------------------------------------------------------
    def read(self, cursor: Optional[str]) -> Iterator[reader.Message]:
        try:
            r = reader.MessagesReader(self.db_path)
        except reader.MessagesAccessError as e:
            self._raise_for(e.status)
            raise
        with r:
            self._convs = {c.chat_id: c for c in r.conversations()}
            self._name = self._names_fn()
            end = r.max_rowid()  # messages that arrive during this sync wait for the next one
            yield from r.messages(since_rowid=int(cursor or 0), until_rowid=end)

    def cursor_of(self, m: reader.Message) -> str:
        return str(m.rowid)

    def _who(self, handle: Optional[str]) -> str:
        if not handle:
            return ""
        return self._name(handle) or handle

    def _title(self, conv: Optional[reader.Conversation], m: reader.Message) -> str:
        if conv is None:
            return self._who(m.sender) or "Messages"
        if conv.display_name:
            return conv.display_name
        if not conv.is_group:
            return self._who(conv.participants[0] if conv.participants else conv.identifier)
        names = [self._who(h) for h in conv.participants]
        return ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "") or "Group chat"

    def to_item(self, m: reader.Message) -> Optional[RawItem]:
        if m.reaction is not None:
            return None
        conv = self._convs.get(m.chat_id or "")
        sender = ME if m.is_from_me else self._who(m.sender)
        people = [self._who(h) for h in (conv.participants if conv else (m.sender,)) if h]
        extra: Dict[str, str] = {}
        if m.service and m.service != "iMessage":
            extra["Service"] = m.service
        if m.edited:
            extra["Edited"] = "yes"
        # The raw handle, for the chat drop rules (a bank saved in Contacts is still a shortcode).
        # My own messages in a one-to-one chat count as that chat's: replies to a pharmacy go too.
        if m.is_from_me:
            handle = conv.identifier if conv is not None and not conv.is_group else ""
        else:
            handle = m.sender or ""
        ts = m.timestamp
        return RawItem(
            id=m.guid,
            kind="chat",
            thread_id=m.chat_id or f"chat-{m.chat_rowid or 0}",
            date_ms=int(ts.astimezone(timezone.utc).timestamp() * 1000) if ts else 0,
            sender=sender,
            participants=[ME, *dict.fromkeys(people)],
            title=self._title(conv, m),
            text=m.text or "",
            attachments=[Attachment(a.name or "attachment", a.mime_type or "", 0) for a in m.attachments],
            extra=extra,
            sender_handle=handle,
        )
