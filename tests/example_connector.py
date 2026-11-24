"""A complete example connector: chats from an exported JSON Lines file.

Copy this when you write a connector (see src/safecontext/sources/CONNECTORS.md).
A real one splits in two: the reader (plain parsing, no SafeContext types) goes
in src/safecontext/readers/, and the connector class in src/safecontext/sources/.

The export has one JSON object per line:
    {"id": "m1", "chat": "family", "from": "Alex", "text": "...", "ts": 1700000000000}
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from safecontext.errors import NeedsPermission
from safecontext.scrub import RawItem
from safecontext.sources.base import FILE, PermissionHelp, ReaderSource, Requirement
from safecontext.store import Store


# --- the reader: knows the file format, nothing about SafeContext -------------

@dataclass
class ExportLine:
    line_no: int
    id: str
    chat: str
    sender: str
    text: str
    ts_ms: int


def read_export(path: Path, after_line: int = 0) -> Iterator[ExportLine]:
    """Lines after `after_line` (1-based), oldest first. Skips lines it can't parse."""
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, start=1):
            if n <= after_line or not raw.strip():
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            yield ExportLine(n, str(d["id"]), str(d.get("chat") or "chat"), str(d.get("from") or ""),
                             str(d.get("text") or ""), int(d.get("ts") or 0))


# --- the connector: maps records to RawItems; the base class does the rest -----

EXPORT_FILE = "export_file"


class ChatExportSource(ReaderSource):
    id = "chat_export"
    name = "Chat export"
    description = "Chats from an exported JSON Lines file you choose, scrubbed before they're stored."
    kinds = ("chat",)
    api_version = 1
    requirements = (Requirement(FILE, "", "A file you choose",
                                "SafeContext reads only the export file you pick, read-only."),)
    account_label = "Export file"

    def __init__(self, store: Store, *, path: Path) -> None:
        super().__init__(store)
        self.path = Path(path)
        self.permission_help = {EXPORT_FILE: PermissionHelp(
            requirement=EXPORT_FILE, title="Export file",
            explain="SafeContext can't open the export file you picked.",
            steps=("Check the file still exists and is readable.", "Click Check again."),
            button="Choose file")}

    def check_access(self) -> None:
        try:
            with open(self.path, "rb"):
                pass
        except OSError:
            raise NeedsPermission("SafeContext can't read the export file.", EXPORT_FILE)

    def read(self, cursor: Optional[str]) -> Iterator[ExportLine]:
        return read_export(self.path, int(cursor or 0))

    def cursor_of(self, record: ExportLine) -> str:
        return str(record.line_no)

    def to_item(self, r: ExportLine) -> Optional[RawItem]:
        if not r.text.strip():
            return None  # nothing to keep
        return RawItem(id=r.id, kind="chat", thread_id=r.chat, date_ms=r.ts_ms, sender=r.sender,
                       participants=[r.sender], title=r.chat, text=r.text)
