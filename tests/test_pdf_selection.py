"""Opt-in folders and files. Every path is a pytest fixture, never a real home file."""
import json
from pathlib import Path

import pytest

from safecontext.app.backend import Backend
from safecontext.pdf_selection import (PdfSelection, PdfSelectionError, choose_with,
                                       system_chooser)
from safecontext.store import Store


def write_pdf(path: Path, payload: bytes = b"fixture-pdf") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def under(paths, root: Path):
    root = root.resolve()
    for raw in paths:
        assert Path(raw).resolve().is_relative_to(root)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "mail.db")


@pytest.fixture
def sel(store):
    return PdfSelection(store)


def test_nothing_is_readable_until_the_user_opts_in(sel, tmp_path):
    pdf = write_pdf(tmp_path / "alone.pdf")
    assert sel.entries() == []
    assert sel.readable_pdfs() == []
    assert not sel.allows_text(str(pdf))

    def reader(_path):
        raise AssertionError("reader called")

    with pytest.raises(PdfSelectionError):
        sel.read_text(str(pdf), reader)


def test_folder_covers_pdfs_inside_it_and_nothing_else(sel, tmp_path):
    folder = tmp_path / "papers"
    nested = write_pdf(folder / "sub" / "nested.PDF")
    top = write_pdf(folder / "top.pdf")
    notes = folder / "notes.txt"
    notes.write_text("not a pdf")
    outside = write_pdf(tmp_path / "outside.pdf")
    decoy = tmp_path / "papers-extra"
    write_pdf(decoy / "decoy.pdf")
    hidden = write_pdf(folder / ".hidden.pdf")

    entry = sel.add_folder(str(folder))
    assert entry == {"kind": "folder", "path": str(folder.resolve())}
    pdfs = sel.readable_pdfs()
    under(pdfs, tmp_path)
    assert pdfs == sorted(str(p.resolve()) for p in (nested, top, hidden))
    assert sel.allows_text(str(nested))
    assert not sel.allows_text(str(notes))
    assert not sel.allows_text(str(outside))
    assert not sel.allows_text(str(decoy / "decoy.pdf"))
    assert not sel.allows_text(str(folder / ".." / "outside.pdf"))

    seen = []

    def reader(path):
        seen.append(path)
        return "fixture-text"

    assert sel.read_text(str(top), reader) == "fixture-text"
    assert seen == [top.resolve()]
    with pytest.raises(PdfSelectionError):
        sel.read_text(str(outside), reader)
    assert seen == [top.resolve()]


def test_symlinks_do_not_escape_an_opted_in_folder(sel, tmp_path):
    folder = tmp_path / "folder"
    keep = write_pdf(folder / "keep.pdf")
    outside = tmp_path / "outside"
    secret = write_pdf(outside / "secret.pdf")
    (folder / "alias").symlink_to(outside)
    (folder / "jump.pdf").symlink_to(secret)

    sel.add_folder(str(folder))
    assert sel.readable_pdfs() == [str(keep.resolve())]
    assert not sel.allows_text(str(secret))
    assert not sel.allows_text(str(folder / "jump.pdf"))

    def reader(_path):
        raise AssertionError("reader called")

    with pytest.raises(PdfSelectionError):
        sel.read_text(str(folder / "jump.pdf"), reader)


def test_a_symlink_the_user_adds_is_the_real_pdf(sel, tmp_path):
    target = write_pdf(tmp_path / "real" / "doc.pdf")
    link = tmp_path / "link.pdf"
    link.symlink_to(target)
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(tmp_path / "real")

    assert sel.add_file(str(link))["path"] == str(target.resolve())
    assert sel.add_folder(str(linked_dir))["path"] == str((tmp_path / "real").resolve())
    assert sel.allows_text(str(link))
    assert sel.allows_text(str(target))
    assert sel.read_text(str(link), lambda path: path.name) == "doc.pdf"


def test_an_individual_pdf_stays_when_its_folder_is_removed(sel, tmp_path):
    folder = tmp_path / "folder"
    inside = write_pdf(folder / "inside.pdf")
    own = write_pdf(tmp_path / "own.pdf")
    sel.add_folder(str(folder))
    sel.add_file(str(own))
    sel.add_file(str(inside))  # also covered by the folder; removing the file leaves the folder
    sel.remove(str(inside))
    assert sel.allows_text(str(inside))
    sel.remove(str(folder))
    assert not sel.allows_text(str(inside))
    assert sel.allows_text(str(own))
    assert sel.readable_pdfs() == [str(own.resolve())]


def test_rejects_missing_paths_and_non_pdfs(sel, tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("hello")
    with pytest.raises(PdfSelectionError):
        sel.add_folder(str(tmp_path / "missing"))
    with pytest.raises(PdfSelectionError):
        sel.add_file(str(notes))
    with pytest.raises(PdfSelectionError):
        sel.add_file(str(tmp_path))
    with pytest.raises(PdfSelectionError):
        sel.add_folder(str(notes))
    assert sel.entries() == []


def test_duplicate_add_is_one_entry_and_persists(store, tmp_path):
    pdf = write_pdf(tmp_path / "café.pdf")
    sel = PdfSelection(store)
    sel.add_file(str(pdf))
    sel.add_file(str(pdf))
    assert sel.entries() == [{"kind": "file", "path": str(pdf.resolve())}]
    again = PdfSelection(store)
    assert again.allows_text(str(pdf))
    saved = json.loads(store.get_meta("pdf_selection"))
    assert saved["files"] == [str(pdf.resolve())]


def test_corrupt_selection_reads_as_empty(store, tmp_path):
    pdf = write_pdf(tmp_path / "a.pdf")
    store.set_meta("pdf_selection", "{")
    sel = PdfSelection(store)
    assert sel.entries() == []
    assert not sel.allows_text(str(pdf))


def test_listing_does_not_open_pdfs(sel, tmp_path, monkeypatch):
    folder = tmp_path / "papers"
    pdf = write_pdf(folder / "a.pdf")
    sel.add_folder(str(folder))

    def boom(*_args, **_kwargs):
        raise AssertionError("opened a file")

    monkeypatch.setattr(Path, "read_bytes", boom)
    monkeypatch.setattr(Path, "read_text", boom)
    monkeypatch.setattr(Path, "open", boom)
    assert sel.readable_pdfs() == [str(pdf.resolve())]
    assert sel.allows_text(str(pdf))


def test_unreadable_subdirectory_is_skipped(sel, tmp_path):
    root = tmp_path / "root"
    blocked = root / "blocked"
    blocked.mkdir(parents=True)
    ok = write_pdf(root / "ok.pdf")
    write_pdf(blocked / "hidden.pdf")
    blocked.chmod(0)
    try:
        sel.add_folder(str(root))
        pdfs = sel.readable_pdfs()
    finally:
        blocked.chmod(0o700)
    assert pdfs == [str(ok.resolve())]


def test_pick_opts_in_only_what_the_chooser_returns(store, tmp_path):
    folder = tmp_path / "chosen"
    pdf = write_pdf(folder / "a.pdf")
    other = write_pdf(tmp_path / "other.pdf")
    calls = []

    def chooser(kind):
        calls.append(kind)
        return [str(folder)]

    sel = PdfSelection(store, chooser=chooser)
    assert sel.pick("folder")[0]["path"] == str(folder.resolve())
    assert calls == ["folder"]
    assert sel.allows_text(str(pdf))
    assert not sel.allows_text(str(other))
    with pytest.raises(PdfSelectionError):
        sel.pick("nope")


def test_choose_with_uses_the_open_panel_and_fails_closed_without_one(tmp_path):
    pdf = write_pdf(tmp_path / "a.pdf")

    class Window:
        def __init__(self):
            self.calls = []

        def create_file_dialog(self, dialog_type, allow_multiple=False, file_types=()):
            self.calls.append((int(dialog_type), allow_multiple, file_types))
            return (str(pdf),)

    class Webview:
        def __init__(self, windows):
            self.windows = windows
            self.FileDialog = type("FileDialog", (), {"FOLDER": 20, "OPEN": 10})

    window = Window()
    assert choose_with(Webview([window]), "file") == [str(pdf)]
    assert window.calls == [(10, True, ("PDF (*.pdf)",))]
    assert choose_with(Webview([window]), "folder") == [str(pdf)]
    assert window.calls[-1][0] == 20 and window.calls[-1][2] == ()
    with pytest.raises(PdfSelectionError):
        choose_with(Webview([]), "folder")


def test_cancelled_pick_adds_nothing(store):
    sel = PdfSelection(store, chooser=lambda kind: [])
    assert sel.pick("file") == []
    assert sel.entries() == []


def test_selection_code_does_not_scan_the_home_folder():
    src = Path(system_chooser.__code__.co_filename).read_text()
    # The chooser function lives in the module. Read the module, not a home path.
    assert "Path.home" not in src


@pytest.fixture
def backend(tmp_path):
    app = Backend(tmp_path / "app", demo=True, auto_sync=False)
    yield app
    app._shutdown()


def test_window_api_opts_in_fixture_paths_only(backend, tmp_path):
    folder = tmp_path / "papers"
    inside = write_pdf(folder / "a.pdf")
    own = write_pdf(tmp_path / "own.pdf")
    notes = tmp_path / "notes.txt"
    notes.write_text("hello")
    outside = write_pdf(tmp_path / "secret" / "secret.pdf")

    assert backend.pdf_selection()["pdfs"] == []
    added = backend.pdf_add_folder(str(folder))
    assert added["ok"] and added["entry"]["kind"] == "folder"
    own_added = backend.pdf_add_file(str(own))
    assert own_added["ok"]
    refused = backend.pdf_add_file(str(notes))
    assert refused["ok"] is False and "PDF" in refused["error"]
    pdfs = backend.pdf_selection()["pdfs"]
    under(pdfs, tmp_path)
    assert pdfs == sorted(str(p.resolve()) for p in (inside, own))
    assert str(outside.resolve()) not in pdfs

    backend._pdfs._chooser = lambda kind: [str(folder)]
    picked = backend.pdf_pick("folder")
    assert picked["ok"] and picked["entries"][0]["kind"] == "folder"
    removed = backend.pdf_remove(str(folder))
    assert removed["ok"] and removed["pdfs"] == [str(own.resolve())]


def test_pick_without_a_window_adds_nothing(backend, monkeypatch):
    def no_window(kind):
        raise PdfSelectionError("Open SafeContext to choose a folder or a PDF.")

    monkeypatch.setattr(backend._pdfs, "_chooser", no_window)
    result = backend.pdf_pick("folder")
    assert result["ok"] is False
    assert backend.pdf_selection()["entries"] == []


def test_demo_files_start_empty_and_add_made_up_ones(tmp_path):
    """Demo mode starts with nothing added (the empty state). Add folder / Add PDF add made-up folders and
    PDFs beside the demo data, never anything from the real home folder."""
    from safecontext.app.backend import Backend
    from safecontext.app.demo import demo_backend_kwargs
    b = Backend(**demo_backend_kwargs(tmp_path / "data"), auto_sync=False)
    try:
        assert b.pdf_selection()["table"] == []
        root = str((tmp_path / "data" / "demo-files").resolve())
        for _ in range(3):
            b.pdf_pick("folder")
            b.pdf_pick("file")
        sel = b.pdf_selection()
        kinds = [e["kind"] for e in sel["entries"]]
        assert kinds.count("folder") == 3 and kinds.count("file") == 3
        assert all(e["path"].startswith(root) for e in sel["entries"])
        assert len(sel["pdfs"]) >= 10 and all(p.startswith(root) and p.endswith(".pdf") for p in sel["pdfs"])
    finally:
        b._shutdown()
