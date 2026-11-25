"""A stand-in for the update server and the Mac tools the updater calls, for demo mode only.

The demo "installed app" is a folder of placeholder files in the demo data folder. Checking finds a
made-up next version with release notes; downloading writes a few KB slowly so the progress shows;
"verifying" copies the placeholder; nothing is swapped, spawned or opened, and no network is used."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import __version__
from .. import updater as upd

DEMO_NOTES = ("Updates now show up within minutes, and you choose when they install.\n"
              "Faster mail search.\nFixed the window jumping off the screen.")
_DMG = b"SafeContext demo disk image\n" * 2000


class _MetaStore:
    """Just the settings the updater keeps (get_meta / set_meta), in memory."""

    def __init__(self) -> None:
        self._d: Dict[str, str] = {}

    def get_meta(self, key: str) -> Optional[str]:
        return self._d.get(key)

    def set_meta(self, key: str, value: Optional[str]) -> None:
        if value is None:
            self._d.pop(key, None)
        else:
            self._d[key] = value


def next_version(v: str = __version__) -> str:
    parts = list(upd.parse_version(v))
    parts[-1] += 1
    return ".".join(map(str, parts))


def _make_app(path: Path, version: str) -> None:
    code = path / "Contents" / "Resources" / "app" / "safecontext"
    code.mkdir(parents=True, exist_ok=True)
    (path / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (code / "__init__.py").write_text(f'__version__ = "{version}"\n')


def demo_updater(data_dir: Path) -> upd.Updater:
    root = Path(data_dir) / "demo-update"
    app = root / "Applications" / upd.APP_NAME
    _make_app(app, __version__)
    version = next_version()
    manifest = json.dumps({"version": version, "url": "https://safecontext.me/download",
                           "sha256": hashlib.sha256(_DMG).hexdigest(), "size": len(_DMG),
                           "notes": DEMO_NOTES, "min_macos": "11.0"}).encode()

    def fetch(url: str, etag: Optional[str] = None):
        time.sleep(0.4)
        return (None, etag) if etag == '"demo"' else (manifest, '"demo"')

    def download(url: str, dest: Path, progress) -> None:
        step = len(_DMG) // 20
        with open(dest, "wb") as f:
            for i in range(0, len(_DMG), step):
                f.write(_DMG[i:i + step])
                progress(min(i + step, len(_DMG)), len(_DMG))
                time.sleep(0.15)

    def run(argv: List[str]) -> "subprocess.CompletedProcess[str]":
        tool = Path(argv[0]).name
        if tool == "hdiutil" and argv[1] == "attach":
            _make_app(Path(argv[argv.index("-mountpoint") + 1]) / upd.APP_NAME, version)
        elif tool == "ditto":
            shutil.copytree(argv[1], argv[2])
        return subprocess.CompletedProcess(argv, 0, "", "")

    def verify(path: Path, v: str) -> None:
        time.sleep(0.8)

    def spawn(plan: Any, pid: int, folder: Path, **kw: Any) -> None:
        pass  # the demo never replaces or reopens anything

    u = upd.Updater(_MetaStore(), fetch=fetch, download=download, run=run, app=app,
                    folder=root / "updates", verify=verify, spawn=spawn, auto_default=True)
    u.is_demo = True
    return u
