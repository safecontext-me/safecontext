"""The object the app window calls into (exposed to JavaScript by pywebview).

There is no HTTP server: the window talks to Python over pywebview's
in-process bridge, so no other program on the Mac can query your mail.
Use in Grok (grok_link.py) only ever makes an outbound connection.

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
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .. import auth, env, migrate
from ..errors import NeedsPermission
from ..grok_link import GROK_CONNECTORS_URL, GrokLink
from ..sources import Sources, default_sources
from ..store import DEFAULT_DATA_DIR, GMAIL, Store
from ..sync import SyncResult, read_heartbeat
from ..tools import run_tool
from .mail import MailView

log = logging.getLogger("safecontext.app")

AUTO_SYNC_SECONDS = 15 * 60

REASON_LABELS = {
    "spam_trash": "Spam & Trash", "draft": "Drafts", "chat": "Chats",
    "category:promotions": "Promotions", "category:social": "Social", "category:forums": "Forums",
    "bulk:list_unsubscribe": "Newsletters & mailing lists", "bulk:precedence": "Bulk mail",
    "bulk:no_reply_sender": "No-reply senders",
    "security:subject": "Security & 2FA codes", "security:body": "Security & 2FA codes",
    "sensitive:sender_domain": "Banks, tax & health senders", "sensitive:sender_keyword": "Banks, tax & health senders",
    "sensitive:subject": "Statements, tax & medical subjects or titles",
    "business_sender": "Texts from businesses & shortcodes",
    "sensitive:text": "Bank, tax & health texts",
}

REDACTION_LABELS = {
    "ONE_TIME_CODE": "2FA / one-time codes", "TOTP_SECRET": "Authenticator keys", "BACKUP_CODE": "Backup codes",
    "CREDIT_CARD": "Card numbers", "CVV": "Card security codes", "SSN": "Social Security numbers",
    "API_KEY": "API keys", "JWT": "Login tokens", "PRIVATE_KEY": "Private keys", "PASSWORD": "Passwords",
    "IBAN": "IBANs", "ROUTING_NUMBER": "Routing numbers", "BANK_ACCOUNT": "Bank account numbers",
    "DATE_OF_BIRTH": "Dates of birth", "PHONE": "Phone numbers", "STREET_ADDRESS": "Street addresses",
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
        except Exception as e:  # never leak a traceback (or mail content) into the UI
            log.exception("app call failed: %s", fn.__name__)
            return {"ok": False, "error": f"Something went wrong ({type(e).__name__})."}
    return wrapper


_NOOP_REVOKE = lambda account, revoke=True: None  # noqa: E731  (demo mode: no Google to revoke at)


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
                 auto_sync: bool = True) -> None:
        self._demo = demo
        self._data_dir = Path(data_dir or DEFAULT_DATA_DIR)
        self._store = Store(self._data_dir / "mail.db")
        self._connect_fn = connect_fn  # None: the real Google sign-in
        self._sources: Sources = default_sources(
            self._store, gmail_factory=gmail_factory,
            gmail_connect=lambda mode, email: (self._connect_fn or _real_gmail_connect)(mode, email),
            calendar_factory=calendar_factory, calendar_connect=calendar_connect_fn,
            revoke_fn=_NOOP_REVOKE if demo else None, imessage_options=imessage_options)
        if not demo:
            migrate.remove_ask_leftovers(self._store)  # the old Ask feature's xAI key, once
        self._grok = (grok_link or GrokLink)(self._store, self._data_dir)
        self._mail = MailView(self._store, self._sources, REASON_LABELS)
        self._threads: Dict[str, threading.Thread] = {}
        self._sync_lock = threading.Lock()
        self._progress: Dict[str, SyncResult] = {}
        self._last_result: Dict[str, SyncResult] = {}
        self._stop = threading.Event()
        if auto_sync:
            threading.Thread(target=self._auto_sync_loop, daemon=True, name="auto-sync").start()
            self._grok.start()  # reconnect to the relay if Grok was left on

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
                if x["state"] not in ("coming_soon", "not_connected"):
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
        self._last_result.pop(source_id, None)
        return {"wiped": bool(wipe)}

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

    def _start_sync(self, source_id: str) -> bool:
        src = self._sources.get(source_id)
        with self._sync_lock:
            if (self._is_syncing(source_id) or read_heartbeat(self._store, source_id) is not None
                    or not src.connected):
                return False
            t = threading.Thread(target=self._run_sync, args=(source_id,), daemon=True, name=f"sync-{source_id}")
            self._threads[source_id] = t
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

    def _on_progress(self, source_id: str, r: SyncResult) -> None:
        self._progress[source_id] = SyncResult(status="running", phase=r.phase, fetched=r.fetched, kept=r.kept,
                                               dropped=r.dropped, deleted=r.deleted)

    def _auto_sync_loop(self) -> None:
        while not self._stop.wait(AUTO_SYNC_SECONDS):
            for src in self._sources.connected():  # each source on its own thread; one failing never blocks another
                if src.state.get("auth_state") not in ("needs_reconnect", "needs_permission"):
                    try:
                        self._start_sync(src.id)
                    except Exception:
                        log.exception("couldn't start %s sync", src.id)

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
            return {"ok": False, "error": "Only links back to the original (Gmail, Google Calendar) can be opened."}
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
                  cursor: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        return self._mail.list(view, source, query, reasons or None, cursor, limit)

    @_safe
    def mail_thread(self, source: str, thread_id: str) -> Dict[str, Any]:
        return self._mail.thread(source, thread_id)

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

    # --- Use in Grok ---------------------------------------------------------------
    @_safe
    def grok_status(self) -> Dict[str, Any]:
        return self._grok.status()

    @_safe
    def grok_enable(self) -> Dict[str, Any]:
        """Turn on: connect to the relay, copy the connector link, open Grok's connectors page."""
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
            return {"ok": False, "error": "Turn on Use in Grok first."}
        return {"url": url, "copied": self._copy(url)}

    @_safe
    def grok_approve(self, approval_id: str, ok: bool = True) -> Dict[str, Any]:
        if not self._grok.approve(str(approval_id), bool(ok)):
            return {"ok": False, "error": "That request expired. Try connecting again in Grok."}
        return {}

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
    def open_grok_connectors(self) -> Dict[str, Any]:
        if self._demo:
            return {"opened": False}  # demo mode never touches the network
        webbrowser.open(GROK_CONNECTORS_URL)
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

    def _shutdown(self) -> None:
        self._stop.set()
        self._grok.shutdown()



def _real_gmail_connect(mode: str, email: Optional[str]) -> str:
    _, account = auth.connect(mode, login_hint=email or None)
    return account
