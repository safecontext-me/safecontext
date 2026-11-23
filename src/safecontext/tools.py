"""The tools, defined once and served to MCP clients: local ones over stdio
(mcp_server.py) and Grok through the relay (grok_link.py). They search, read and list across every connected source (Gmail,
Google Calendar, ...), with an optional source filter. Every result carries a
citation: date, sender, source and a deep link back to the original.
Everything returned comes from the scrubbed store.

`search_mail`, `read_thread` and `list_messages` are the v1 Gmail tools, kept
as compatible aliases for MCP clients configured before other sources existed.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .auth import RECONNECT_MESSAGE
from .scrub import KINDS
from .sources import SOURCE_NAMES
from .store import GMAIL, Hit, ItemView, Store

MAX_BODY_CHARS = 6000
MAX_THREAD_MESSAGES = 20
MAX_THREAD_CHARS = 40000

_DATE = {"type": "string", "description": "Date as YYYY-MM-DD (UTC) or full ISO 8601 timestamp."}
_SOURCE = {"type": "string",
           "description": "Optional: only this source, e.g. 'gmail' or 'gcal' (Google Calendar). "
                          "Call list_sources to see what's connected. Omit to cover every source."}
_KIND = {"type": "string", "enum": list(KINDS),
         "description": "Optional: only this kind of item: message (email), event, chat or transcript."}
_SENDER = {"type": "string", "description": "Optional sender / organizer filter: a full address, "
                                            "'@domain.com', or part of a name."}
_AFTER = {**_DATE, "description": "Only items on or after this date (events: ending on or after). "
                                  + _DATE["description"]}
_BEFORE = {**_DATE, "description": "Only items before this date (events: starting before). " + _DATE["description"]}


def _search_schema(with_source: bool) -> Dict[str, Any]:
    props: Dict[str, Any] = {"query": {"type": "string", "description": "Words to search for. All words must match."}}
    if with_source:
        props.update(source=_SOURCE, kind=_KIND)
    props.update(sender=_SENDER, after=_AFTER, before=_BEFORE,
                 limit={"type": "integer", "minimum": 1, "maximum": 50, "default": 10})
    return {"type": "object", "properties": props, "required": ["query"], "additionalProperties": False}


def _list_schema(with_source: bool) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    if with_source:
        props.update(source=_SOURCE, kind=_KIND)
    props.update(sender=_SENDER, after=_AFTER, before=_BEFORE)
    if with_source:
        props["order"] = {"type": "string", "enum": ["newest", "oldest"], "default": "newest",
                          "description": "newest first (default), or oldest first, e.g. for upcoming events."}
    props["limit"] = {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
    return {"type": "object", "properties": props, "additionalProperties": False}


_THREAD_SCHEMA = {
    "type": "object",
    "properties": {"thread_id": {"type": "string", "description": "thread_id from a search or list result."},
                   "source": {"type": "string", "description": "Optional: the source from that result."}},
    "required": ["thread_id"],
    "additionalProperties": False,
}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search",
        "description": (
            "Full-text search across the user's scrubbed data from every connected source (Gmail, Google "
            "Calendar, ...). Sensitive data has been replaced with [REDACTED:TYPE] and some items (security "
            "codes, banking, health, bulk mail) are excluded entirely. Returns matches with source, date, "
            "sender, title, a snippet and a link back to the original."),
        "parameters": _search_schema(True),
    },
    {
        "name": "read",
        "description": ("Read every stored item in a thread, oldest first: an email thread, or a calendar event "
                        "(with its attendees, location and call link). Includes date, sender and a link."),
        "parameters": _THREAD_SCHEMA,
    },
    {
        "name": "list_items",
        "description": ("List items filtered by source, kind, sender and/or date range, newest first by default. "
                        "No search words needed. For upcoming events use source='gcal', after=today, "
                        "order='oldest'."),
        "parameters": _list_schema(True),
    },
    {
        "name": "list_sources",
        "description": "Which sources are connected, how many items each holds, and when each last synced.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    # --- v1 Gmail tools, kept for MCP clients that call them by name ---------------
    {
        "name": "search_mail",
        "description": "Same as `search` with source='gmail': full-text search over the user's scrubbed Gmail.",
        "parameters": _search_schema(False),
        "alias": True,
    },
    {
        "name": "read_thread",
        "description": "Same as `read`: every stored message in a thread, oldest first, with date, sender and link.",
        "parameters": _THREAD_SCHEMA,
        "alias": True,
    },
    {
        "name": "list_messages",
        "description": "Same as `list_items` with source='gmail': messages newest first, by sender and/or dates.",
        "parameters": _list_schema(False),
        "alias": True,
    },
]

TOOL_NAMES = {t["name"] for t in TOOLS}
PRIMARY_TOOLS = [t for t in TOOLS if not t.get("alias")]


class ToolError(Exception):
    pass


def source_name(source: str) -> str:
    return SOURCE_NAMES.get(source, source)


def _cite(date: str, sender: str, source: str) -> str:
    who = f" · {sender}" if sender else ""
    return f"{date[:10]}{who} · {source_name(source)}"


def _hit(h: Hit) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "source": h.source, "kind": h.kind, "id": h.id, "thread_id": h.thread_id, "date": h.citation.date,
        "sender": h.citation.sender, "title": h.title, "snippet": h.snippet, "link": h.citation.link,
        "citation": _cite(h.citation.date, h.citation.sender, h.source)}
    if h.end:
        out["end"] = h.end
    if h.source == GMAIL:  # v1 field names
        out.update(subject=h.title, gmail_link=h.citation.link)
    return out


def _item(v: ItemView, budget: int) -> Dict[str, Any]:
    body = v.body[:min(MAX_BODY_CHARS, max(budget, 0))]
    out: Dict[str, Any] = {
        "source": v.source, "kind": v.kind, "id": v.id, "date": v.citation.date, "sender": v.citation.sender,
        "participants": v.participants, "title": v.title, "body": body, "link": v.citation.link,
        "citation": _cite(v.citation.date, v.citation.sender, v.source),
        "attachments": [{"filename": a.filename, "mime_type": a.mime_type, "size": a.size} for a in v.attachments]}
    if v.end:
        out["end"] = v.end
    if v.extra:
        out["details"] = v.extra
    if v.source == GMAIL:  # v1 field names
        out.update(recipients=v.participants, subject=v.title, gmail_link=v.citation.link)
    if len(body) < len(v.body):
        out["body_truncated"] = True
    if v.redactions:
        out["redactions"] = v.redactions
    return out


def _notice(store: Store, source: Optional[str] = None) -> str:
    notes = []
    for src in ([source] if source else store.source_ids()):
        name = source_name(src)
        auth_state = store.get_state(src, "auth_state")
        if not store.get_state(src, "account") and auth_state not in ("needs_reconnect", "needs_permission"):
            continue
        if auth_state == "needs_reconnect":
            notes.append((RECONNECT_MESSAGE if src == GMAIL else f"{name} needs reconnecting.")
                         + " Results below may be out of date.")
        elif auth_state == "needs_permission":
            notes.append(f"{name} needs permission on this Mac; its results may be out of date.")
        elif (store.get_state(src, "history_id") if src == GMAIL else store.get_state(src, "last_sync_ms")) is None:
            notes.append(f"The first full sync of {name} is still running, so results may be incomplete."
                         if src != GMAIL else "The first full sync is still running, so results may be incomplete.")
    return " ".join(notes)


def _str(args: Dict[str, Any], key: str, required: bool = False) -> Any:
    v = args.get(key)
    if v is None or v == "":
        if required:
            raise ToolError(f"'{key}' is required")
        return None
    if not isinstance(v, str):
        raise ToolError(f"'{key}' must be a string")
    return v


def _limit(args: Dict[str, Any], default: int, maximum: int) -> int:
    v = args.get("limit", default)
    try:
        return max(1, min(int(v), maximum))
    except (TypeError, ValueError):
        raise ToolError("'limit' must be an integer") from None


def _kind(args: Dict[str, Any]) -> Optional[str]:
    k = _str(args, "kind")
    if k and k not in KINDS:
        raise ToolError(f"'kind' must be one of {list(KINDS)}")
    return k


def search(store: Store, args: Dict[str, Any]) -> Dict[str, Any]:
    hits = store.search(_str(args, "query", True), source=_str(args, "source"), kind=_kind(args),
                        sender=_str(args, "sender"), after=_str(args, "after"), before=_str(args, "before"),
                        limit=_limit(args, 10, 50))
    return {"results": [_hit(h) for h in hits], "count": len(hits)}


def search_mail(store: Store, args: Dict[str, Any]) -> Dict[str, Any]:
    return search(store, {**args, "source": GMAIL})


def read(store: Store, args: Dict[str, Any]) -> Dict[str, Any]:
    tid = _str(args, "thread_id", True)
    views = store.read_thread(tid, _str(args, "source"))
    if not views:
        return {"thread_id": tid, "messages": [], "note": "Nothing stored in this thread (it may have been "
                                                           "excluded by the scrubber, or the id is wrong)."}
    omitted = max(0, len(views) - MAX_THREAD_MESSAGES)
    views = views[-MAX_THREAD_MESSAGES:]
    budget, items = MAX_THREAD_CHARS, []
    for v in reversed(views):  # newest get body budget first
        m = _item(v, budget)
        budget -= len(m["body"])
        items.append(m)
    out: Dict[str, Any] = {"thread_id": tid, "source": views[0].source, "messages": list(reversed(items))}
    if omitted:
        out["older_messages_omitted"] = omitted
    return out


def list_items(store: Store, args: Dict[str, Any]) -> Dict[str, Any]:
    order = _str(args, "order") or "newest"
    if order not in ("newest", "oldest"):
        raise ToolError("'order' must be 'newest' or 'oldest'")
    hits = store.list_items(source=_str(args, "source"), kind=_kind(args), sender=_str(args, "sender"),
                            after=_str(args, "after"), before=_str(args, "before"), limit=_limit(args, 20, 100),
                            oldest_first=order == "oldest")
    return {"results": [_hit(h) for h in hits], "count": len(hits)}


def list_messages(store: Store, args: Dict[str, Any]) -> Dict[str, Any]:
    return list_items(store, {**args, "source": GMAIL})


def list_sources(store: Store, args: Dict[str, Any]) -> Dict[str, Any]:
    out = []
    for src in store.source_ids():
        kept, _ = store.counts(src)
        account = store.get_state(src, "account")
        if not account and not kept:
            continue
        oldest, newest = store.date_range(src)
        last = store.get_state(src, "last_sync_ms")
        out.append({"source": src, "name": source_name(src), "connected": bool(account), "items": kept,
                    "auth_state": store.get_state(src, "auth_state") or "ok",
                    "last_sync": _iso_ms(last), "oldest": _iso_ms(oldest), "newest": _iso_ms(newest)})
    return {"sources": out}


def _iso_ms(ms: Any) -> Optional[str]:
    from .store import _iso
    return _iso(int(ms)) if ms not in (None, "") else None


_IMPL: Dict[str, Callable[[Store, Dict[str, Any]], Dict[str, Any]]] = {
    "search": search, "read": read, "list_items": list_items, "list_sources": list_sources,
    "search_mail": search_mail, "read_thread": read, "list_messages": list_messages}


def run_tool(store: Store, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Run a tool; errors come back as {"error": ...} so the model can recover."""
    if name not in _IMPL:
        return {"error": f"unknown tool '{name}'. Available: {sorted(_IMPL)}"}
    if not isinstance(args, dict):
        return {"error": "arguments must be a JSON object"}
    unknown = set(args) - set(next(t for t in TOOLS if t["name"] == name)["parameters"]["properties"])
    if unknown:
        return {"error": f"unknown argument(s): {sorted(unknown)}"}
    try:
        result = _IMPL[name](store, args)
    except ToolError as e:
        return {"error": str(e)}
    except ValueError as e:  # e.g. an unparseable date
        return {"error": f"invalid argument: {e}"}
    if name != "list_sources":
        source = GMAIL if name in ("search_mail", "list_messages") else args.get("source") or None
        notice = _notice(store, source if isinstance(source, str) else None)
        if notice:
            result["notice"] = notice
    return result

