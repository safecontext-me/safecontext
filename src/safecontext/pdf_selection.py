"""Folders and files the user opted in. PDF text may be read only from these.

A folder covers the PDFs inside it. A file covers that PDF alone. The two lists
are a union: removing a file does not hide it when a parent folder is still
opted in, and removing a folder leaves a PDF that was added on its own.

Nothing in this module opens a PDF. ``read_text`` is the only way to get text,
and it refuses every path that is not an opted-in PDF before it calls a reader.
Symlinks are not followed out of an opted-in folder. A symlink the user adds
themselves is stored as the real file or folder it points at.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .store import Store

META_KEY = "pdf_selection"
_KINDS = ("folder", "file")


class PdfSelectionError(Exception):
    """The user asked to opt in something that is not a folder or a PDF on this Mac."""


class PdfSelection:
    """The opted-in folders and PDFs, stored in the app database."""

    def __init__(self, store: Store, chooser: Optional[Callable[[str], Sequence[str]]] = None) -> None:
        self.store = store
        self._chooser = chooser or system_chooser

    def entries(self) -> List[Dict[str, str]]:
        data = self._load()
        return ([{"kind": "folder", "path": p} for p in data["folders"]]
                + [{"kind": "file", "path": p} for p in data["files"]])

    def add_folder(self, path: str) -> Dict[str, str]:
        resolved = self._existing(path)
        if not resolved.is_dir():
            raise PdfSelectionError("Choose a folder. SafeContext reads the PDFs inside it.")
        return self._add("folders", resolved)

    def add_file(self, path: str) -> Dict[str, str]:
        resolved = self._existing(path)
        if not _is_regular_pdf(resolved):
            raise PdfSelectionError("Choose a PDF. SafeContext reads text only from PDFs you add.")
        return self._add("files", resolved)

    def remove(self, path: str) -> None:
        """Drop a folder or file the user added. Unknown paths are left as they are."""
        data = self._load()
        keys = {self._stored_key(path)}
        raw = path if isinstance(path, str) else ""
        if raw:
            keys.add(raw)
        data["folders"] = [p for p in data["folders"] if p not in keys]
        data["files"] = [p for p in data["files"] if p not in keys]
        self._save(data)

    def pick(self, kind: str) -> List[Dict[str, str]]:
        """Open the chooser and opt in what the user picks. An empty result is a cancel."""
        if kind not in _KINDS:
            raise PdfSelectionError("Choose a folder or a PDF.")
        chosen = self._chooser(kind) or []
        if isinstance(chosen, str):
            chosen = [chosen]
        add = self.add_folder if kind == "folder" else self.add_file
        return [add(p) for p in chosen]

    def allows_text(self, path: str) -> bool:
        return self._allowed_pdf(path) is not None

    def readable_pdfs(self) -> List[str]:
        """PDFs whose text may be read. Names and paths only; the files are not opened."""
        data = self._load()
        found = set()
        for folder in data["folders"]:
            root = Path(folder)
            if root.is_dir() and not root.is_symlink():
                found.update(str(p) for p in _pdfs_under(root))
        for file in data["files"]:
            allowed = self._allowed_pdf(file)
            if allowed is not None and str(allowed) == file:
                found.add(file)
        return sorted(found)

    def table(self) -> List[Dict[str, Any]]:
        """The Files table: the folders added and every readable PDF, newest first by when each was last
        modified (a stat, the files aren't opened). `removable`: the user added it (a PDF found in an
        added folder goes when its folder does)."""
        data = self._load()
        rows: List[Dict[str, Any]] = []
        for folder in data["folders"]:
            rows.append({"kind": "folder", "path": folder, "removable": True, "modified_ms": _mtime_ms(folder)})
        added = set(data["files"])
        for pdf in self.readable_pdfs():
            rows.append({"kind": "file", "path": pdf, "removable": pdf in added, "modified_ms": _mtime_ms(pdf)})
        rows.sort(key=lambda r: (r["modified_ms"] or 0, r["path"]), reverse=True)
        return rows

    def read_text(self, path: str, reader: Callable[[Path], str]) -> str:
        """Read one opted-in PDF. ``reader`` is not called for any other path."""
        allowed = self._allowed_pdf(path)
        if allowed is None:
            raise PdfSelectionError("That PDF isn't one you added. SafeContext won't read it.")
        return reader(allowed)

    def _add(self, key: str, resolved: Path) -> Dict[str, str]:
        data = self._load()
        text = str(resolved)
        if text not in data[key]:
            data[key].append(text)
            self._save(data)
        kind = "folder" if key == "folders" else "file"
        return {"kind": kind, "path": text}

    def _allowed_pdf(self, path: str) -> Optional[Path]:
        resolved = self._resolve(path)
        if resolved is None or not _is_regular_pdf(resolved):
            return None
        data = self._load()
        if str(resolved) in data["files"]:
            return resolved
        for folder in data["folders"]:
            root = Path(folder)
            if root.is_dir() and not root.is_symlink() and _stays_inside(root, resolved):
                return resolved
        return None

    def _existing(self, path: str) -> Path:
        resolved = self._resolve(path)
        if resolved is None:
            raise PdfSelectionError("That folder or file isn't on this Mac.")
        return resolved

    def _resolve(self, path: str) -> Optional[Path]:
        if not isinstance(path, str) or not path.strip():
            return None
        try:
            return Path(path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return None

    def _stored_key(self, path: str) -> str:
        resolved = self._resolve(path) if isinstance(path, str) else None
        return str(resolved) if resolved is not None else ""

    def _load(self) -> Dict[str, List[str]]:
        raw = self.store.get_meta(META_KEY)
        empty: Dict[str, List[str]] = {"folders": [], "files": []}
        if not raw:
            return empty
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return empty
        if not isinstance(data, dict):
            return empty
        return {"folders": _strings(data.get("folders")), "files": _strings(data.get("files"))}

    def _save(self, data: Dict[str, List[str]]) -> None:
        self.store.set_meta(META_KEY, json.dumps(
            {"folders": data["folders"], "files": data["files"]}, ensure_ascii=False))


def system_chooser(kind: str) -> List[str]:
    """The Mac open panel. Tests pass their own chooser and never call this."""
    try:
        import webview
    except Exception as exc:
        raise PdfSelectionError("Open SafeContext to choose a folder or a PDF.") from exc
    return choose_with(webview, kind)


def choose_with(webview: Any, kind: str) -> List[str]:
    if kind not in _KINDS:
        raise PdfSelectionError("Choose a folder or a PDF.")
    windows = getattr(webview, "windows", None) or []
    if not windows:
        raise PdfSelectionError("Open SafeContext to choose a folder or a PDF.")
    dialog = webview.FileDialog.FOLDER if kind == "folder" else webview.FileDialog.OPEN
    kwargs: Dict[str, Any] = {"allow_multiple": True}
    if kind == "file":
        kwargs["file_types"] = ("PDF (*.pdf)",)
    chosen = windows[0].create_file_dialog(dialog, **kwargs)
    if not chosen:
        return []
    return [str(p) for p in chosen]


def _strings(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [p for p in value if isinstance(p, str) and p]


def _mtime_ms(path: str) -> Optional[int]:
    try:
        return int(os.stat(path).st_mtime * 1000)
    except OSError:
        return None


def _is_regular_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf" and path.is_file() and not path.is_symlink()


def _stays_inside(root: Path, resolved: Path) -> bool:
    """True when ``resolved`` is a PDF reached from ``root`` without a symlink."""
    root = root.resolve()
    if root.is_symlink() or not resolved.is_relative_to(root):
        return False
    cursor = root
    parts = resolved.relative_to(root).parts
    for index, part in enumerate(parts):
        cursor = cursor / part
        if cursor.is_symlink():
            return False
        if index < len(parts) - 1 and not cursor.is_dir():
            return False
    return _is_regular_pdf(cursor) and cursor.resolve() == resolved


def _pdfs_under(root: Path) -> List[Path]:
    root = root.resolve()
    if not root.is_dir() or root.is_symlink():
        return []
    found: List[Path] = []

    def skip(_err: OSError) -> None:
        return None

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=skip):
        current = Path(dirpath)
        try:
            if not current.resolve().is_relative_to(root):
                dirnames[:] = []
                continue
        except (OSError, RuntimeError):
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            child = current / name
            if child.is_symlink():
                continue
            try:
                resolved = child.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if _stays_inside(root, resolved):
                found.append(resolved)
    return found
