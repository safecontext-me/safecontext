"""Strip quoted replies and signatures from plain text.

Quoted text duplicates other messages in the thread (which are scrubbed in
their own right), and signatures are mostly phone numbers and addresses.
Forwarded content is kept: when someone forwards a message, it *is* the
content.
"""
from __future__ import annotations

import re
from typing import List, Optional

_REPLY_HEADER = re.compile(
    r"^[ \t]*On\b[^\n]{0,300}(?:\n[^\n]{0,300})?\bwrote:[ \t]*$", re.M)
_ORIGINAL_MESSAGE = re.compile(
    r"^[ \t]*(?:-{2,}[ \t]*Original Message[ \t]*-{2,}|_{8,})[ \t]*$", re.M | re.I)
_OUTLOOK_HEADER = re.compile(
    r"^[ \t]*From:[ \t].+\n[ \t]*(?:Sent|Date):[ \t].+$", re.M)
_FORWARD_MARKER = re.compile(r"Forwarded message|Begin forwarded message", re.I)

_SIG_DELIM = re.compile(r"^-- ?$", re.M)
_MOBILE_FOOTER = re.compile(
    r"^[ \t]*(?:Sent from my \w[^\n]*|Sent from (?:Mail|Outlook|Yahoo Mail)[^\n]*|"
    r"Get Outlook for \w[^\n]*|Sent via [^\n]*|Sent from Gmail Mobile)[ \t]*$",
    re.M | re.I)
_VALEDICTION = re.compile(
    r"^[ \t]*(?:best|thanks|thank you|thanks again|many thanks|cheers|regards|best regards|"
    r"kind regards|warm regards|warmly|sincerely|all the best|talk soon|take care|xo+|love)[,!.]?[ \t]*$",
    re.I)


def _first_reply_cut(text: str) -> Optional[int]:
    cuts: List[int] = []
    for pat in (_REPLY_HEADER, _ORIGINAL_MESSAGE):
        m = pat.search(text)
        if m:
            cuts.append(m.start())
    for m in _OUTLOOK_HEADER.finditer(text):
        before = text[max(0, m.start() - 120):m.start()]
        if not _FORWARD_MARKER.search(before):
            cuts.append(m.start())
            break
    return min(cuts) if cuts else None


def strip_quoted(text: str) -> str:
    cut = _first_reply_cut(text)
    if cut is not None:
        text = text[:cut]
    lines = [ln for ln in text.split("\n") if not ln.lstrip().startswith(">")]
    return "\n".join(lines).rstrip()


def strip_signature(text: str) -> str:
    m = _SIG_DELIM.search(text)
    if m:
        text = text[:m.start()]
    text = _MOBILE_FOOTER.sub("", text)

    # Heuristic: "Best,\nName\nTitle\nCompany\nphone..." -> keep valediction + name.
    lines = text.rstrip().split("\n")
    for i in range(len(lines) - 1, max(-1, len(lines) - 10), -1):
        if _VALEDICTION.match(lines[i]):
            tail = lines[i + 1:]
            if len(tail) <= 8 and all(len(ln) <= 80 for ln in tail):
                name = [ln for ln in tail if ln.strip()][:1]
                lines = lines[: i + 1] + name
            break
    return "\n".join(lines).rstrip()


def strip_quotes_and_signature(text: str) -> str:
    return strip_signature(strip_quoted(text))
