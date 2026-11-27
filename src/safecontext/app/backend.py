"""The object the app window calls into (exposed to JavaScript by pywebview).

There is no HTTP server: the window talks to Python over pywebview's
in-process bridge, so no other program on the Mac can query your mail.
The MCP link (grok_link.py) only ever makes an outbound connection.

Every public method returns a JSON-friendly dict and never raises; errors
come back as {"ok": False, "error": "..."} for the UI to show.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import sys
import threading
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from .. import __version__, auth, env, migrate, permlog, review, updater as upd
from ..scrub.controls import PatternError, add_pattern, builtin_rules, delete_pattern, load_patterns, preview, set_enabled
from ..errors import NeedsPermission
from ..grok_link import CONNECTOR_PAGES, GrokLink
from ..pdf_selection import PdfSelection, PdfSelectionError
from ..sources import Sources, default_sources
from ..store import DEFAULT_DATA_DIR, GMAIL, TAG_COLORS, Store, TagError
from ..sync import SyncResult, read_heartbeat
from ..tools import run_tool
from .mail import MailView
from .skipped import SkippedView

log = logging.getLogger("safecontext.app")

# Automatic syncing: every source at launch, then every AUTO_SYNC_SECONDS. Bringing the window
# forward or waking the Mac checks again, but never sooner than MIN_SYNC_GAP_SECONDS after the
# source last started or finished a sync. The loop wakes every TICK_SECONDS; a wall-clock jump
# much bigger than that means the Mac slept.
AUTO_SYNC_SECONDS = 5 * 60
MIN_SYNC_GAP_SECONDS = 2 * 60
TICK_SECONDS = 30

REASON_LABELS = {
    "spam_trash": "Spam & Trash", "draft": "Drafts", "chat": "Chats",
    "category:promotions": "Promotions", "category:social": "Social", "category:forums": "Forums",
    "bulk:list_unsubscribe": "Newsletters & mailing lists", "bulk:precedence": "Bulk mail",
    "bulk:bounce": "Bounce notices", "bulk:no_reply_sender": "No-reply senders (being re-synced)",
    "security:subject": "Security & 2FA codes", "security:body": "Security & 2FA codes",
    "sensitive:sender_domain": "Banks, tax & health senders", "sensitive:sender_keyword": "Banks, tax & health senders",
    "sensitive:subject": "Statements, tax & medical subjects or titles",
    "sensitive:dating": "Dating apps",
    "business_sender": "Texts from businesses & shortcodes",
    "sensitive:text": "Bank, tax & health texts",
}

REDACTION_LABELS = {
    "ONE_TIME_CODE": "2FA / one-time codes", "TOTP_SECRET": "Authenticator keys", "BACKUP_CODE": "Backup codes",
    "CREDIT_CARD": "Card numbers", "CVV": "Card security codes", "SSN": "Social Security numbers",
    "API_KEY": "API keys", "JWT": "Login tokens", "PRIVATE_KEY": "Private keys", "PASSWORD": "Passwords",
    "IBAN": "IBANs", "ROUTING_NUMBER": "Routing numbers", "BANK_ACCOUNT": "Bank account numbers",
    "DATE_OF_BIRTH": "Dates of birth", "PHONE": "Phone numbers", "STREET_ADDRESS": "Street addresses",
    "BOOKING_CODE": "Booking & confirmation codes",
    "HEALTH": "Health details", "LEGAL": "Legal matters", "PERSONAL": "Personal matters",
}


_in_app_bundle = env.is_app_launcher


def _safe(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            out = fn(*args, **kwargs)
            return {"ok": True, **(out or {})}
        except auth.NeedsReconnect as e:
            return {"ok": False, "error": e.message, "needs_reconnect": True}
        except NeedsPermission as e:  # a macOS permission is missing; the Sources card explains it
            return {"ok": False, "error": e.message, "needs_permission": True, "requirement": e.requirement}
        except auth.NotConfigured as e:
            return {"ok": False, "error": str(e)}
        except (PdfSelectionError, TagError) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # never leak a traceback (or mail content) into the UI
            log.exception("app call failed: %s", fn.__name__)
            return {"ok": False, "error": f"Something went wrong ({type(e).__name__})."}
    return wrapper


_NOOP_REVOKE = lambda account, revoke=True: None  # noqa: E731  (demo mode: no Google to revoke at)


META_SCAN_FACTS = "review_scan_facts"  # what the last opt-in scan saw (names and rules only)

class Backend:
    """Public methods are callable from the window. Everything else is underscore-prefixed
    because pywebview exposes public attributes, including methods of nested objects."""

    def __init__(self, data_dir: Optional[Path] = None, *, demo: bool = False,
                 gmail_factory: Optional[Callable[[str, str], Any]] = None,
                 connect_fn: Optional[Callable[[str, Optional[str]], str]] = None,
                 calendar_factory: Optional[Callable[[str], Any]] = None,
                 calendar_connect_fn: Optional[Callable[[Optional[str]], str]] = None,
                 grok_link: Optional[Callable[[Store, Path], GrokLink]] = None,
                 imessage_options: Optional[Dict[str, Any]] = None,
                 source_options: Optional[Dict[str, Dict[str, Any]]] = None,
                 updater: Optional[upd.Updater] = None,
                 review_home: Optional[Path] = None,
                 auto_sync: bool = True,
                 pdf_chooser: Optional[Callable[[str], Any]] = None,
                 pdf_start: Optional[Sequence[Tuple[str, str]]] = None) -> None:
        self._demo = demo
        self._data_dir = Path(data_dir or DEFAULT_DATA_DIR)
        # Where the opt-in review scan looks for other AI tools' config. Demo mode never looks
        # in the real home folder: it gets a fixture (demo.py) or an empty folder.
        self._review_home = Path(review_home) if review_home else (
            self._data_dir / "demo-home" if demo else Path.home())
        # Where the review looks for installed AI apps (existence of the .app only).
        self._app_roots = [self._review_home / "Applications"] if (review_home or demo) else [
            Path("/Applications"), Path.home() / "Applications"]
        self._store = Store(self._data_dir / "mail.db")
        # Folders and PDFs the user opts in. Text may be read only from those PDFs.
        self._pdfs = PdfSelection(self._store, chooser=pdf_chooser)
        if pdf_start and not self._pdfs.entries():  # demo: made-up folders and PDFs, opted in at first start
            for kind, path in pdf_start:
                (self._pdfs.add_folder if kind == "folder" else self._pdfs.add_file)(path)
        self._connect_fn = connect_fn  # None: the real Google sign-in
        self._sources: Sources = default_sources(
            self._store, gmail_factory=gmail_factory,
            gmail_connect=lambda mode, email: (self._connect_fn or _real_gmail_connect)(mode, email),
            calendar_factory=calendar_factory, calendar_connect=calendar_connect_fn,
            revoke_fn=_NOOP_REVOKE if demo else None, imessage_options=imessage_options,
            options=source_options)
        if not demo:
            migrate.remove_ask_leftovers(self._store)  # the old Ask feature's xAI key, once
        self._grok = (grok_link or GrokLink)(self._store, self._data_dir)
        self._mail = MailView(self._store, self._sources, REASON_LABELS)
        self._skipped = SkippedView(self._store, self._sources, REASON_LABELS)  # held in memory, never stored
        self._threads: Dict[str, threading.Thread] = {}
        self._sync_lock = threading.Lock()
        self._progress: Dict[str, SyncResult] = {}
        self._last_result: Dict[str, SyncResult] = {}
        self._last_run: Dict[str, float] = {}  # source id -> wall time a sync last started or ended
        self._last_tick: Optional[float] = None
        self._stop = threading.Event()
        # Demo mode never touches the network, so it gets no updater unless a test passes one.
        self._updater: Optional[upd.Updater] = updater if updater is not None else (
            None if demo else upd.Updater(self._store))
        self._quit_fn: Optional[Callable[[], None]] = None  # set by main.run: closes the window
        self._auto_sync_on = auto_sync  # off: only clicks sync (tests)
        if auto_sync:
            threading.Thread(target=self._auto_sync_loop, daemon=True, name="auto-sync").start()
            self._grok.start()  # reconnect to the relay if Grok was left on
            if self._updater is not None and not demo:
                self._updater.start_background()

    # --- status ------------------------------------------------------------
    @_safe
    def status(self) -> Dict[str, Any]:
        """Gmail's status at the top level (the sidebar), every source under "sources"."""
        s = self._store
        g = s.state(GMAIL)
        p = self._progress.get(GMAIL) or SyncResult(status="idle")
        last = g.get("last_sync_ms")
        kept, dropped = s.counts(GMAIL)
        hb = read_heartbeat(s, GMAIL)
        if self._is_syncing(GMAIL) and hb is None:  # local thread starting up, before its first beat
            hb = {"phase": p.phase, "fetched": p.fetched, "kept": p.kept, "dropped": p.dropped}
        oldest, newest = s.date_range(GMAIL)
        last_result = self._last_result.get(GMAIL)
        sources = self._source_statuses()
        return {
            "demo": self._demo,
            "account": g.get("account"),
            "mode": g.get("sync_mode") or "full",
            "needs_reconnect": g.get("auth_state") == "needs_reconnect",
            "reconnect_message": auth.RECONNECT_MESSAGE,
            "first_sync_done": g.get("history_id") is not None,
            "syncing": hb is not None,
            "progress": None if hb is None else {
                "phase": hb.get("phase") or "", "fetched": hb.get("fetched", 0), "kept": hb.get("kept", 0),
                "dropped": hb.get("dropped", 0), "started_ms": hb.get("started_ms"),
                "per_min": hb.get("per_min"), "waiting_until_ms": hb.get("waiting_until_ms"),
                "elsewhere": hb.get("pid") not in (None, os.getpid()),
                "backfill": g.get("history_id") is None},
            "oldest_ms": oldest, "newest_ms": newest,
            "last_result": None if not last_result else {
                "status": last_result.status, "message": last_result.message},
            "last_sync_ms": int(last) if last else None,
            "kept": kept, "dropped": dropped,
            "sources": sources,
            "any_connected": any(x["account"] for x in sources if x["available"]),
            "total_items": s.counts()[0],
            "data_dir": str(self._data_dir),
            "environment": env.CURRENT,
        }

    def _source_statuses(self):
        out = self._sources.statuses()
        for x in out:
            if self._is_syncing(x["id"]):
                x["syncing"] = True
                if x["state"] not in ("coming_soon", "unavailable", "not_connected"):
                    x["state"] = "syncing"
            res = self._last_result.get(x["id"])
            x["last_result"] = None if not res else {"status": res.status, "message": res.message}
        return out

    def _is_syncing(self, source_id: str) -> bool:
        t = self._threads.get(source_id)
        return bool(t and t.is_alive())

    # --- sources -------------------------------------------------------------
    @_safe
    def sources(self) -> Dict[str, Any]:
        return {"sources": self._source_statuses()}

    @_safe
    def connect_source(self, source_id: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        src = self._sources.get(source_id)
        opts = {k: v for k, v in (options or {}).items() if k in ("mode", "email")}
        account = src.connect(**opts)
        self._start_sync(source_id)
        return {"account": account}

    @_safe
    def open_source_settings(self, source_id: str) -> Dict[str, Any]:
        """Open the System Settings pane for the permission this source is missing (Full Disk
        Access for iMessage). Only ever on a click: the user turns the access on themselves."""
        src = self._sources.get(source_id)
        help_ = src.missing_permission() or next(iter(src.permission_help.values()), None)
        if help_ is None:
            return {"ok": False, "error": f"{src.name} doesn't need a Mac permission."}
        if not src.open_settings(help_.requirement):
            return {"ok": False, "error": "Couldn't open System Settings. Open it yourself and go to "
                                          f"Privacy & Security → {help_.title}."}
        return {"opened": True}

    @_safe
    def sync_source(self, source_id: str) -> Dict[str, Any]:
        self._sources.get(source_id)
        return {"started": self._start_sync(source_id)}

    @_safe
    def disconnect_source(self, source_id: str, wipe: bool = True) -> Dict[str, Any]:
        self._sources.get(source_id)
        if wipe:
            t = self._threads.get(source_id)
            if t:
                t.join(timeout=30)
        self._sources.disconnect(source_id, wipe=bool(wipe))
        self._skipped.forget(source_id)
        self._last_result.pop(source_id, None)
        return {"wiped": bool(wipe)}

    # --- files: the only PDFs whose text may be read ---------------------------
    def _pdf_payload(self) -> Dict[str, Any]:
        # home: so the Files table can show paths under it as ~/…
        return {"entries": self._pdfs.entries(), "pdfs": self._pdfs.readable_pdfs(), "table": self._pdfs.table(),
                "home": str(Path.home())}

    @_safe
    def pdf_selection(self) -> Dict[str, Any]:
        """Folders and files the user added, and the PDFs whose text may be read."""
        return self._pdf_payload()

    @_safe
    def pdf_add_folder(self, path: str) -> Dict[str, Any]:
        return {"entry": self._pdfs.add_folder(path), **self._pdf_payload()}

    @_safe
    def pdf_add_file(self, path: str) -> Dict[str, Any]:
        return {"entry": self._pdfs.add_file(path), **self._pdf_payload()}

    @_safe
    def pdf_remove(self, path: str) -> Dict[str, Any]:
        self._pdfs.remove(path)
        return self._pdf_payload()

    @_safe
    def pdf_pick(self, kind: str = "folder") -> Dict[str, Any]:
        """Open the folder or PDF panel and opt in what the user picks."""
        entries = self._pdfs.pick(kind)
        if not entries:
            return {"cancelled": True}
        return {"entries": entries, **self._pdf_payload()}

    # --- Gmail (the v1 calls the onboarding screen and Settings use) -----------
    @_safe
    def connect(self, mode: str = "full", email: Optional[str] = None) -> Dict[str, Any]:
        if mode not in auth.MODES:
            return {"ok": False, "error": "Unknown mode."}
        account = self._sources.get(GMAIL).connect(mode=mode, email=email)
        self._start_sync(GMAIL)
        return {"account": account}

    @_safe
    def sync_now(self) -> Dict[str, Any]:
        """Sync every connected source, each on its own."""
        started = [self._start_sync(s.id) for s in self._sources.connected()]
        return {"started": any(started)}

    @_safe
    def window_focused(self) -> Dict[str, Any]:
        """The window came forward: check any source not checked in the last couple of minutes."""
        if self._updater is not None:
            self._updater.on_focus()  # and for a new version, if the last check is 15+ min old
        return {"started": self._auto_sync(MIN_SYNC_GAP_SECONDS) if self._auto_sync_on else []}

    def _start_sync(self, source_id: str) -> bool:
        src = self._sources.get(source_id)
        with self._sync_lock:
            if (self._is_syncing(source_id) or read_heartbeat(self._store, source_id) is not None
                    or not src.connected):
                return False
            t = threading.Thread(target=self._run_sync, args=(source_id,), daemon=True, name=f"sync-{source_id}")
            self._threads[source_id] = t
            self._last_run[source_id] = time.time()
            t.start()
            return True

    def _run_sync(self, source_id: str) -> None:
        self._progress[source_id] = SyncResult(status="running", phase="starting")
        try:
            result = self._sources.get(source_id).sync(progress=lambda r: self._on_progress(source_id, r))
        except Exception as e:  # Source.sync shouldn't raise; if it does, it stays this source's problem
            log.exception("%s sync crashed", source_id)
            result = SyncResult(status="retry_later", message=f"Sync stopped unexpectedly ({type(e).__name__}).")
        if result.status != "busy":
            self._last_result[source_id] = result
        self._progress[source_id] = result
        self._last_run[source_id] = time.time()  # a long first sync that stops waits a full gap, not none

    def _on_progress(self, source_id: str, r: SyncResult) -> None:
        self._progress[source_id] = SyncResult(status="running", phase=r.phase, fetched=r.fetched, kept=r.kept,
                                               dropped=r.dropped, deleted=r.deleted)

    def _auto_sync_loop(self) -> None:
        self._auto_tick()  # at launch: picks up new mail and resumes a paused first sync
        while not self._stop.wait(TICK_SECONDS):
            self._auto_tick()

    def _auto_tick(self, now: Optional[float] = None) -> list:
        now = time.time() if now is None else now
        woke = self._last_tick is not None and now - self._last_tick > TICK_SECONDS + 60
        self._last_tick = now
        return self._auto_sync(MIN_SYNC_GAP_SECONDS if woke else AUTO_SYNC_SECONDS, now)

    def _auto_sync(self, min_gap: float, now: Optional[float] = None) -> list:
        """Start a sync for each connected source that last ran at least `min_gap` seconds ago.
        Sources that need reconnecting or a Mac permission wait for the user."""
        now = time.time() if now is None else now
        started = []
        for src in self._sources.connected():  # each source on its own thread; one failing never blocks another
            if src.state.get("auth_state") in ("needs_reconnect", "needs_permission"):
                continue
            last = self._last_run.get(src.id)
            if last is not None and now - last < min_gap:
                continue
            try:
                if self._start_sync(src.id):
                    started.append(src.id)
            except Exception:
                log.exception("couldn't start %s sync", src.id)
        return started

    @_safe
    def disconnect(self, wipe: bool = False) -> Dict[str, Any]:
        """Gmail. wipe=False keeps the local mail (the source shows "needs reconnect")."""
        return self.disconnect_source.__wrapped__(self, GMAIL, wipe)

    # --- browsing mail ---------------------------------------------------------
    @_safe
    def search(self, query: str, sender: str = "", after: str = "", before: str = "",
               source: str = "") -> Dict[str, Any]:
        args = {"query": query, "limit": 50}
        args.update({k: v for k, v in (("sender", sender), ("after", after), ("before", before),
                                       ("source", source)) if v})
        return self._tool("search", args)

    @_safe
    def recent(self, sender: str = "", after: str = "", before: str = "", source: str = "") -> Dict[str, Any]:
        args: Dict[str, Any] = {"limit": 50}
        args.update({k: v for k, v in (("sender", sender), ("after", after), ("before", before),
                                       ("source", source)) if v})
        return self._tool("list_items", args)

    @_safe
    def read_thread(self, thread_id: str, source: str = "") -> Dict[str, Any]:
        args = {"thread_id": thread_id}
        if source:
            args["source"] = source
        return self._tool("read", args)

    def _tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        out = run_tool(self._store, name, args)
        if "error" in out:
            raise auth.NotConfigured(out["error"])
        return out

    @_safe
    def open_link(self, url: str) -> Dict[str, Any]:
        # Only ever open the kinds of deep link our sources generate (Gmail, Google Calendar...),
        # never a URL taken from the content of a message or event.
        if not self._sources.link_allowed(url):
            return {"ok": False, "error": "Only links back to the original (Gmail, Google Calendar, Dropbox) can be opened."}
        webbrowser.open(url)
        return {}

    @_safe
    def open_in_gmail(self, url: str) -> Dict[str, Any]:
        if not isinstance(url, str) or not url.startswith("https://mail.google.com/mail/"):
            return {"ok": False, "error": "Only Gmail links can be opened."}
        return self.open_link.__wrapped__(self, url)

    # --- Mail view (browse synced mail, see what isn't synced) ---------------------
    @_safe
    def mail_folders(self) -> Dict[str, Any]:
        return self._mail.folders()

    @_safe
    def mail_list(self, view: str = "all", source: str = "", query: str = "", reasons: Optional[list] = None,
                  cursor: Optional[str] = None, limit: int = 100, order: str = "newest",
                  labels: Optional[list] = None) -> Dict[str, Any]:
        return self._mail.list(view, source, query, reasons or None, cursor, limit, order, labels or None)

    # --- labels (mirrored from Gmail) and tags (the user's own, on this Mac only) -----
    @_safe
    def mail_labels(self, source: str = "") -> Dict[str, Any]:
        return {"labels": self._mail.labels(source), "colors": list(TAG_COLORS)}

    @_safe
    def tag_create(self, name: str, color: str = "") -> Dict[str, Any]:
        return {"tag": self._store.create_tag(str(name or ""), str(color or ""))}

    @_safe
    def tag_update(self, tag_id: int, name: Optional[str] = None, color: Optional[str] = None) -> Dict[str, Any]:
        self._store.update_tag(int(tag_id), name, color)
        return {}

    @_safe
    def tag_delete(self, tag_id: int) -> Dict[str, Any]:
        self._store.delete_tag(int(tag_id))
        return {}

    @_safe
    def tag_thread(self, tag_id: int, source: str, thread_id: str, on: bool = True) -> Dict[str, Any]:
        self._sources.get(source)  # unknown sources raise
        self._store.tag_thread(int(tag_id), source, str(thread_id), bool(on))
        return {"labels": self._store.thread_labels(source, str(thread_id))}

    @_safe
    def mail_thread(self, source: str, thread_id: str) -> Dict[str, Any]:
        return self._mail.thread(source, thread_id)

    # --- skipped items, fetched live to show once (app/skipped.py): never stored, never sent to AI ---
    @_safe
    def skipped_item(self, source: str, item_id: str) -> Dict[str, Any]:
        """One skipped item's content, fetched from its source now. Any screen can call it with the
        source and id of a Not synced row (the Mail reader, the Security Dashboard's feed)."""
        return self._skipped.item(str(source), str(item_id))

    @_safe
    def skipped_previews(self, rows: list) -> Dict[str, Any]:
        """Sender, title and snippet for skipped list rows [{"source", "id"}], fetched now."""
        return self._skipped.previews([r for r in (rows or []) if isinstance(r, dict)])

    @_safe
    def skipped_forget(self) -> Dict[str, Any]:
        """The user left Not synced: drop every skipped item held in memory."""
        self._skipped.forget()
        return {}

    @_safe
    def mail_reasons(self, source: str = "") -> Dict[str, Any]:
        return {"reasons": self._mail.reasons(source)}

    @_safe
    def mail_coverage(self, source: str = "") -> Dict[str, Any]:
        live = {}
        for sid in list(self._threads):  # a local sync starting up, before its first heartbeat
            p = self._progress.get(sid)
            if self._is_syncing(sid) and p:
                live[sid] = {"phase": p.phase, "fetched": p.fetched, "kept": p.kept, "dropped": p.dropped}
        return {"sources": self._mail.coverage(source, live)}

    # --- privacy report --------------------------------------------------------
    @_safe
    def privacy_report(self) -> Dict[str, Any]:
        st = self._store.stats()
        dropped: Dict[str, int] = {}
        for reason, n in st["dropped_by_reason"].items():
            label = REASON_LABELS.get(reason, "Couldn't be processed" if reason.startswith("error:") else reason)
            dropped[label] = dropped.get(label, 0) + n
        redacted = {REDACTION_LABELS.get(k, k): v for k, v in
                    sorted(st["redactions"].items(), key=lambda kv: -kv[1])}
        by_source = []
        for x in self._sources:
            kept, skipped = self._store.counts(x.id)
            if kept or skipped:
                by_source.append({"id": x.id, "name": x.name, "kept": kept, "dropped": skipped})
        return {"kept": st["kept"], "dropped_total": st["dropped"],
                "dropped": dict(sorted(dropped.items(), key=lambda kv: -kv[1])), "redacted": redacted,
                "by_source": by_source}

    # --- scrubber controls (file and PDF text extracted on this Mac) -------------
    def _scrubber_view(self) -> Dict[str, Any]:
        return {"builtin": builtin_rules(),
                "patterns": [row.as_dict() for row in load_patterns(self._store)]}

    @_safe
    def scrubber_rules(self) -> Dict[str, Any]:
        """Built-in rules, which always run, and the patterns the user added for files."""
        return self._scrubber_view()

    @_safe
    def scrubber_add(self, name: str, pattern: str) -> Dict[str, Any]:
        try:
            add_pattern(self._store, name or "", pattern or "")
        except PatternError as e:
            return {"ok": False, "error": e.message}
        return self._scrubber_view()

    @_safe
    def scrubber_set_enabled(self, pattern_id: str, enabled: bool) -> Dict[str, Any]:
        try:
            set_enabled(self._store, pattern_id or "", bool(enabled))
        except PatternError as e:
            return {"ok": False, "error": e.message}
        return self._scrubber_view()

    @_safe
    def scrubber_delete(self, pattern_id: str) -> Dict[str, Any]:
        try:
            delete_pattern(self._store, pattern_id or "")
        except PatternError as e:
            return {"ok": False, "error": e.message}
        return self._scrubber_view()

    @_safe
    def scrubber_preview(self, text: str) -> Dict[str, Any]:
        """Show what a sample would look like after scrubbing. The sample is not saved."""
        try:
            cleaned, counts, truncated = preview(self._store, text if isinstance(text, str) else "")
        except PatternError as e:
            return {"ok": False, "error": e.message}
        return {"text": cleaned, "redacted": counts, "truncated": truncated}

    # --- MCP link (shown as "MCP"; the grok_* names are kept from "Use in Grok") ----
    @_safe
    def grok_status(self) -> Dict[str, Any]:
        return self._grok.status()

    @_safe
    def grok_enable(self) -> Dict[str, Any]:
        """Turn on: connect to the relay and copy the connector link."""
        url = self._grok.enable()
        return {"url": url, "copied": self._copy(url), **self._grok.status()}

    @_safe
    def grok_disable(self) -> Dict[str, Any]:
        self._grok.disable()
        return self._grok.status()

    @_safe
    def grok_regenerate(self) -> Dict[str, Any]:
        url = self._grok.regenerate()
        return {"url": url, "copied": self._copy(url), **self._grok.status()}

    @_safe
    def grok_copy_link(self) -> Dict[str, Any]:
        url = self._grok.connector_url()
        if not url or not self._grok.enabled:
            return {"ok": False, "error": "Turn on MCP first."}
        return {"url": url, "copied": self._copy(url)}

    @_safe
    def grok_approve(self, approval_id: str, ok: bool = True) -> Dict[str, Any]:
        if not self._grok.approve(str(approval_id), bool(ok)):
            return {"ok": False, "error": "That request expired. Try connecting again from your AI app."}
        return {}

    @_safe
    def grok_revoke(self, grant_id: str) -> Dict[str, Any]:
        """Sign one AI app out of the MCP link (the relay deletes that sign-in's tokens)."""
        if not self._grok.revoke(str(grant_id)):
            return {"ok": False, "error": "Couldn't revoke it. Check that MCP is connected, then try again."}
        return self._grok.status()

    @_safe
    def grok_set_keep_awake(self, on: bool) -> Dict[str, Any]:
        self._grok.set_keep_awake(bool(on))
        return self._grok.status()

    @_safe
    def grok_set_link_only(self, on: bool) -> Dict[str, Any]:
        self._grok.set_link_only(bool(on))
        return self._grok.status()

    @_safe
    def grok_clear_activity(self) -> Dict[str, Any]:
        self._grok.activity.clear()
        return {}

    @_safe
    def open_connectors(self, app: str = "grok") -> Dict[str, Any]:
        """Open an AI app's page for adding a custom connector (only the fixed pages in CONNECTOR_PAGES)."""
        url = CONNECTOR_PAGES.get(app) if isinstance(app, str) else None
        if not url:
            return {"ok": False, "error": "That app has no connectors page to open."}
        if self._demo:
            return {"opened": False}  # demo mode never touches the network
        webbrowser.open(url)
        return {"opened": True}

    def _copy(self, text: str) -> bool:
        """The system clipboard via pbcopy (WebKit's clipboard API needs a fresh click).
        Demo mode leaves the clipboard alone; the window copies it itself."""
        if self._demo or not text:
            return False
        try:
            subprocess.run(["/usr/bin/pbcopy"], input=text.encode(), check=True, timeout=5)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    # --- AI access review (review.py) --------------------------------------------------
    def _review_data(self) -> Dict[str, Any]:
        answers = review.load_answers(self._store.get_meta(review.META_ANSWERS))
        scan = review.scan_ai_configs(self._review_home) if self._store.get_meta(review.META_SCAN) == "1" else None
        if scan is not None:
            self._log_scan_changes(scan)
        status = self._grok.status()  # also logs connect requests that went unanswered
        since = int(time.time() * 1000) - review.REQUEST_WINDOW_DAYS * review.DAY_MS
        return review.build_review(status, answers, scan, events=self._grok.permlog.since(since, "connect"),
                                   apps=review.installed_apps(self._app_roots))

    def _log_scan_changes(self, scan: Dict[str, Any]) -> None:
        """Compare with what the last scan saw and log what appeared or went. The first scan only
        sets the baseline. Keeps names and rules only, like the scan itself."""
        facts = permlog.scan_facts(scan)
        raw = self._store.get_meta(META_SCAN_FACTS)
        if raw is not None:
            try:
                before = set(json.loads(raw))
            except (ValueError, TypeError):
                before = set()
            for note in permlog.scan_changes(before, facts)[:20]:
                self._grok.permlog.record("scan_change", note=note)
        if raw is None or set(json.loads(raw or "[]")) != facts:
            self._store.set_meta(META_SCAN_FACTS, json.dumps(sorted(facts)))

    @_safe
    def review(self) -> Dict[str, Any]:
        return self._review_data()

    @_safe
    def review_answer(self, check_id: str, answer: str = "") -> Dict[str, Any]:
        """Record what the user found on a guided check ("" clears it)."""
        if check_id not in review.GUIDED_BY_ID or answer not in ("", *review.ANSWERS):
            return {"ok": False, "error": "Unknown check."}
        answers = review.load_answers(self._store.get_meta(review.META_ANSWERS))
        if answer:
            answers[check_id] = {"answer": answer, "ts": int(time.time() * 1000)}
        else:
            answers.pop(check_id, None)
        self._store.set_meta(review.META_ANSWERS, json.dumps(answers))
        return self._review_data()

    @_safe
    def review_set_scan(self, on: bool) -> Dict[str, Any]:
        """Opt in or out of reading Claude Desktop's and Claude Code's config files."""
        was = self._store.get_meta(review.META_SCAN) == "1"
        self._store.set_meta(review.META_SCAN, "1" if on else None)
        if bool(on) != was:
            self._grok.permlog.record("scan", "on" if on else "off")
            self._store.set_meta(META_SCAN_FACTS, None)  # the next scan sets a fresh baseline
        return self._review_data()

    @_safe
    def permission_log(self) -> Dict[str, Any]:
        """The permission log, newest first (permlog.py). Kept only on this Mac."""
        self._grok.status()  # log any request that expired while nobody looked
        return {"entries": self._grok.permlog.entries(200)}

    @_safe
    def permission_log_clear(self) -> Dict[str, Any]:
        self._grok.permlog.clear()
        return {"entries": []}

    @_safe
    def review_open(self, check_id: str) -> Dict[str, Any]:
        """Open a guided check's page (only the fixed links in review.GUIDED)."""
        url = review.guided_link(check_id) if isinstance(check_id, str) else None
        if not url:
            return {"ok": False, "error": "That check has no page to open."}
        if self._demo:
            return {"opened": False}  # demo mode never opens anything
        if url.startswith("x-apple.systempreferences:"):
            ok = subprocess.run(["/usr/bin/open", url], check=False, capture_output=True).returncode == 0
        else:
            ok = webbrowser.open(url)
        return {"opened": bool(ok)}

    @_safe
    def review_save_report(self) -> Dict[str, Any]:
        """Write the report as a self-contained HTML file in the data folder and open it, so it
        can be printed to PDF or shared. It holds no addresses, link or config values."""
        data = self._review_data()
        folder = self._data_dir / "reports"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / review.report_filename(data["generated_ms"])
        path.write_text(review.report_html(data, __version__), encoding="utf-8")
        opened = False
        if not self._demo:
            opened = subprocess.run(["/usr/bin/open", str(path)], check=False,
                                    capture_output=True).returncode == 0
        return {"path": str(path), "opened": opened}

    # --- MCP -------------------------------------------------------------------------
    @_safe
    def mcp_config(self) -> Dict[str, Any]:
        args = ["--data-dir", str(self._data_dir), "mcp"]
        if _in_app_bundle(sys.executable):
            # Packaged .app: sys.executable is the app's launcher, which always runs
            # safecontext and passes its arguments straight through (it doesn't take -m).
            server = {"command": sys.executable, "args": args}
        else:
            src = str(Path(__file__).resolve().parents[2])
            server = {"command": sys.executable, "args": ["-m", "safecontext", *args],
                      "env": {"PYTHONPATH": src, "PYTHONWARNINGS": "ignore"}}
        # "safecontext-dev" from a checkout, so both can be added to one MCP client.
        return {"json": json.dumps({"mcpServers": {env.mcp_server_name(): server}}, indent=2)}

    # --- updates (updater.py) ---------------------------------------------------------
    def _need_updater(self) -> upd.Updater:
        if self._updater is None:
            raise auth.NotConfigured("Updates are off in demo mode.")
        return self._updater

    @_safe
    def update_status(self) -> Dict[str, Any]:
        if self._updater is None:
            return {"current": upd.__version__, "disabled": True}
        return self._updater.status()

    @_safe
    def update_check(self) -> Dict[str, Any]:
        return self._need_updater().check(manual=True)

    @_safe
    def update_set_auto(self, on: bool) -> Dict[str, Any]:
        u = self._need_updater()
        u.set_auto_check(bool(on))
        return u.status()

    @_safe
    def update_install(self) -> Dict[str, Any]:
        """Download and verify the new version, then quit so the helper can swap it in."""
        u = self._need_updater()
        try:
            return u.start_install(self._restart_into)
        except upd.UpdateError as e:
            return {"ok": False, "error": str(e)}

    @_safe
    def update_queue(self) -> Dict[str, Any]:
        """Install when I quit: download and verify now, swap after the user quits (no reopen)."""
        try:
            return self._need_updater().queue_for_quit()
        except upd.UpdateError as e:
            return {"ok": False, "error": str(e)}

    @_safe
    def update_cancel_queue(self) -> Dict[str, Any]:
        return self._need_updater().cancel_quit_install()

    @_safe
    def update_set_on_quit(self, on: bool) -> Dict[str, Any]:
        return self._need_updater().set_install_on_quit(bool(on))

    @_safe
    def update_later(self) -> Dict[str, Any]:
        return self._need_updater().later()

    @_safe
    def update_skip(self) -> Dict[str, Any]:
        try:
            return self._need_updater().skip()
        except upd.UpdateError as e:
            return {"ok": False, "error": str(e)}

    @_safe
    def update_unskip(self) -> Dict[str, Any]:
        return self._need_updater().unskip()

    def _on_window_closing(self) -> None:
        """The window is closing (red button or ⌘Q): start an "install when I quit" swap, if one is staged.
        Not exposed to the page (leading underscore)."""
        if self._updater is not None:
            try:
                self._updater.on_quit()
            except Exception:
                logging.getLogger("safecontext.app").exception("install on quit failed")

    def _restart_into(self, plan: upd.SwapPlan) -> None:
        assert self._updater is not None
        self._updater.launch_helper(plan)
        if self._quit_fn is not None and not self._updater.is_demo:  # the demo stays open
            self._quit_fn()

    @_safe
    def update_open_download(self) -> Dict[str, Any]:
        """The manual path: open the download page in the browser."""
        if self._demo:
            return {"opened": False}
        webbrowser.open(upd.DOWNLOAD_PAGE)
        return {"opened": True}

    def _shutdown(self) -> None:
        self._stop.set()
        self._grok.shutdown()
        if self._updater is not None:
            self._updater.stop()



def _real_gmail_connect(mode: str, email: Optional[str]) -> str:
    _, account = auth.connect(mode, login_hint=email or None)
    return account
