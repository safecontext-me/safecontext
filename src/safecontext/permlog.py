"""Permission log: what AI apps asked SafeContext for, what you answered, and what changed.

Kept only on this Mac, in a 0600 file in SafeContext's folder. Each entry is a time, a fixed kind
and at most a short app name and a short fixed-format note. It never holds codes, tokens, the MCP
link, what an app searched for, or values from other apps' files.

Kinds (the UI words live in describe()):
  connect      an app asked to connect; outcome approved | denied | expired | cancelled
  signins      the number of sign-ins AI apps hold changed (note "2 → 3")
  link         MCP link turned on | off | regenerated (every app signed out)
  link_only    "Connect with just the link" turned on | off
  keep_awake   turned on | off
  scan         the opt-in scan was turned on | off
  scan_change  the scan found a change (note says what, e.g. "Claude Desktop: MCP server added: notes")
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

log = logging.getLogger(__name__)

KEEP = 500
KEEP_DAYS = 365
DAY_MS = 86_400_000
KINDS = {"connect", "signins", "link", "link_only", "keep_awake", "scan", "scan_change"}
OUTCOMES = {"approved", "denied", "expired", "cancelled", "on", "off", "regenerated", ""}
MAX_TEXT = 120


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clean(s: Any, n: int) -> str:
    return " ".join(str(s or "").split())[:n]


class PermissionLog:
    def __init__(self, path: Optional[Path], now=_now_ms) -> None:
        self.path = Path(path) if path else None
        self._now = now
        self._lock = threading.Lock()
        self._entries: List[Dict[str, Any]] = []
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self._entries = [e for e in data if isinstance(e, dict) and e.get("kind") in KINDS
                                 and isinstance(e.get("ts_ms"), int)]
            except (OSError, ValueError, TypeError):
                self._entries = []

    def record(self, kind: str, outcome: str = "", client: str = "", note: str = "") -> Optional[Dict[str, Any]]:
        if kind not in KINDS or outcome not in OUTCOMES:
            return None
        e = {"ts_ms": self._now(), "kind": kind, "outcome": outcome}
        if client:
            e["client"] = _clean(client, 60)
        if note:
            e["note"] = _clean(note, MAX_TEXT)
        with self._lock:
            self._entries.append(e)
            self._trim()
            self._save()
        return e

    def entries(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(e, text=describe(e)) for e in reversed(self._entries[-limit:])]

    def since(self, ms: int, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._entries if e["ts_ms"] >= ms and (kind is None or e["kind"] == kind)]

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._save()

    def _trim(self) -> None:
        cutoff = self._now() - KEEP_DAYS * DAY_MS
        self._entries = [e for e in self._entries if e["ts_ms"] >= cutoff][-KEEP:]

    def _save(self) -> None:
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self._entries, f)
            os.replace(tmp, self.path)
        except OSError as err:
            log.warning("couldn't save the permission log: %s", err)


_CONNECT = {"approved": "You approved it", "denied": "You denied it",
            "expired": "No answer in time, so it was refused", "cancelled": "The app gave up before you answered"}


def describe(e: Dict[str, Any]) -> str:
    """One plain sentence for an entry."""
    k, o, who, note = e.get("kind"), e.get("outcome", ""), e.get("client") or "An AI app", e.get("note", "")
    if k == "connect":
        return f"{who} asked to connect. {_CONNECT.get(o, '')}".strip()
    if k == "signins":
        return f"Sign-ins held by AI apps: {note}" if note else "Sign-ins held by AI apps changed"
    if k == "link":
        return {"on": "MCP link turned on", "off": "MCP link turned off",
                "regenerated": "New MCP link made. Every AI app was signed out"}.get(o, "MCP link changed")
    if k == "link_only":
        return ("\"Connect with just the link\" turned on: no approval needed" if o == "on"
                else "\"Connect with just the link\" turned off: apps need your approval")
    if k == "keep_awake":
        return f"Keep awake turned {o}"
    if k == "scan":
        return f"Checking Claude Desktop and Claude Code turned {o}"
    if k == "scan_change":
        return note or "Claude Desktop or Claude Code settings changed"
    return ""


# --- what the opt-in scan saw, as a set of facts, so a later scan can say what changed -----------

def scan_facts(scan: Optional[Dict[str, Any]]) -> Set[str]:
    """Names, modes and rules only (the scan never returns values)."""
    out: Set[str] = set()
    if not scan:
        return out
    d = scan.get("claude_desktop")
    if d:
        out.update(f"Claude Desktop: MCP server|{s['name']}" for s in d.get("servers", []) if not s.get("own"))
    c = scan.get("claude_code")
    if c:
        out.update(f"Claude Code: MCP server|{s['name']}" for s in c.get("servers", []) if not s.get("own"))
        out.update(f"Claude Code: project MCP server|{n}" for n in c.get("project_servers", []))
        out.update(f"Claude Code: broad allow rule|{b['rule']}" for b in c.get("broad", []))
        if c.get("mode") == "bypassPermissions" or c.get("skip_prompt"):
            out.add("Claude Code: skips permission prompts|")
        elif c.get("mode") == "auto":
            out.add("Claude Code: auto mode by default|")
    return out


def scan_changes(before: Iterable[str], after: Iterable[str]) -> List[str]:
    """Plain notes for what appeared and what went, added first, sorted so the order is stable."""
    before, after = set(before), set(after)

    def note(fact: str, verb: str) -> str:
        what, _, name = fact.partition("|")
        return f"{what} {verb}: {name}" if name else f"{what} ({verb})"
    return [note(f, "added") for f in sorted(after - before)] + [note(f, "removed") for f in sorted(before - after)]
