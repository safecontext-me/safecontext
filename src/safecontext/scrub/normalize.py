"""Turn decoded message bodies into clean plain text before any rule runs.

Normalizing first closes easy evasions: zero-width characters splitting a
card number, full-width digits, non-breaking spaces, HTML entities.
"""
from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser
from typing import List, Tuple

_INVISIBLE = re.compile("[­᠎​-‏‪-‮⁠-⁤﻿]")
_MANY_BLANKS = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")
_TRAILING_WS = re.compile(r"[ \t]+\n")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _INVISIBLE.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    text = _TRAILING_WS.sub("\n", text)
    text = _MANY_BLANKS.sub("\n\n", text)
    return text.strip()


# Private-use sentinels marking quoted regions while we build text.
_Q_OPEN, _Q_CLOSE = "", ""

_VOID = {"br", "img", "hr", "meta", "link", "input", "wbr", "col", "area", "base", "source"}
_BLOCK = {
    "p", "div", "li", "tr", "table", "ul", "ol", "blockquote", "section", "article",
    "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "address",
}
_SKIP = {"script", "style", "head", "title", "noscript", "template"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.stack: List[Tuple[str, bool, bool]] = []  # (tag, skips_content, is_quote)
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "br":
            self.out.append("\n")
            return
        if tag == "hr":
            self.out.append("\n")
            return
        if tag in _VOID:
            return
        classes = " ".join((v or "") for k, v in attrs if k == "class").lower()
        skips = tag in _SKIP or "gmail_signature" in classes
        is_quote = tag == "blockquote" or "gmail_quote" in classes
        self.stack.append((tag, skips, is_quote))
        if skips:
            self.skip_depth += 1
        if tag in _BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("- ")
        if tag == "td":
            self.out.append(" ")
        if is_quote:
            self.out.append(_Q_OPEN)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _VOID:
            return
        if not any(t == tag for t, _, _ in self.stack):
            return  # stray end tag
        while self.stack:
            t, skips, is_quote = self.stack.pop()
            if skips:
                self.skip_depth -= 1
            if is_quote:
                self.out.append(_Q_CLOSE)
            if t in _BLOCK:
                self.out.append("\n")
            if t == tag:
                break

    def handle_data(self, data):
        if self.skip_depth == 0:
            self.out.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    raw = "".join(parser.out)
    # Collapse whitespace inside lines the way a browser would.
    raw = re.sub(r"[ \t ]+", " ", raw)

    lines_out: List[str] = []
    depth = 0
    for line in raw.split("\n"):
        quoted = depth > 0 or _Q_OPEN in line
        depth += line.count(_Q_OPEN) - line.count(_Q_CLOSE)
        depth = max(depth, 0)
        clean = line.replace(_Q_OPEN, "").replace(_Q_CLOSE, "").strip()
        if quoted and clean:
            clean = "> " + clean
        lines_out.append(clean)
    return "\n".join(lines_out)
