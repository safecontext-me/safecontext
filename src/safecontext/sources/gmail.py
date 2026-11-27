"""Gmail as a source: the connector around sync.Syncer and auth's Gmail sign-in."""
from __future__ import annotations

import html
from typing import Any, Callable, Dict, Optional, Sequence

from .. import auth
from ..gmail_parse import parse_gmail_message
from ..scrub import RawItem
from ..store import GMAIL, Store, gmail_link
from ..sync import ProgressFn, Syncer
from .base import OAUTH_SCOPE, BaseSource, Requirement, SourceStatus

GmailFactory = Callable[[str, str], Any]           # (account, mode) -> GmailAPI
GmailConnect = Callable[[str, Optional[str]], str]  # (mode, email hint) -> account


def _real_connect(mode: str, email: Optional[str]) -> str:
    _, account = auth.connect(mode, login_hint=email or None)
    return account


def _real_gmail(account: str, mode: str):
    from ..gmail_api import GoogleGmailAPI
    return GoogleGmailAPI(auth.get_credentials(account, mode))


class GmailSource(BaseSource):
    id = GMAIL
    name = "Gmail"
    description = "Your email, scrubbed on this Mac. Attachments are never downloaded, only their names."
    kinds = ("message",)
    api_version = 1
    link_prefixes = ("https://mail.google.com/mail/",)
    google = True

    def __init__(self, store: Store, *, api_factory: Optional[GmailFactory] = None,
                 connect_fn: Optional[GmailConnect] = None, revoke_fn: Optional[Callable[..., None]] = None) -> None:
        super().__init__(store)
        self.api_factory = api_factory or _real_gmail
        self.connect_fn = connect_fn or _real_connect
        self.revoke_fn = revoke_fn

    @property
    def mode(self) -> str:
        return self.state.get("sync_mode") or "full"

    @property
    def requirements(self) -> Sequence[Requirement]:  # type: ignore[override]
        if self.mode == "headers":
            return (Requirement(OAUTH_SCOPE, auth.SCOPE_METADATA, "Gmail headers (read-only)",
                                "To store sender, recipients, subject and date. No message text."),)
        return (Requirement(OAUTH_SCOPE, auth.SCOPE_READONLY, "Read-only Gmail access",
                            "To download your mail and scrub it here. SafeContext can't send, delete or change mail."),)

    def first_sync_done(self) -> bool:
        return self.state.get("history_id") is not None

    def status(self) -> SourceStatus:
        out = super().status()
        if out.state == "needs_reconnect":
            out.message = auth.RECONNECT_MESSAGE
        return out

    def connect(self, mode: str = "full", email: Optional[str] = None, **_: Any) -> str:
        if mode not in auth.MODES:
            raise auth.NotConfigured("Unknown mode.")
        account = self.connect_fn(mode, email)
        previous = self.account
        if previous and previous != account:
            if not self.revoke_fn:  # a real sign-in just saved a token for the other account; forget it
                auth.TokenStore().delete(account)
            raise auth.NotConfigured(f"This Mac already holds mail for {previous}. Disconnect and delete it first "
                                     "to switch accounts.")
        self._mark_connected(account)
        self.state.set("sync_mode", mode)
        return account

    def _syncer(self, progress: Optional[ProgressFn]) -> Syncer:
        mode = self.mode
        return Syncer(self.store, self.api_factory(self.account, mode), mode=mode, progress=progress)

    can_fetch_skipped = True

    def fetch_skipped(self, ids: Sequence[str], full: bool = True) -> Dict[str, Optional[RawItem]]:
        """messages.get with the read-only scope Gmail already has (format=metadata for a list row,
        or in headers-only mode, where the full message isn't allowed)."""
        metadata = not full or self.mode == "headers"
        api = self.api_factory(self.account, self.mode)
        if hasattr(api, "_max_retries"):  # someone is waiting on screen: fail fast, don't back off for minutes
            api._max_retries = 2
        found = api.get_messages(list(ids), "metadata" if metadata else "full")
        out: Dict[str, Optional[RawItem]] = {}
        for i in ids:
            res = found.get(i)
            if res is None:
                out[i] = None
                continue
            item = _as_item(parse_gmail_message(res, metadata_only=metadata), self.account)
            if not full:  # a list row: Gmail's own short snippet stands in for the text
                item.text = html.unescape(res.get("snippet") or "")
            out[i] = item
        return out

    def disconnect(self, wipe: bool = True, revoke: bool = True) -> None:
        if self.account:
            (self.revoke_fn or auth.disconnect)(self.account, revoke=revoke)
        super().disconnect(wipe=wipe)
        if not wipe:
            self.state.set("last_error", None)  # auth_state=needs_reconnect shows RECONNECT_MESSAGE


def _as_item(m: Any, account: str) -> RawItem:
    """A fetched Gmail message (RawMessage) in the shape every other source uses."""
    people = [x for x in (m.header("to"), m.header("cc")) if x]
    return RawItem(id=m.id, kind="message", thread_id=m.thread_id, date_ms=m.internal_date_ms,
                   sender=m.header("from"), participants=people, title=m.header("subject"),
                   text=m.text_body, html=m.html_body, attachments=list(m.attachments),
                   link=gmail_link(account, m.header("message-id"), m.thread_id))
