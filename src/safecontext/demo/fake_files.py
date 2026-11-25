"""Demo folders and PDFs for the Files screen: a made-up tree beside the demo data, never the real
home folder. Each PDF is a tiny valid one-page file with a line of made-up text."""
from __future__ import annotations

import os
import random
import time
from pathlib import Path
from typing import Callable, List, Tuple

# (folder, [PDFs inside it]) opted in as folders; the loose PDFs are opted in one by one.
_FOLDERS = [
    ("Documents/Taxes 2025", ["W-2 Acme Corp.pdf", "1099-INT Bank.pdf", "Property tax bill.pdf"]),
    ("Documents/Contracts", ["Lease agreement.pdf", "Freelance SOW - Northwind.pdf",
                             "Archive/NDA 2023.pdf"]),
]
_LOOSE = ["Downloads/Flight confirmation SFO-JFK.pdf", "Desktop/Insurance card.pdf"]
# In the tree but not opted in: what Add folder / Add PDF offers next in the demo.
_EXTRA_FOLDER = "Documents/Receipts"
_EXTRA_FOLDER_PDFS = ["Laptop receipt.pdf", "Dentist invoice.pdf"]
_EXTRA_PDF = "Downloads/Concert tickets.pdf"


def _pdf(text: str) -> bytes:
    """A minimal one-page PDF showing `text` (ASCII only)."""
    text = text.encode("ascii", "replace").decode().replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 14 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _write(root: Path, rel: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_bytes(_pdf(f"Demo document: {Path(rel).stem}"))
    return p


def demo_files(root: Path) -> Tuple[List[Tuple[str, str]], Callable[[str], List[str]]]:
    """Make the demo tree under `root`. Returns the folders and PDFs to opt in at start, as
    (kind, path), and a chooser for Add folder / Add PDF that offers the made-up extras."""
    root = Path(root)
    new = not root.exists()
    start: List[Tuple[str, str]] = []
    for folder, pdfs in _FOLDERS:
        for name in pdfs:
            _write(root, f"{folder}/{name}")
        start.append(("folder", str((root / folder).resolve())))
    for rel in _LOOSE:
        start.append(("file", str(_write(root, rel).resolve())))
    for name in _EXTRA_FOLDER_PDFS:
        _write(root, f"{_EXTRA_FOLDER}/{name}")
    extra_pdf = _write(root, _EXTRA_PDF)
    if new:  # made-up "last modified" dates, a few days apart, so the table has an order to show
        now = time.time()
        paths = sorted(root.rglob("*"), key=str)
        random.Random(7).shuffle(paths)  # a fixed, not-alphabetical order
        for i, p in enumerate(paths):
            t = now - (i * 2.7 + 0.3) * 86_400
            os.utime(p, (t, t))

    # Add folder / Add PDF hand out the made-up ones in turn, so an empty demo can be filled back up.
    folders = [p for k, p in start if k == "folder"] + [str((root / _EXTRA_FOLDER).resolve())]
    pdfs = [p for k, p in start if k == "file"] + [str(extra_pdf.resolve())]
    turn = {"folder": 0, "file": 0}

    def chooser(kind: str) -> List[str]:
        pool = folders if kind == "folder" else pdfs
        pick = pool[turn[kind] % len(pool)]
        turn[kind] += 1
        return [pick]

    return start, chooser
