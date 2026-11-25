"""Dropbox as a source.

Sign-in is Dropbox's own OAuth (see dropbox_auth.py), separate from the Google
grant. Sync lists the account from the root, then follows Dropbox's cursor so
later syncs fetch only changes. Text documents are copied and scrubbed on this
Mac. Everything else keeps its name, type, and size — the bytes are not
downloaded. Folders are not items.

Files are stored as kind "message" because that is the document shape the app
already shows (a title, a body, a date, a link) and new kinds are not allowed.
The sender is left blank on purpose: the scrubber reads the sender as an email
From address, and a work address at a clinic or a bank would make it drop
every file. The account email is stored on the row by the store, and the path
is kept in extra.

Email-only drop rules look at Gmail labels and bulk headers. A file has
neither, so those rules do not match. The universal rules still run: a file
named like a tax form is dropped, and card numbers in a notes file are
redacted.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from .. import auth, dropbox_auth
from ..dropbox_api import CursorExpired, DropboxAPI, NotDownloadable
from ..errors import TransientError
from ..scrub import Attachment, RawItem, scrub_item
from ..store import Store, to_ms
from ..sync import BaseSyncer, ProgressFn
from .base import OAUTH_SCOPE, BaseSource, Requirement

log = logging.getLogger("safecontext.dropbox")

DROPBOX = "dropbox"
# A notes file or a small export, not a disk image. Larger text is stored as a
# name and a size so one file can't fill the local database.
MAX_TEXT_BYTES = 1_000_000
TEXT_EXTS = frozenset({".txt", ".text", ".md", ".markdown", ".csv", ".json", ".html", ".htm", ".xml", ".log"})
_MIME = {
    ".txt": "text/plain", ".text": "text/plain", ".log": "text/plain",
    ".md": "text/markdown", ".markdown": "text/markdown",
    ".csv": "text/csv", ".json": "application/json",
    ".html": "text/html", ".htm": "text/html", ".xml": "application/xml",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".pdf": "application/pdf", ".zip": "application/zip",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_HTML = frozenset({".html", ".htm"})
_SCOPE = " ".join(dropbox_auth.SCOPES)

DropboxFactory = Callable[[str], DropboxAPI]
DropboxConnect = Callable[[Optional[str]], str]


def _real_connect(email: Optional[str]) -> str:
    return dropbox_auth.connect(email)


def _real_api(account: str) -> DropboxAPI:
    from ..dropbox_api import DropboxHTTP
    return DropboxHTTP(dropbox_auth.access_token(account))


def _suffix(name: str) -> str:
    return Path(name).suffix.lower()


def _mime(name: str) -> str:
    return _MIME.get(_suffix(name), "application/octet-stream")


def _link(path: str) -> str:
    if not path.startswith("/"):
        return ""
    return "https://www.dropbox.com/home" + quote(path, safe="/")


def _as_text(blob: bytes) -> Optional[str]:
    # A NUL in the first slice means this is not a text document, whatever the extension says.
    if b"\x00" in blob[:8192]:
        return None
    return blob.decode("utf-8", errors="replace")


def should_download(entry: dict) -> bool:
    """True when the file is a small text document Dropbox will actually return."""
    if entry.get(".tag") != "file":
        return False
    if entry.get("is_downloadable") is False:
        return False
    if _suffix(entry.get("name") or "") not in TEXT_EXTS:
        return False
    try:
        size = int(entry.get("size") or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= size <= MAX_TEXT_BYTES


def file_to_item(entry: dict, body: Optional[bytes], *, account: str) -> Optional[RawItem]:
    """One Dropbox file. `body` None means we keep metadata only. Folders return None."""
    if entry.get(".tag") != "file":
        return None
    file_id = entry.get("id") or ""
    if not file_id:
        return None
    name = entry.get("name") or "Untitled"
    path = entry.get("path_display") or ""
    try:
        size = int(entry.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    text, html = "", ""
    attachments = []
    if body is None:
        attachments = [Attachment(name, _mime(name), size)]
    else:
        decoded = _as_text(body)
        if decoded is None:
            attachments = [Attachment(name, _mime(name), size)]
        elif _suffix(name) in _HTML:
            html = decoded
        else:
            text = decoded
    extra = {}
    if path:
        extra["Path"] = path
    if account:
        extra["Account"] = account
    when = to_ms(entry.get("client_modified") or entry.get("server_modified")) or 0
    return RawItem(
        id=file_id,
        kind="message",
        thread_id=file_id,
        date_ms=when,
        sender="",
        title=name,
        text=text,
        html=html,
        link=_link(path),
        attachments=attachments,
        extra=extra,
    )


class DropboxSyncer(BaseSyncer):
    source = DROPBOX
    service = "Dropbox"
    reconnect_message = dropbox_auth.RECONNECT_MESSAGE

    def __init__(self, store: Store, api: DropboxAPI, *, account: str, progress: Optional[ProgressFn] = None) -> None:
        super().__init__(store, progress=progress, api=api)
        self.account = account

    def _sync(self) -> None:
        self._pass(restarted=False)

    def _pass(self, *, restarted: bool) -> None:
        try:
            self._listing()
        except CursorExpired:
            log.info("dropbox cursor expired; listing from the start")
            self.state.set("cursor", None)
            self.state.set("page_cursor", None)
            if restarted:
                raise TransientError("Dropbox cursor reset twice")
            self._pass(restarted=True)

    def _listing(self) -> None:
        if self.store.stale_ids(source=DROPBOX):
            self.state.set("cursor", None)
            self.state.set("page_cursor", None)
        page_cursor = self.state.get("page_cursor") or None
        cursor = self.state.get("cursor") or None
        full = cursor is None or bool(page_cursor)
        # Prune only a listing that started at the top. A resumed listing's cursor
        # continues mid-list, so the ids already stored are not in this pass.
        started_at_top = not page_cursor
        self.result.phase = "backfill" if full else "incremental"
        seen: set = set()
        arg = page_cursor if page_cursor else (None if full else cursor)
        while True:
            page = self.api.list_folder(arg)
            self._apply(page.get("entries") or [])
            if full:
                for entry in page.get("entries") or []:
                    if entry.get(".tag") == "file" and entry.get("id"):
                        seen.add(entry["id"])
            nxt = page.get("cursor") or ""
            if page.get("has_more"):
                if full:
                    self.state.set("page_cursor", nxt)
                else:
                    self.state.set("cursor", nxt)
                arg = nxt
                continue
            if full and started_at_top:
                self._prune(seen)
            if full:
                self.state.set("page_cursor", None)
            if nxt:
                self.state.set("cursor", nxt)
            return

    def _apply(self, entries: list) -> None:
        results = []
        deleted = []
        for entry in entries:
            tag = entry.get(".tag")
            fid = entry.get("id") or ""
            if not fid:
                continue
            if tag == "deleted":
                deleted.append(fid)
                continue
            if tag != "file":
                continue
            try:
                item = file_to_item(entry, self._body(entry), account=self.account)
                if item is None:
                    continue
                results.append(scrub_item(item))
            except Exception as e:
                results.append(self._error(fid, e))
        if deleted:
            existing = self.store.ids(DROPBOX)
            gone = [d for d in deleted if d in existing]
            if gone:
                self.store.delete(gone, DROPBOX)
                self.result.deleted += len(gone)
        if results:
            self.store.put_many(results, DROPBOX, account=self.account)
            self._count(results)
        self._report()
        log.info("dropbox page files=%s removed=%s", len(results), len(deleted))

    def _body(self, entry: dict) -> Optional[bytes]:
        if not should_download(entry):
            return None
        if int(entry.get("size") or 0) == 0:
            return b""
        try:
            blob = self.api.download(entry["id"])
        except NotDownloadable:
            return None
        if len(blob) > MAX_TEXT_BYTES or b"\x00" in blob[:8192]:
            return None
        return blob

    def _prune(self, seen: set) -> None:
        stale = [i for i in self.store.ids(DROPBOX) if i not in seen]
        if stale:
            self.store.delete(stale, DROPBOX)
            self.result.deleted += len(stale)


class DropboxSource(BaseSource):
    id = DROPBOX
    name = "Dropbox"
    description = "Files in your Dropbox, scrubbed on this Mac. Text documents are read; other files keep only a name, type, and size."
    kinds = ("message",)
    api_version = 1
    link_prefixes = ("https://www.dropbox.com/home/",)
    requirements = (Requirement(
        OAUTH_SCOPE, _SCOPE, "Read-only Dropbox access",
        "To list your files and read text documents so they can be scrubbed on this Mac. "
        "SafeContext can't upload, edit, delete, move, or share anything in Dropbox."),)
    google = False

    def __init__(self, store: Store, *, api_factory: Optional[DropboxFactory] = None,
                 connect_fn: Optional[DropboxConnect] = None,
                 revoke_fn: Optional[Callable[..., None]] = None) -> None:
        super().__init__(store)
        self.api_factory = api_factory or _real_api
        self.connect_fn = connect_fn or _real_connect
        self.revoke_fn = revoke_fn

    def connect(self, email: Optional[str] = None, **_: object) -> str:
        account = self.connect_fn(email)
        previous = self.account
        if previous and previous != account:
            if not self.revoke_fn:
                dropbox_auth.TokenStore().delete(account)
            raise auth.NotConfigured(
                f"This Mac already holds files for {previous}. Disconnect Dropbox first to switch accounts.")
        self._mark_connected(account)
        return account

    def _syncer(self, progress: Optional[ProgressFn]) -> DropboxSyncer:
        return DropboxSyncer(self.store, self.api_factory(self.account), account=self.account, progress=progress)

    def disconnect(self, wipe: bool = True, revoke: bool = True) -> None:
        if self.account:
            fn = self.revoke_fn or (lambda account, revoke=True: dropbox_auth.revoke(account, revoke=revoke))
            fn(self.account, revoke=revoke)
        super().disconnect(wipe=wipe)
