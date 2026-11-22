"""The three mail tools, defined once and served to both Grok (function
calling) and MCP clients. Every result carries a citation: date, sender,
and a Gmail deep link. Everything returned comes from the scrubbed store.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from .auth import RECONNECT_MESSAGE
from .store import Hit, MessageView, Store

MAX_BODY_CHARS = 6000
MAX_THREAD_MESSAGES = 20
MAX_THREAD_CHARS = 40000

_DATE = {"type": "string", "description": "Date as YYYY-MM-DD (UTC) or full ISO 8601 timestamp."}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_mail",
        "description": (
            "Full-text search over the user's scrubbed Gmail. Sensitive data has been replaced with "
            "[REDACTED:TYPE] and some mail (security codes, banking, health, bulk) is excluded entirely. "
            "Returns matches with date, sender, subject, a snippet and a Gmail link."),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to search for. All words must match."},
                "sender": {"type": "string",
                           "description": "Optional sender filter: a full address, '@domain.com', or part of a name."},
                "after": {**_DATE, "description": "Only mail on or after this date. " + _DATE["description"]},
                "before": {**_DATE, "description": "Only mail before this date. " + _DATE["description"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_thread",
        "description": "Read every stored message in a thread, oldest first, with date, sender and Gmail link.",
        "parameters": {
            "type": "object",
            "properties": {"thread_id": {"type": "string", "description": "thread_id from a search or list result."}},
            "required": ["thread_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_messages",
        "description": "List messages newest first, filtered by sender and/or date range. No search words needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "sender": {"type": "string",
                           "description": "A full address, '@domain.com', or part of a sender name."},
                "after": {**_DATE, "description": "Only mail on or after this date. " + _DATE["description"]},
                "before": {**_DATE, "description": "Only mail before this date. " + _DATE["description"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = {t["name"] for t in TOOLS}


class ToolError(Exception):
    pass


def _hit(h: Hit) -> Dict[str, Any]:
    return {"id": h.id, "thread_id": h.thread_id, "date": h.citation.date, "sender": h.citation.sender,
            "subject": h.subject, "snippet": h.snippet, "gmail_link": h.citation.link}


def _message(v: MessageView, budget: int) -> Dict[str, Any]:
    body = v.body[:min(MAX_BODY_CHARS, max(budget, 0))]
    out = {"id": v.id, "date": v.citation.date, "sender": v.citation.sender, "recipients": v.recipients,
           "subject": v.subject, "body": body, "gmail_link": v.citation.link,
           "attachments": [{"filename": a.filename, "mime_type": a.mime_type, "size": a.size} for a in v.attachments]}
    if len(body) < len(v.body):
        out["body_truncated"] = True
    if v.redactions:
        out["redactions"] = v.redactions
    return out


def _notice(store: Store) -> str:
    if store.get_meta("auth_state") == "needs_reconnect":
        return RECONNECT_MESSAGE + " Results below may be out of date."
    if store.get_meta("history_id") is None and store.get_meta("account_email"):
        return "The first full sync is still running, so results may be incomplete."
    return ""


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


def search_mail(store: Store, args: Dict[str, Any]) -> Dict[str, Any]:
    hits = store.search(_str(args, "query", True), sender=_str(args, "sender"), after=_str(args, "after"),
                        before=_str(args, "before"), limit=_limit(args, 10, 50))
    return {"results": [_hit(h) for h in hits], "count": len(hits)}


def read_thread(store: Store, args: Dict[str, Any]) -> Dict[str, Any]:
    tid = _str(args, "thread_id", True)
    views = store.read_thread(tid)
    if not views:
        return {"thread_id": tid, "messages": [], "note": "No stored messages in this thread (it may have been "
                                                           "excluded by the scrubber, or the id is wrong)."}
    omitted = max(0, len(views) - MAX_THREAD_MESSAGES)
    views = views[-MAX_THREAD_MESSAGES:]
    budget, msgs = MAX_THREAD_CHARS, []
    for v in reversed(views):  # newest get body budget first
        m = _message(v, budget)
        budget -= len(m["body"])
        msgs.append(m)
    out: Dict[str, Any] = {"thread_id": tid, "messages": list(reversed(msgs))}
    if omitted:
        out["older_messages_omitted"] = omitted
    return out


def list_messages(store: Store, args: Dict[str, Any]) -> Dict[str, Any]:
    hits = store.list_messages(sender=_str(args, "sender"), after=_str(args, "after"),
                               before=_str(args, "before"), limit=_limit(args, 20, 100))
    return {"results": [_hit(h) for h in hits], "count": len(hits)}


_IMPL: Dict[str, Callable[[Store, Dict[str, Any]], Dict[str, Any]]] = {
    "search_mail": search_mail, "read_thread": read_thread, "list_messages": list_messages}


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
    notice = _notice(store)
    if notice:
        result["notice"] = notice
    return result


def citations(result: Dict[str, Any]) -> List[Dict[str, str]]:
    """Pull every cited message out of a tool result."""
    items = result.get("results") or result.get("messages") or []
    return [{"date": i["date"], "sender": i["sender"], "subject": i.get("subject", ""), "gmail_link": i["gmail_link"]}
            for i in items if "gmail_link" in i]
