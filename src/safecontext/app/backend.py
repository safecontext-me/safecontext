"""The object the app window calls into (exposed to JavaScript by pywebview).

There is no HTTP server: the window talks to Python over pywebview's
in-process bridge, so no other program on the Mac can query your mail.

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
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .. import auth, grok
from ..store import DEFAULT_DATA_DIR, Store
from ..sync import SyncResult, Syncer, read_heartbeat
from ..tools import run_tool

log = logging.getLogger("safecontext.app")

AUTO_SYNC_SECONDS = 15 * 60

REASON_LABELS = {
    "spam_trash": "Spam & Trash", "draft": "Drafts", "chat": "Chats",
    "category:promotions": "Promotions", "category:social": "Social", "category:forums": "Forums",
    "bulk:list_unsubscribe": "Newsletters & mailing lists", "bulk:precedence": "Bulk mail",
    "bulk:no_reply_sender": "No-reply senders",
    "security:subject": "Security & 2FA codes", "security:body": "Security & 2FA codes",
    "sensitive:sender_domain": "Banks, tax & health senders", "sensitive:sender_keyword": "Banks, tax & health senders",
    "sensitive:subject": "Statements, tax & medical subjects",
}
REDACTION_LABELS = {
    "ONE_TIME_CODE": "2FA / one-time codes", "TOTP_SECRET": "Authenticator keys", "BACKUP_CODE": "Backup codes",
    "CREDIT_CARD": "Card numbers", "CVV": "Card security codes", "SSN": "Social Security numbers",
    "API_KEY": "API keys", "JWT": "Login tokens", "PRIVATE_KEY": "Private keys", "PASSWORD": "Passwords",
    "IBAN": "IBANs", "ROUTING_NUMBER": "Routing numbers", "BANK_ACCOUNT": "Bank account numbers",
    "DATE_OF_BIRTH": "Dates of birth", "PHONE": "Phone numbers", "STREET_ADDRESS": "Street addresses",
    "HEALTH": "Health details", "LEGAL": "Legal matters", "PERSONAL": "Personal matters",
}


def _in_app_bundle(executable: str) -> bool:
    """True for a Briefcase .app launcher (…/SafeContext.app/Contents/MacOS/SafeContext),
    false for a python interpreter, including a framework build's Python.app."""
    exe = Path(executable)
    return (exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents"
            and not exe.name.lower().startswith("python"))


def _safe(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            out = fn(*args, **kwargs)
            return {"ok": True, **(out or {})}
        except auth.NeedsReconnect as e:
            return {"ok": False, "error": e.message, "needs_reconnect": True}
        except (auth.NotConfigured, grok.GrokError) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # never leak a traceback (or mail content) into the UI
            log.exception("app call failed: %s", fn.__name__)
            return {"ok": False, "error": f"Something went wrong ({type(e).__name__})."}
    return wrapper


class Backend:
    """Public methods are callable from the window. Everything else is underscore-prefixed
    because pywebview exposes public attributes, including methods of nested objects."""

    def __init__(self, data_dir: Optional[Path] = None, *, demo: bool = False,
                 gmail_factory: Optional[Callable[[str, str], Any]] = None,
                 connect_fn: Optional[Callable[[str, Optional[str]], str]] = None,
                 grok_client_factory: Optional[Callable[[], Any]] = None,
                 auto_sync: bool = True) -> None:
        self._demo = demo
        self._data_dir = Path(data_dir or DEFAULT_DATA_DIR)
        self._store = Store(self._data_dir / "mail.db")
        self._gmail_factory = gmail_factory or self._real_gmail
        self._connect_fn = connect_fn or self._real_connect
        self._grok_client_factory = grok_client_factory
        self._agent: Optional[grok.GrokMailAgent] = None
        self._sync_thread: Optional[threading.Thread] = None
        self._sync_lock = threading.Lock()
        self._progress = SyncResult(status="idle")
        self._last_result: Optional[SyncResult] = None
        self._stop = threading.Event()
        if auto_sync:
            threading.Thread(target=self._auto_sync_loop, daemon=True, name="auto-sync").start()

    # --- wiring for real Gmail / Google sign-in ---------------------------
    @staticmethod
    def _real_connect(mode: str, email: Optional[str]) -> str:
        _, account = auth.connect(mode, login_hint=email or None)
        return account

    @staticmethod
    def _real_gmail(account: str, mode: str):
        from ..gmail_api import GoogleGmailAPI
        return GoogleGmailAPI(auth.get_credentials(account, mode))

    # --- status ------------------------------------------------------------
    @_safe
    def status(self) -> Dict[str, Any]:
        s = self._store
        p = self._progress
        last = s.get_meta("last_sync_ms")
        kept, dropped = s.counts()
        hb = read_heartbeat(s)
        if self._is_syncing() and hb is None:  # local thread starting up, before its first beat
            hb = {"phase": p.phase, "fetched": p.fetched, "kept": p.kept, "dropped": p.dropped}
        oldest, newest = s.date_range()
        return {
            "demo": self._demo,
            "account": s.get_meta("account_email"),
            "mode": s.get_meta("sync_mode") or "full",
            "needs_reconnect": s.get_meta("auth_state") == "needs_reconnect",
            "reconnect_message": auth.RECONNECT_MESSAGE,
            "first_sync_done": s.get_meta("history_id") is not None,
            "syncing": hb is not None,
            "progress": None if hb is None else {
                "phase": hb.get("phase") or "", "fetched": hb.get("fetched", 0), "kept": hb.get("kept", 0),
                "dropped": hb.get("dropped", 0), "started_ms": hb.get("started_ms"),
                "per_min": hb.get("per_min"), "waiting_until_ms": hb.get("waiting_until_ms"),
                "elsewhere": hb.get("pid") not in (None, os.getpid()),
                "backfill": s.get_meta("history_id") is None},
            "oldest_ms": oldest, "newest_ms": newest,
            "last_result": None if not self._last_result else {
                "status": self._last_result.status, "message": self._last_result.message},
            "last_sync_ms": int(last) if last else None,
            "kept": kept, "dropped": dropped,
            "has_xai_key": bool(self._grok_client_factory) or bool(grok.get_api_key()),
            "model": s.get_meta("grok_model") or grok.DEFAULT_MODEL,
            "data_dir": str(self._data_dir),
        }

    def _is_syncing(self) -> bool:
        return bool(self._sync_thread and self._sync_thread.is_alive())

    # --- connect / sync / disconnect ----------------------------------------
    @_safe
    def connect(self, mode: str = "full", email: Optional[str] = None) -> Dict[str, Any]:
        if mode not in auth.MODES:
            return {"ok": False, "error": "Unknown mode."}
        account = self._connect_fn(mode, email)
        previous = self._store.get_meta("account_email")
        if previous and previous != account:
            raise auth.NotConfigured(f"This Mac already holds mail for {previous}. Disconnect and delete it first "
                                     "to switch accounts.")
        self._store.set_meta("account_email", account)
        self._store.set_meta("sync_mode", mode)
        self._store.set_meta("auth_state", "ok")
        self._start_sync()
        return {"account": account}

    @_safe
    def sync_now(self) -> Dict[str, Any]:
        return {"started": self._start_sync()}

    def _start_sync(self) -> bool:
        with self._sync_lock:
            if (self._is_syncing() or read_heartbeat(self._store) is not None
                    or not self._store.get_meta("account_email")):
                return False
            self._sync_thread = threading.Thread(target=self._run_sync, daemon=True, name="sync")
            self._sync_thread.start()
            return True

    def _run_sync(self) -> None:
        account = self._store.get_meta("account_email") or ""
        mode = self._store.get_meta("sync_mode") or "full"
        self._progress = SyncResult(status="running", phase="starting")
        try:
            api = self._gmail_factory(account, mode)
            result = Syncer(self._store, api, mode=mode, progress=self._on_progress).run()
        except auth.NeedsReconnect as e:
            self._store.set_meta("auth_state", "needs_reconnect")
            result = SyncResult(status="needs_reconnect", message=e.message)
        except (auth.TransientAuthError, auth.NotConfigured) as e:
            result = SyncResult(status="retry_later", message=str(e) or "Couldn't reach Google.")
        except Exception as e:
            log.exception("sync crashed")
            result = SyncResult(status="retry_later", message=f"Sync stopped unexpectedly ({type(e).__name__}).")
        if result.status != "busy":
            self._last_result = result
        self._progress = result

    def _on_progress(self, r: SyncResult) -> None:
        self._progress = SyncResult(status="running", phase=r.phase, fetched=r.fetched, kept=r.kept,
                                    dropped=r.dropped, deleted=r.deleted)

    def _auto_sync_loop(self) -> None:
        while not self._stop.wait(AUTO_SYNC_SECONDS):
            if self._store.get_meta("auth_state") != "needs_reconnect":
                self._start_sync()

    @_safe
    def disconnect(self, wipe: bool = False) -> Dict[str, Any]:
        account = self._store.get_meta("account_email") or ""
        if account and not self._demo:
            auth.disconnect(account)
        if wipe:
            if self._sync_thread:
                self._sync_thread.join(timeout=30)
            self._store.wipe()
        else:
            self._store.set_meta("auth_state", "needs_reconnect")
        self._agent = None
        return {"wiped": bool(wipe)}

    # --- browsing mail ---------------------------------------------------------
    @_safe
    def search(self, query: str, sender: str = "", after: str = "", before: str = "") -> Dict[str, Any]:
        args = {"query": query, "limit": 50}
        args.update({k: v for k, v in (("sender", sender), ("after", after), ("before", before)) if v})
        return self._tool("search_mail", args)

    @_safe
    def recent(self, sender: str = "", after: str = "", before: str = "") -> Dict[str, Any]:
        args: Dict[str, Any] = {"limit": 50}
        args.update({k: v for k, v in (("sender", sender), ("after", after), ("before", before)) if v})
        return self._tool("list_messages", args)

    @_safe
    def read_thread(self, thread_id: str) -> Dict[str, Any]:
        return self._tool("read_thread", {"thread_id": thread_id})

    def _tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        out = run_tool(self._store, name, args)
        if "error" in out:
            raise auth.NotConfigured(out["error"])
        return out

    @_safe
    def open_in_gmail(self, url: str) -> Dict[str, Any]:
        # Only ever open Gmail links we generated, never a URL taken from email content.
        if not isinstance(url, str) or not url.startswith("https://mail.google.com/mail/"):
            return {"ok": False, "error": "Only Gmail links can be opened."}
        webbrowser.open(url)
        return {}

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
        return {"kept": st["kept"], "dropped_total": st["dropped"],
                "dropped": dict(sorted(dropped.items(), key=lambda kv: -kv[1])), "redacted": redacted}

    # --- Grok ----------------------------------------------------------------------
    @_safe
    def ask(self, question: str) -> Dict[str, Any]:
        if not question or not question.strip():
            return {"ok": False, "error": "Type a question first."}
        if self._agent is None:
            client = self._grok_client_factory() if self._grok_client_factory else grok.make_client()
            self._agent = grok.GrokMailAgent(self._store, client=client,
                                             model=self._store.get_meta("grok_model") or None)
        ans = self._agent.ask(question.strip())
        return {"text": ans.text, "sources": ans.sources, "tool_calls": ans.tool_calls}

    @_safe
    def new_chat(self) -> Dict[str, Any]:
        self._agent = None
        return {}

    @_safe
    def set_xai_key(self, key: str) -> Dict[str, Any]:
        key = (key or "").strip()
        if not key.startswith("xai-") or len(key) < 20:
            return {"ok": False, "error": "That doesn't look like an xAI API key (they start with “xai-”)."}
        grok.set_api_key(key)
        self._agent = None
        return {}

    @_safe
    def clear_xai_key(self) -> Dict[str, Any]:
        grok.clear_api_key()
        self._agent = None
        return {}

    @_safe
    def set_model(self, model: str) -> Dict[str, Any]:
        model = (model or "").strip()
        self._store.set_meta("grok_model", model or None)
        self._agent = None
        return {"model": model or grok.DEFAULT_MODEL}

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
        return {"json": json.dumps({"mcpServers": {"safecontext": server}}, indent=2)}

    def _shutdown(self) -> None:
        self._stop.set()

