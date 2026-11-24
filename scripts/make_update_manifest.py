#!/usr/bin/env python3
"""Write update.json (the auto-update manifest) for a release DMG.

    .venv/bin/python scripts/make_update_manifest.py dist/SafeContext-0.1.5.dmg --notes "Faster Gmail sync."

It mounts the DMG, checks the app inside exactly the way the installed app will
(bundle ID, codesign, our Team ID, Gatekeeper/notarization, version), checks the
version matches pyproject.toml and the file name, and writes dist/update.json with
the DMG's SHA-256 and size. Nothing secret is involved: the trust comes from the
Developer ID signature that `briefcase package` already put on the app.

Upload the DMG first, then update.json (see packaging/README.md), so the manifest
never points at a download that isn't there yet.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from safecontext import updater as upd  # noqa: E402


def build(dmg: Path, notes: str, verify: bool = True) -> dict:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    m = re.search(r"SafeContext-(\d+(?:\.\d+)+)\.dmg$", dmg.name)
    if m and m.group(1) != version:
        raise SystemExit(f"{dmg.name} doesn't match pyproject.toml's version {version}.")
    with tempfile.TemporaryDirectory(prefix="safecontext-manifest-") as tmp:
        app = Path(tmp) / upd.APP_NAME
        upd.copy_app_from_dmg(dmg, app)
        found = upd.bundled_version(app)
        if found != version:
            raise SystemExit(f"The app in {dmg.name} is {found}, but pyproject.toml says {version}.")
        if verify:
            upd.verify_app(app, version)  # the same checks the installed app runs
    manifest = {
        "version": version,
        "url": upd.DOWNLOAD_PAGE,
        "sha256": upd.sha256_file(dmg),
        "size": dmg.stat().st_size,
        "notes": notes.strip(),
        "min_macos": "11.0",
    }
    upd.parse_manifest(json.dumps(manifest).encode())  # the app must accept what we publish
    return manifest


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dmg", type=Path)
    ap.add_argument("--notes", default="", help="one or two plain sentences shown in the update banner")
    ap.add_argument("--out", type=Path, default=ROOT / "dist" / "update.json")
    args = ap.parse_args(argv)
    try:
        manifest = build(args.dmg, args.notes)
    except upd.UpdateError as e:
        raise SystemExit(f"Not publishable: {e}")
    args.out.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
