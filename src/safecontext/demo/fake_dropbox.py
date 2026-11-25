"""In-memory Dropbox that behaves like the read API where sync cares.

A listing cursor returns only changes (deletions included). An expired cursor
raises CursorExpired. Revoked access and a single injected network failure
are there for the sync tests. Nothing here uploads, deletes, or shares.
"""
from __future__ import annotations

from collections import Counter
from typing import Dict, Optional

from safecontext.auth import NeedsReconnect
from safecontext.dropbox_api import CursorExpired, NotDownloadable
from safecontext.errors import TransientError


class FakeDropbox:
    def __init__(self, email: str = "alex@example.org", page_size: int = 50) -> None:
        self.email = email
        self.page_size = page_size
        self.files: Dict[str, dict] = {}
        self.content: Dict[str, bytes] = {}
        self.changed: Dict[str, int] = {}
        self.seq = 0
        self.oldest = 0
        self.revoked = False
        self.calls: Counter = Counter()
        self.downloaded = []
        self.fail_list_at: Optional[int] = None
        self._full_snapshot = []
        self._full_seq = 0

    def add(self, file_id: str, name: str, path: str, content: Optional[bytes] = None, *,
            tag: str = "file", size: Optional[int] = None, modified: str = "2026-01-02T03:04:05Z",
            downloadable: bool = True) -> None:
        self.seq += 1
        entry = {".tag": tag, "name": name, "path_display": path, "path_lower": path.lower(), "id": file_id,
                 "client_modified": modified, "server_modified": modified}
        if tag == "file":
            entry["is_downloadable"] = downloadable
            if content is None:
                entry["size"] = 0 if size is None else size
            else:
                self.content[file_id] = content
                entry["size"] = len(content) if size is None else size
        self.files[file_id] = entry
        self.changed[file_id] = self.seq

    def remove(self, file_id: str) -> None:
        self.seq += 1
        old = self.files.get(file_id, {"name": "gone", "path_display": "/"})
        self.files[file_id] = {".tag": "deleted", "name": old.get("name"), "path_display": old.get("path_display"),
                               "id": file_id}
        self.content.pop(file_id, None)
        self.changed[file_id] = self.seq

    def expire(self) -> None:
        self.oldest = self.seq + 1

    def _check(self) -> None:
        if self.revoked:
            raise NeedsReconnect("revoked_or_expired")

    def list_folder(self, cursor: Optional[str]) -> dict:
        self._check()
        self.calls["list"] += 1
        if self.fail_list_at == self.calls["list"]:
            raise TransientError("simulated network drop")
        if cursor is None:
            pool = [e for e in self.files.values() if e.get(".tag") != "deleted"]
            pool.sort(key=lambda e: e["id"])
            self._full_snapshot = pool
            self._full_seq = self.seq
            since, offset, mode = 0, 0, "full"
        else:
            try:
                _, since_s, offset_s, mode, _seq = cursor.split("|")
                since, offset = int(since_s), int(offset_s)
            except ValueError:
                raise CursorExpired() from None
            if since < self.oldest:
                raise CursorExpired()
            if mode == "full":
                pool = self._full_snapshot
            else:
                pool = [e for fid, e in self.files.items() if self.changed.get(fid, 0) > since]
                pool.sort(key=lambda e: e["id"])
        page = pool[offset:offset + self.page_size]
        next_offset = offset + len(page)
        has_more = next_offset < len(pool)
        if has_more:
            nxt = f"v|{since}|{next_offset}|{mode}|{self._full_seq if mode == 'full' else self.seq}"
        else:
            caught = self._full_seq if mode == "full" else self.seq
            nxt = f"v|{caught}|0|inc|0"
        return {"entries": [dict(e) for e in page], "cursor": nxt, "has_more": has_more}

    def download(self, file_id: str) -> bytes:
        self._check()
        self.calls["download"] += 1
        self.downloaded.append(file_id)
        if file_id not in self.content:
            raise NotDownloadable()
        return self.content[file_id]


def make_fake_dropbox(email: str = "alex@example.org") -> FakeDropbox:
    """A small made-up Dropbox for demo mode: notes to search, files kept as name and size,
    and a few sensitive files the scrubber drops."""
    box = FakeDropbox(email)
    box.add("id:demo-groceries", "Groceries.txt", "/Notes/Groceries.txt", b"Oat milk, eggs, sourdough, lemons.")
    box.add("id:demo-trip", "Lisbon trip.md", "/Notes/Lisbon trip.md",
            b"# Lisbon\nFlight TP 1351 on May 3. Hotel near Alfama. Call Sam about the tram tour.",
            modified="2026-10-14T09:30:00Z")
    box.add("id:demo-plan", "Q4 plan.md", "/Work/Q4 plan.md",
            b"Ship the new onboarding. Card on file for the vendor: 4242 4242 4242 4242.",
            modified="2026-11-02T16:00:00Z")
    box.add("id:demo-deck", "Roadmap.key", "/Work/Roadmap.key", size=8_400_000)
    box.add("id:demo-photo", "Beach.jpg", "/Photos/Beach.jpg", size=2_100_000)
    box.add("id:demo-tax", "W-2 2025.pdf", "/Taxes/W-2 2025.pdf", size=90_000)
    box.add("id:demo-bank", "bank_statement_2026-10.csv", "/Finance/bank_statement_2026-10.csv",
            b"date,amount\n2026-10-01,-12.00\n")
    box.add("id:demo-passport", "Passport scan.jpg", "/Scans/Passport scan.jpg", size=1_200_000)
    return box
