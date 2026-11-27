"""The connector interface every source implements, and the shared pieces.

A source is one place SafeContext copies data from (Gmail, Google Calendar,
iMessage, ...). Each one:

  * says who it is and what it needs (`id`, `name`, `requirements`)
  * reports `status()`: connected / needs permission / error, last sync, counts
  * `connect()`s (a Google sign-in, or checking a macOS permission)
  * `sync()`s incrementally, keeping its own cursor in source_state(<id>)
  * `disconnect()`s, which removes that source's data from this Mac

Every item goes through the scrubber before it's stored; a source never
writes to the store directly except through `Store.put_many`, which accepts
scrubbed results only.

To wrap a module in `readers/` (a plain iterator of dataclasses), subclass
`ReaderSource` below; see its docstring for exactly what to provide.

Connectors ship inside SafeContext: each one is listed in `sources.CONNECTORS`
and checked by `validate_connector` when the app starts. There is no plugin
folder; outside code never runs inside the app. CONNECTORS.md next to this
file is the guide for writing one, and tests/connector_kit.py is the test
every connector must pass.
"""
from __future__ import annotations

import logging
import re
import sys
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

from ..errors import NeedsPermission
from ..scrub import RawItem, scrub_item
from ..scrub.models import KINDS
from ..store import READ_STATE_SOURCES, Store
from ..sync import BaseSyncer, ProgressFn, SyncResult, read_heartbeat

log = logging.getLogger("safecontext.sources")

# The version of this interface. Every connector states the version it was
# written for (`api_version`); bump this, and update every connector, when a
# change here would break one.
CONNECTOR_API_VERSION = 1

# Requirement kinds
OAUTH_SCOPE = "oauth_scope"            # value: the scope URL
MACOS_PERMISSION = "macos_permission"  # value: "full_disk_access" | "calendars" | "contacts" | ...
FILE = "file"                          # value: a path the user picks or a default path
FOLDER = "folder"
REQUIREMENT_KINDS = (OAUTH_SCOPE, MACOS_PERMISSION, FILE, FOLDER)

# Source states shown on the Sources screen
NOT_CONNECTED, CONNECTED, SYNCING = "not_connected", "connected", "syncing"
NEEDS_RECONNECT, NEEDS_PERMISSION, ERROR, COMING_SOON = (
    "needs_reconnect", "needs_permission", "error", "coming_soon")


@dataclass(frozen=True)
class Requirement:
    """One thing a source needs before it can sync, with the reason shown to the user."""

    kind: str    # OAUTH_SCOPE | MACOS_PERMISSION | FILE | FOLDER
    value: str   # scope URL, permission name, or path
    label: str   # short: "Read-only access to Google Calendar"
    why: str     # one sentence: what SafeContext does with it


FULL_DISK_ACCESS = "full_disk_access"


@dataclass(frozen=True)
class PermissionHelp:
    """What the Sources screen shows when a macOS permission is missing: a
    plain-language reason and the steps, plus a button that opens the right
    System Settings pane (the source's `open_settings`). Nothing is requested
    silently: macOS has no prompt for Full Disk Access, the user turns it on."""

    requirement: str            # "full_disk_access"
    title: str                  # "Full Disk Access"
    explain: str                # why, in plain language
    steps: Tuple[str, ...]      # what to do, in order
    button: str = "Open System Settings"

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "steps": list(self.steps)}


def responsible_app_name() -> str:
    """macOS grants Full Disk Access to the app that started the process:
    SafeContext.app when packaged, otherwise the terminal (or the app) it was run from."""
    from ..env import is_app_launcher
    return "SafeContext" if is_app_launcher(sys.executable) else "the app you run SafeContext from (e.g. Terminal)"


def full_disk_access_help(what: str) -> PermissionHelp:
    """Full Disk Access help for a reader of `what` ("your Messages history", ...)."""
    app = responsible_app_name()
    return PermissionHelp(
        requirement=FULL_DISK_ACCESS, title="Full Disk Access",
        explain=(f"macOS keeps {what} in a protected folder. To read it, SafeContext needs Full Disk Access. "
                 "SafeContext only reads that folder and scrubs what it copies, on this Mac. It never sends, "
                 "changes or deletes anything, and you can turn the access off again at any time."),
        steps=("Click Open System Settings. It opens Privacy & Security → Full Disk Access.",
               f"Turn on {app}. If it isn't in the list, add it with the + button.",
               f"Quit and reopen {app.split(' (')[0]}, then click Check again."))


@dataclass
class SourceStatus:
    id: str
    name: str
    state: str                       # one of the states above
    message: str = ""                # what's wrong / what to do, when state isn't connected
    account: str = ""                # e.g. the Google account, or "This Mac"
    last_sync_ms: Optional[int] = None
    item_count: int = 0              # kept (scrubbed) items stored on this Mac
    dropped_count: int = 0           # items skipped entirely by the scrubber
    unread_count: int = 0            # items the source shows as unread (sources with a read state only)
    first_sync_done: bool = False
    syncing: bool = False
    progress: Optional[Dict[str, Any]] = None  # heartbeat of a running sync

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Source(Protocol):
    """What the app, the CLI and the background sync need from any source."""

    id: str                              # stable key: items are stored under it ("gmail", "gcal", ...)
    name: str                            # "Google Calendar"
    description: str                     # one line for the Sources card
    kinds: Tuple[str, ...]               # item kinds it produces ("message", "event", "chat", "transcript")
    link_prefixes: Tuple[str, ...]       # deep links the app may open for its items

    @property
    def requirements(self) -> Sequence[Requirement]: ...

    def status(self) -> SourceStatus: ...

    def connect(self, **options: Any) -> str:
        """Get access (sign-in, permission check). Returns the account label. Raises
        auth.NeedsReconnect / NeedsPermission / auth.NotConfigured with a message."""
        ...

    def sync(self, progress: Optional[ProgressFn] = None) -> SyncResult:
        """Fetch what changed since the stored cursor, scrub it, store it. Never raises."""
        ...

    def disconnect(self, wipe: bool = True, revoke: bool = True) -> None:
        """Forget access and (with wipe) delete this source's data from this Mac."""
        ...


class ConnectorError(ValueError):
    """A connector's declaration breaks the interface. Raised when the registry loads it,
    so a broken connector stops the app's tests, not a user's sync."""


class UndeclaredKind(ValueError):
    """A connector produced an item of a kind it didn't declare. The item is stored as
    dropped (reason "error:UndeclaredKind"), never as content."""


_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


def validate_connector(src: "BaseSource") -> None:
    """Check one connector's declaration. Raises ConnectorError naming every problem."""
    problems: List[str] = []
    who = getattr(src, "id", "") or type(src).__name__
    if not isinstance(src.id, str) or not _ID.match(src.id):
        problems.append("id must be 2-32 lowercase letters, digits or _, starting with a letter")
    if not src.name:
        problems.append("name is empty")
    if not src.description:
        problems.append("description is empty")
    if src.api_version != CONNECTOR_API_VERSION:
        problems.append(f"api_version is {src.api_version!r}; this SafeContext implements {CONNECTOR_API_VERSION}")
    if not src.kinds:
        problems.append("kinds is empty")
    unknown = [k for k in src.kinds if k not in KINDS]
    if unknown:
        problems.append(f"unknown kinds {unknown}: use {list(KINDS)} so the right scrub rules apply")
    for p in src.link_prefixes:
        if not (isinstance(p, str) and p.startswith("https://") and len(p) > len("https://x.y/")):
            problems.append(f"link prefix {p!r} must be a specific https:// URL prefix")
    for r in src.requirements:
        if not isinstance(r, Requirement):
            problems.append(f"requirement {r!r} isn't a Requirement")
        elif r.kind not in REQUIREMENT_KINDS:
            problems.append(f"requirement kind {r.kind!r} isn't one of {list(REQUIREMENT_KINDS)}")
        elif not (r.label and r.why):
            problems.append(f"requirement {r.kind}:{r.value} needs a label and a why for the user")
    if isinstance(src, ReaderSource):
        # A reader connector supplies records; batching, scrubbing and storing stay in ReaderSyncer.
        for attr, base in (("sync", BaseSource), ("_syncer", ReaderSource)):
            if getattr(type(src), attr) is not getattr(base, attr):
                problems.append(f"a ReaderSource must not override {attr}()")
    if problems:
        raise ConnectorError(f"Connector {who}: " + "; ".join(problems) + ".")


class BaseSource:
    """State bookkeeping shared by every source. Subclasses set the class
    attributes and implement `connect` and `_syncer`."""

    id = ""
    name = ""
    description = ""
    kinds: Tuple[str, ...] = ()
    link_prefixes: Tuple[str, ...] = ()
    requirements: Sequence[Requirement] = ()
    api_version = 0  # each connector sets it to the CONNECTOR_API_VERSION it was written for
    google = False  # shares the Google OAuth grant with other Google sources

    def __init__(self, store: Store) -> None:
        self.store = store
        self.state = store.state(self.id)

    # --- state -------------------------------------------------------------
    @property
    def account(self) -> str:
        return self.state.get("account") or ""

    @property
    def connected(self) -> bool:
        return bool(self.account)

    def first_sync_done(self) -> bool:
        return self.state.get("last_sync_ms") is not None

    def status(self) -> SourceStatus:
        st = self.state
        kept, dropped = self.store.counts(self.id)
        unread = self.store.unread_count(self.id) if self.id in READ_STATE_SOURCES else 0
        last = st.get("last_sync_ms")
        hb = read_heartbeat(self.store, self.id)
        out = SourceStatus(id=self.id, name=self.name, state=NOT_CONNECTED, account=self.account,
                           last_sync_ms=int(last) if last else None, item_count=kept, dropped_count=dropped,
                           unread_count=unread,
                           first_sync_done=self.first_sync_done(), syncing=hb is not None, progress=hb)
        auth_state = st.get("auth_state")
        if not self.connected:
            # A first connect that stopped at a missing macOS permission shows as such until resolved.
            if auth_state == NEEDS_PERMISSION:
                out.state, out.message = NEEDS_PERMISSION, st.get("last_error") or f"{self.name} needs permission."
            else:
                out.state = NOT_CONNECTED
        elif hb is not None:
            out.state = SYNCING
        elif auth_state == NEEDS_RECONNECT:
            out.state, out.message = NEEDS_RECONNECT, st.get("last_error") or f"{self.name} needs reconnecting."
        elif auth_state == NEEDS_PERMISSION:
            out.state, out.message = NEEDS_PERMISSION, st.get("last_error") or f"{self.name} needs permission."
        elif st.get("last_error"):
            out.state, out.message = ERROR, st.get("last_error") or ""
        else:
            out.state = CONNECTED
        return out

    def _mark_connected(self, account: str) -> None:
        self.state.set("account", account)
        self.state.set("auth_state", "ok")
        self.state.set("last_error", None)
        self.state.set("permission_needed", None)

    def _mark_needs_permission(self, e: NeedsPermission) -> None:
        self.state.set("auth_state", NEEDS_PERMISSION)
        self.state.set("last_error", e.message)
        self.state.set("permission_needed", e.requirement or None)

    # --- macOS permissions -------------------------------------------------
    # requirement value -> PermissionHelp, for the permissions this source can ask the user for
    permission_help: Dict[str, PermissionHelp] = {}

    def missing_permission(self) -> Optional[PermissionHelp]:
        """Help for the permission the last connect or sync found missing, if any."""
        if self.state.get("auth_state") != NEEDS_PERMISSION:
            return None
        req = self.state.get("permission_needed") or ""
        return self.permission_help.get(req) or next(iter(self.permission_help.values()), None)

    def open_settings(self, requirement: str) -> bool:
        """Open the System Settings pane for `requirement`. False if this source has none."""
        return False

    # --- sync --------------------------------------------------------------
    def sync(self, progress: Optional[ProgressFn] = None) -> SyncResult:
        if not self.connected:
            return SyncResult(status="not_connected", message=f"{self.name} isn't connected.")
        try:
            return self._syncer(progress).run()
        except Exception as e:  # building the client failed (auth, keychain...): record it, don't raise
            from .. import auth
            if isinstance(e, auth.NeedsReconnect):
                self.state.set("auth_state", NEEDS_RECONNECT)
                result = SyncResult(status="needs_reconnect", message=e.message)
            elif isinstance(e, NeedsPermission):
                self._mark_needs_permission(e)
                result = SyncResult(status="needs_permission", message=e.message)
            elif isinstance(e, (auth.TransientAuthError, auth.NotConfigured)):
                result = SyncResult(status="retry_later", message=str(e) or "Couldn't reach Google.")
            else:
                log.exception("%s sync crashed", self.id)
                result = SyncResult(status="retry_later",
                                    message=f"Sync stopped unexpectedly ({type(e).__name__}).")
            self.state.set("last_error", result.message)
            return result

    def _syncer(self, progress: Optional[ProgressFn]) -> BaseSyncer:
        raise NotImplementedError

    def connect(self, **options: Any) -> str:
        raise NotImplementedError

    # --- showing a skipped item (app/skipped.py) ---------------------------------
    # A source that can fetch one item again sets this and implements fetch_skipped.
    can_fetch_skipped = False

    def fetch_skipped(self, ids: Sequence[str], full: bool = True) -> Dict[str, Optional[RawItem]]:
        """Fetch items the scrubber skipped again from the source, so the app can show them to
        the user once. id -> RawItem, or None if it's gone at the source. `full` False may return
        just enough for a list row (sender, title). The result is unscrubbed: callers hold it in
        memory only and never store, log or hand it to an AI app. Raises auth.NeedsReconnect,
        TransientError or NeedsPermission like a sync would."""
        raise NotImplementedError

    def disconnect(self, wipe: bool = True, revoke: bool = True) -> None:
        if wipe:
            self.store.wipe_source(self.id)
        else:
            self.state.set("auth_state", NEEDS_RECONNECT)
            self.state.set("last_error", f"{self.name} is disconnected. Connect it again to resume syncing.")


# --- wrapping a reader --------------------------------------------------------

class ReaderSource(BaseSource):
    """A source built on a local reader: a function that yields plain
    dataclass records (readers/imessage.py, readers/whatsapp.py, ...).

    A subclass provides:

        id, name, description, kinds, requirements     class attributes
        api_version = 1                                the CONNECTOR_API_VERSION it targets
        link_prefixes                                  deep links it produces, if any

        def read(self, cursor: Optional[str]) -> Iterable[Record]
            Records strictly after `cursor` (None = everything), oldest first.
        def to_item(self, record) -> RawItem | None
            Map one record to a RawItem (no scrubbing; that happens here).
            Return None to skip a record.
        def cursor_of(self, record) -> str
            A cursor that makes `read` resume after this record
            (e.g. a ROWID or a timestamp, as a string).

    and optionally:

        def check_access(self) -> None
            Raise errors.NeedsPermission("…", requirement) when, say, Full Disk
            Access or Contacts access is missing. Called on connect and before
            every sync.
        account_label = "This Mac"
        def unread_ids(self) -> Optional[Iterable[str]]
            For a source with a read state: the ids of every item it shows as
            unread right now (set RawItem.unread in to_item too). Read state can
            change without new records, so it's re-read after every sync and
            copied onto the stored items. Default None: no read state.

    That's all: batching, scrubbing, storing, the per-source cursor, lock,
    heartbeat, progress and error handling come from here. Records are read,
    scrubbed and dropped from memory one batch at a time; nothing raw is kept.
    """

    account_label = "This Mac"
    batch_size = 200

    def read(self, cursor: Optional[str]) -> Iterable[Any]:
        raise NotImplementedError

    def to_item(self, record: Any) -> Optional[RawItem]:
        raise NotImplementedError

    def cursor_of(self, record: Any) -> str:
        raise NotImplementedError

    def check_access(self) -> None:
        return None

    def unread_ids(self) -> Optional[Iterable[str]]:
        return None

    def connect(self, **options: Any) -> str:
        """Check access first. A missing permission is remembered (the Sources card shows it
        with its help) and raised, so nothing is marked connected until access works."""
        try:
            self.check_access()
        except NeedsPermission as e:
            self._mark_needs_permission(e)
            raise
        self._mark_connected(self.account_label)
        return self.account_label

    def _syncer(self, progress: Optional[ProgressFn]) -> BaseSyncer:
        return ReaderSyncer(self, progress=progress)


class ReaderSyncer(BaseSyncer):
    def __init__(self, src: ReaderSource, *, progress: Optional[ProgressFn] = None) -> None:
        self.source = src.id
        self.service = src.name
        self.src = src
        super().__init__(src.store, progress=progress)

    def _sync(self) -> None:
        self.src.check_access()
        cursor = self.state.get("cursor")
        self.result.phase = "backfill" if cursor is None else "incremental"
        batch: List[Any] = []
        for record in self.src.read(cursor):
            batch.append(record)
            if len(batch) >= self.src.batch_size:
                cursor = self._store_batch(batch, cursor)
                batch = []
        if batch:
            self._store_batch(batch, cursor)
        unread = self.src.unread_ids()
        if unread is not None:  # mirror the source's read state, which changes without new records
            self.store.replace_unread(self.source, unread)

    def _store_batch(self, records: List[Any], cursor: Optional[str]) -> Optional[str]:
        results = []
        for rec in records:
            try:
                item = self.src.to_item(rec)
                if item is not None:
                    if item.kind not in self.src.kinds:
                        raise UndeclaredKind(item.kind)
                    results.append(scrub_item(item))
            except Exception as e:  # one odd record never stops the sync, and is never stored raw
                rid = str(getattr(rec, "id", "") or getattr(rec, "rowid", "") or self.src.cursor_of(rec))
                results.append(self._error(rid, e))
        self.store.put_many(results, self.source)
        cursor = self.src.cursor_of(records[-1])
        self.state.set("cursor", cursor)  # only after the batch is stored
        self._count(results)
        self._report()
        return cursor

