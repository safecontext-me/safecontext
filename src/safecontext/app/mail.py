"""The Mail view: browse what's synced like a mail client, and see what isn't.

Kept items come from `items` (already scrubbed). Dropped items come from
`dropped`, which holds no content at all: only the id, thread, date and the
reason code. Nothing here fetches anything; the view only reads the store.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ..sources import Sources
from ..store import GMAIL, Store
from ..sync import read_heartbeat

ERROR_GROUP = "Couldn't be processed"

# Reason code -> (short label for a row, one sentence on why). Groups for the
# filter chips are backend.REASON_LABELS, the same names "What was removed" uses.
REASON_SHORT: Dict[str, tuple] = {
    "spam_trash": ("Spam or Trash", "It was in Spam or Trash."),
    "draft": ("Draft", "Drafts aren't synced."),
    "chat": ("Chat", "Old Google Chat messages aren't synced."),
    "category:promotions": ("Promotions", "Gmail filed it under Promotions."),
    "category:social": ("Social", "Gmail filed it under Social."),
    "category:forums": ("Forums", "Gmail filed it under Forums."),
    "bulk:list_unsubscribe": ("Newsletter", "It has an unsubscribe header, so it's a newsletter or mailing list."),
    "bulk:precedence": ("Bulk mail", "It's marked as bulk mail."),
    "bulk:no_reply_sender": ("No-reply sender", "It came from a no-reply address."),
    "security:subject": ("Security code or sign-in", "Its subject looks like a security code, sign-in or "
                         "password email."),
    "security:body": ("Security code", "It contains a security code or a password reset link."),
    "sensitive:sender_domain": ("Bank/financial, tax or health sender", "The sender is a bank, tax or health "
                                "service."),
    "sensitive:sender_keyword": ("Bank/financial, tax or health sender", "The sender's domain looks like a bank, "
                                 "insurer, clinic or tax service."),
    "business_sender": ("Business or automated text", "It came from a shortcode, a business sender ID or a "
                        "toll-free number, the way banks, pharmacies and sign-in codes are sent."),
    "sensitive:text": ("Bank, tax or health text", "It reads like a bank, tax or health notice (a balance, a "
                       "charge, a prescription or an appointment)."),
    "sensitive:subject": ("Statement, tax or medical subject", "Its subject or title mentions a statement, tax "
                          "form or medical matter."),
}


def reason_info(code: str, groups: Dict[str, str]) -> Dict[str, str]:
    if code.startswith("error:"):
        return {"code": code, "label": "Couldn't be processed", "group": ERROR_GROUP,
                "why": "Something in it couldn't be read, so it was skipped rather than stored unscrubbed."}
    short, why = REASON_SHORT.get(code, (code, ""))
    return {"code": code, "label": short, "group": groups.get(code, short), "why": why}


class MailView:
    def __init__(self, store: Store, sources: Sources, reason_groups: Dict[str, str]) -> None:
        self.store, self.sources, self.groups = store, sources, reason_groups

    # --- which sources a mailbox covers -----------------------------------------
    def _browsable(self) -> List[Any]:
        """Sources with something to show: connected, or with data on this Mac."""
        have = set(self.store.source_ids())
        return [s for s in self.sources if s.connected or s.id in have]

    def _scope(self, source: str) -> List[str]:
        """'' means every mail source; otherwise just that source."""
        if source:
            self.sources.get(source)  # unknown ids raise NotConfigured
            return [source]
        return [s.id for s in self._browsable() if "message" in s.kinds]

    def _codes_for(self, scope: Sequence[str], groups: Optional[Sequence[str]]) -> Optional[List[str]]:
        if not groups:
            return None
        want = set(groups)
        return [c for c in self.store.dropped_reasons(scope) if reason_info(c, self.groups)["group"] in want]

    # --- calls ---------------------------------------------------------------------
    def folders(self) -> Dict[str, Any]:
        """The Mail sidebar: mailboxes with counts, then each source."""
        srcs = []
        mail_kept = mail_dropped = 0
        for s in self._browsable():
            kept, dropped = self.store.counts(s.id)
            if "message" in s.kinds:
                mail_kept, mail_dropped = mail_kept + kept, mail_dropped + dropped
            srcs.append({"id": s.id, "name": s.name, "account": s.account, "kinds": list(s.kinds),
                         "kept": kept, "dropped": dropped, "mail": "message" in s.kinds})
        return {"mailboxes": {"all": mail_kept + mail_dropped, "synced": mail_kept, "not_synced": mail_dropped},
                "sources": srcs}

    def reasons(self, source: str = "") -> List[Dict[str, Any]]:
        """Filter chips for Not synced: reason groups with counts, largest first."""
        out: Dict[str, Dict[str, Any]] = {}
        for code, n in self.store.dropped_reasons(self._scope(source)).items():
            info = reason_info(code, self.groups)
            g = out.setdefault(info["group"], {"group": info["group"], "count": 0})
            g["count"] += n
        return sorted(out.values(), key=lambda g: -g["count"])

    def _decorate(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for r in rows:
            if r["type"] == "dropped":
                r["reason_info"] = reason_info(r["reason"], self.groups)
        return rows

    def list(self, view: str = "all", source: str = "", query: str = "", reasons: Optional[Sequence[str]] = None,
             cursor: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        scope = self._scope(source)
        query = (query or "").strip()
        if query:
            if view == "not_synced":  # dropped items have no stored text to search
                return {"rows": [], "next": None, "searched": False}
            page = self.store.search_threads(query, scope, offset=int(cursor or 0), limit=limit)
            return {**page, "rows": self._decorate(page["rows"]), "searched": True}
        page = self.store.browse(scope, view=view, reasons=self._codes_for(scope, reasons) if view == "not_synced"
                                 else None, cursor=cursor, limit=limit)
        return {**page, "rows": self._decorate(page["rows"]), "searched": False}

    def thread(self, source: str, thread_id: str) -> Dict[str, Any]:
        self.sources.get(source)
        out = self.store.thread_detail(source, thread_id)
        self._decorate(out["entries"])
        return out

    def coverage(self, source: str = "", progress: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Per source: date range, kept / dropped (by reason) / pending, last sync,
        a running sync's progress, and what is still to fetch or never fetched."""
        out = []
        for s in self._browsable():
            if source and s.id != source:
                continue
            if not source and "message" not in s.kinds:
                continue
            out.append(self._coverage_one(s, (progress or {}).get(s.id)))
        return out

    def _coverage_one(self, s: Any, live: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        c = self.store.coverage(s.id)
        st = s.state
        hb = read_heartbeat(self.store, s.id) or live
        by_group: Dict[str, int] = {}
        for code, n in c["dropped_by_reason"].items():
            g = reason_info(code, self.groups)["group"]
            by_group[g] = by_group.get(g, 0) + n
        first_done = s.first_sync_done()
        last = st.get("last_sync_ms")
        notes: List[str] = []
        remaining: Optional[int] = None
        if s.id == GMAIL:
            total = st.get("mailbox_total")
            if not first_done:
                if total and total.isdigit():
                    remaining = max(0, int(total) - c["kept"] - c["dropped"])
                notes.append("First sync in progress: it works from newest to oldest, so older mail is still "
                             "to fetch." if hb else
                             "First sync paused before reaching your oldest mail. Sync now to continue.")
            if (st.get("sync_mode") or "full") == "headers":
                notes.append("Headers only: sender, recipients, subject and date are stored, no message text.")
            else:
                notes.append("Promotions, Social, Forums, Chats, Drafts, Spam and Trash are never downloaded, "
                             "so they aren't counted here.")
        elif not first_done:
            notes.append("First sync in progress." if hb else "Not synced yet.")
        if c["stale"]:
            notes.append(f"{c['stale']:,} items will be re-fetched and re-scrubbed after a rule change.")
        return {
            "id": s.id, "name": s.name, "account": s.account,
            "oldest_ms": c["oldest_ms"], "newest_ms": c["newest_ms"],
            "kept": c["kept"], "dropped": c["dropped"],
            "dropped_by_group": dict(sorted(by_group.items(), key=lambda kv: -kv[1])),
            "pending": c["stale"], "remaining_estimate": remaining,
            "first_sync_done": first_done, "last_sync_ms": int(last) if last else None,
            "syncing": hb is not None,
            "progress": None if hb is None else {k: hb.get(k) for k in ("phase", "fetched", "kept", "dropped",
                                                                       "started_ms", "per_min")},
            "last_error": st.get("last_error") or "",
            "notes": notes,
        }
