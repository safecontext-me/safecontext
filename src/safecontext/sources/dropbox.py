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
redacted. File names and folders get one more rule here (sensitive_file):
a file in a "Taxes" or "Medical" folder, or named like a bank statement, a
passport scan or a private key, is dropped before its bytes are downloaded,
and a text document that reads like a statement or a medical record is
dropped after.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence
from urllib.parse import quote

from .. import auth, dropbox_auth
from ..dropbox_api import CursorExpired, DropboxAPI, NotDownloadable, NotFound
from ..errors import TransientError
from ..scrub import Attachment, RawItem, scrub_item
from ..scrub.drop import check_sensitive_text, check_subject
from ..scrub.models import Dropped
from ..store import Store, to_ms
from ..sync import BaseSyncer, ProgressFn
from .base import NOT_CONNECTED, OAUTH_SCOPE, UNAVAILABLE, BaseSource, Requirement, SourceStatus

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


# The scrubber's subject rule is written for email subjects. A file name adds
# underscores and dashes ("bank_statement_03.csv", which \b doesn't split) and
# kinds of documents an email subject rarely names (a passport scan, a private
# key). Matched against the name and every folder on the path, with _ - . as
# spaces. Conservative on purpose: a wrongly dropped file costs little, a kept
# bank statement is what SafeContext exists to prevent.
_SENSITIVE_NAME = re.compile(
    r"\b(?:statements?|tax(?:es)?|1099s?|w ?2s?|w ?9|1040s?|1098|k 1|irs|payroll|pay ?stubs?|payslips?|"
    r"salary|bank(?:ing)?|brokerage|mortgage|loan|credit report|invoices? paid|"
    r"medical|health|healthcare|lab results?|diagnos[ie]s|prescriptions?|therapy|psychiatr\w*|"
    r"insurance|eob|patient|vaccination|immuni[sz]ation|"
    r"passports?|driver'?s? licen[cs]e|licen[cs]e scan|id card|identity card|social security|ssn|"
    r"birth certificate|visa|green card|residence permit|"
    r"passwords?|credentials?|recovery (?:codes?|key)|backup codes?|seed phrase|private key|"
    r"id (?:rsa|ed25519|ecdsa|dsa)|keychain|1password|bitwarden|lastpass)\b",
    re.I,
)
# Files that are secrets by type, whatever they are called. Not ".key": that is also Keynote.
_SECRET_EXTS = frozenset({".pem", ".p12", ".pfx", ".kdbx", ".keychain", ".asc", ".gpg", ".ovpn"})
_SECRET_NAMES = frozenset({".env", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
                           "credentials", "credentials.json"})
# A text document that reads like a statement, a tax form or a medical record.
_SENSITIVE_DOC = re.compile(
    r"\b(?:statement period|(?:opening|closing|beginning|ending|available) balance|account number|"
    r"routing number|iban|sort code|taxpayer identification|employer identification|"
    r"adjusted gross income|form (?:1040|1099|w-?2)|social security number|"
    r"date of birth|DOB|patient (?:name|id)|medical record number|MRN|diagnosis|"
    r"chief complaint|policy (?:number|holder)|member id|group number)\b|"
    r"^\s*(?:medications?|allergies|patient|findings)\s*:",
    re.I | re.M,
)
_DOC_SCAN_CHARS = 20_000


def _spaced(part: str) -> str:
    return re.sub(r"[_\-.+]+", " ", part)


def sensitive_name(path: str, name: str) -> Optional[str]:
    """Drop reason for a file whose name or folders say it is sensitive, else None."""
    if name.lower() in _SECRET_NAMES or _suffix(name) in _SECRET_EXTS:
        return "sensitive:subject"
    parts = [p for p in (path or "").split("/") if p] or [name]
    for part in parts:
        spaced = _spaced(part)
        if _SENSITIVE_NAME.search(spaced) or check_subject(spaced):
            return "sensitive:subject"
    return None


def sensitive_text(text: str) -> Optional[str]:
    """Drop reason for a text document that reads like a statement or a medical record."""
    head = text[:_DOC_SCAN_CHARS]
    if _SENSITIVE_DOC.search(head):
        return "sensitive:text"
    return check_sensitive_text(head)


def _real_connect(email: Optional[str]) -> str:
    return dropbox_auth.connect(email)


def _real_api(account: str) -> DropboxAPI:
    from ..dropbox_api import DropboxHTTP
    # A provider, not a token: a long first sync outlives the 4-hour access token.
    # access_token() refreshes within 60 s of expiry, so re-reading the Keychain every
    # 50 s keeps the token valid without a Keychain read per request.
    cache = {"token": "", "at": 0.0}

    def token() -> str:
        now = time.monotonic()
        if not cache["token"] or now - cache["at"] > 50:
            cache["token"], cache["at"] = dropbox_auth.access_token(account), now
        return cache["token"]
    return DropboxHTTP(token)


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
                when = to_ms(entry.get("client_modified") or entry.get("server_modified")) or 0
                reason = sensitive_name(entry.get("path_display") or "", entry.get("name") or "")
                if reason:  # dropped before its bytes are downloaded
                    results.append(Dropped(fid, fid, reason, when))
                    continue
                body = self._body(entry)
                item = file_to_item(entry, body, account=self.account)
                if item is None:
                    continue
                reason = sensitive_text(item.text or item.html) if (item.text or item.html) else None
                results.append(Dropped(fid, fid, reason, when) if reason else scrub_item(item, strip_quotes=False))
            except (TransientError, auth.NeedsReconnect):
                # Network trouble, rate limiting or lost access is not this file's fault. Stop here
                # without storing the page; the next sync resumes at this page (page_cursor).
                raise
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
                 revoke_fn: Optional[Callable[..., None]] = None,
                 configured_fn: Optional[Callable[[], bool]] = None) -> None:
        super().__init__(store)
        self.api_factory = api_factory or _real_api
        self.connect_fn = connect_fn or _real_connect
        self.revoke_fn = revoke_fn
        # A build without a Dropbox app key shows the card as unavailable instead of failing on Connect.
        # A stand-in connect_fn (tests, demo) needs no key.
        self.configured_fn = configured_fn or (dropbox_auth.configured if connect_fn is None else (lambda: True))

    def status(self) -> SourceStatus:
        out = super().status()
        if out.state == NOT_CONNECTED and not self.configured_fn():
            out.state, out.message = UNAVAILABLE, dropbox_auth.NOT_IN_BUILD
        return out

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

    can_fetch_skipped = True

    def fetch_skipped(self, ids: Sequence[str], full: bool = True) -> Dict[str, Optional[RawItem]]:
        """Read-only get_metadata, then (full) the bytes of a small text file. Other files come
        back as a name, type and size, the way sync sees them."""
        api = self.api_factory(self.account)
        out: Dict[str, Optional[RawItem]] = {}
        for fid in ids:
            try:
                entry = api.get_metadata(fid)
            except NotFound:
                out[fid] = None
                continue
            body = None
            if full and should_download(entry):
                try:
                    blob = b"" if int(entry.get("size") or 0) == 0 else api.download(fid)
                    body = None if len(blob) > MAX_TEXT_BYTES else blob
                except NotDownloadable:
                    body = None
            out[fid] = file_to_item(entry, body, account=self.account)
        return out

    def disconnect(self, wipe: bool = True, revoke: bool = True) -> None:
        if self.account:
            fn = self.revoke_fn or (lambda account, revoke=True: dropbox_auth.revoke(account, revoke=revoke))
            fn(self.account, revoke=revoke)
        super().disconnect(wipe=wipe)
