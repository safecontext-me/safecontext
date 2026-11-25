"""Keep the installed SafeContext.app up to date.

How it works:

1. **Check.** A plain HTTPS GET of ``MANIFEST_URL``, a small JSON file published with
   each release (see ``scripts/make_update_manifest.py``)::

       {"version": "0.1.5", "url": "https://safecontext.me/download",
        "sha256": "…", "size": 53087522, "notes": "…", "min_macos": "11.0"}

   The request carries no cookies, no account and no device ID. It does send the app
   version (``?v=0.1.6``) and, after the first check, ``If-None-Match`` with the
   ETag of the manifest it last saw (a hash of that public file, kept in memory
   only), so an unchanged manifest costs a 304 and a few hundred bytes. The app
   checks shortly after launch, when the window comes forward (at most every
   ``FOCUS_MIN_GAP``), after the Mac wakes, and every ``CHECK_EVERY_SECONDS``,
   backing off after failures (``RETRY_SECONDS``). It only checks on its own as the
   installed app (never a dev checkout or demo mode). "Check for updates" in
   Settings checks on demand.

   Nothing is installed without the user: **Update and restart** installs now;
   **Install when I quit** (or the "Install updates when I quit" setting, off by
   default) downloads and verifies in the background and swaps the app after the
   user quits, without reopening it (the helper runs the new app once with
   ``--update-self-test`` instead, which proves it starts and exits). **Later**
   hides the offer until the next launch or ``LATER_SECONDS``; **Skip this version**
   hides that version until a newer one comes out.

2. **Download and verify.** The DMG is streamed to ``~/Library/Caches/SafeContext/updates``
   and its SHA-256 must match the manifest. The app inside is copied next to the
   installed one and must pass, before anything is replaced:
   ``codesign --verify --deep --strict``, a code requirement pinning Apple's
   Developer ID chain and our Team ID (``TEAM_ID``), Gatekeeper's notarization
   check (``spctl``), the bundle ID, and a version newer than the running one.
   The signature check is the real gate: a tampered manifest or download can't
   produce an app signed by our team.

3. **Swap and relaunch.** A small shell helper (``_HELPER``) waits for this process
   to quit, moves the old app aside, moves the new one into place and opens it.
   The new app writes a "launched" marker as it starts; if none appears in
   ``LAUNCH_TIMEOUT`` seconds the helper puts the old app back, reopens it, and
   the old app shows that the update was rolled back (and stops offering that
   version automatically).

The data folder (``~/Library/Application Support/SafeContext``) and the Keychain items
are outside the bundle, and the new app has the same signing identity, so both carry
over exactly as with a manual drag-and-replace.

When the app can't replace itself (not in a writable folder, run from the DMG, or
from a translocated copy in Downloads), the update button opens the download page.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import __version__, env

log = logging.getLogger("safecontext.updater")

MANIFEST_URL = "https://safecontext.me/update.json"
DOWNLOAD_PAGE = "https://safecontext.me/download"
ALLOWED_HOSTS = ("safecontext.me",)  # the manifest may only send us to our own site
TEAM_ID = "VYY267ZENY"
BUNDLE_ID = "me.safecontext.safecontext"
APP_NAME = "SafeContext.app"
# Apple-anchored Developer ID Application certificate issued to our team.
CODE_REQUIREMENT = ('anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] '
                    'and certificate leaf[field.1.2.840.113635.100.6.1.13] '
                    f'and certificate leaf[subject.OU] = "{TEAM_ID}"')

CHECK_EVERY_SECONDS = 2 * 3600  # while running; plus at launch, on focus and after wake
FIRST_CHECK_DELAY = 20
FOCUS_MIN_GAP = 15 * 60  # the window coming forward checks if the last check is older than this
WAKE_DELAY = 30  # after the Mac wakes, give the network a moment
WAKE_JUMP = 120  # the wall clock running this much ahead of the monotonic one means the Mac slept
TICK_SECONDS = 60  # how often the background loop looks at the clock
RETRY_SECONDS = (15 * 60, 30 * 60, 3600, 2 * 3600)  # after 1, 2, 3, 4+ failed checks in a row
LATER_SECONDS = 24 * 3600  # "Later" hides the offer this long (or until the next launch)
HELPER_FRESH_SECONDS = 300  # a helper started this recently may still be moving the staged app
MAX_MANIFEST_BYTES = 64 * 1024
MAX_DMG_BYTES = 1024 * 1024 * 1024
LAUNCH_TIMEOUT = 90
USER_AGENT = "SafeContext-Updater"  # no identifier; the version goes in ?v= (see manifest_url)

META_AUTO = "update_auto_check"
META_SKIP = "update_skip_version"
META_ON_QUIT = "update_install_on_quit"
SELF_TEST_ARG = "--update-self-test"

_VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(Exception):
    """A user-facing reason an update can't go ahead."""


# --- manifest -----------------------------------------------------------------------

def parse_version(v: str) -> Tuple[int, ...]:
    if not isinstance(v, str) or not _VERSION_RE.match(v.strip()):
        raise UpdateError(f"Not a version number: {v!r}")
    return tuple(int(x) for x in v.strip().split("."))


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


@dataclass(frozen=True)
class Manifest:
    version: str
    url: str
    sha256: str
    size: Optional[int] = None
    notes: str = ""
    min_macos: str = "11.0"


def parse_manifest(raw: bytes) -> Manifest:
    try:
        d = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UpdateError("The update information couldn't be read.")
    if not isinstance(d, dict):
        raise UpdateError("The update information couldn't be read.")
    version = str(d.get("version", ""))
    parse_version(version)
    url = str(d.get("url", ""))
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise UpdateError("The update points somewhere other than safecontext.me.")
    sha = str(d.get("sha256", "")).lower()
    if not _SHA_RE.match(sha):
        raise UpdateError("The update has no valid checksum.")
    size = d.get("size")
    if size is not None and (not isinstance(size, int) or not 0 < size <= MAX_DMG_BYTES):
        raise UpdateError("The update's size is not valid.")
    min_macos = str(d.get("min_macos") or "11.0")
    parse_version(min_macos)
    notes = str(d.get("notes") or "")[:2000]
    return Manifest(version=version, url=url, sha256=sha, size=size, notes=notes, min_macos=min_macos)


# --- network (replaced in tests) ------------------------------------------------------

def manifest_url(version: str = __version__) -> str:
    """MANIFEST_URL plus the app version, which the site counts per day and nothing else (see the
    privacy policy's "Update checks"). Anything that isn't a plain version number is left off."""
    return f"{MANIFEST_URL}?v={version}" if _VERSION_RE.match(version or "") else MANIFEST_URL


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})


def http_get(url: str, limit: int = MAX_MANIFEST_BYTES) -> bytes:
    with urllib.request.urlopen(_request(url), timeout=20) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("The update information is too large.")
    return data


def http_fetch_manifest(url: str, etag: Optional[str] = None) -> Tuple[Optional[bytes], Optional[str]]:
    """GET the manifest. Returns (body, etag), or (None, etag) when the server answers 304
    because `etag` still matches. The only extra header is If-None-Match with that public hash."""
    req = _request(url)
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read(MAX_MANIFEST_BYTES + 1)
            new_etag = r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, e.headers.get("ETag") or etag
        raise
    if len(data) > MAX_MANIFEST_BYTES:
        raise UpdateError("The update information is too large.")
    return data, new_etag


def http_download(url: str, dest: Path, progress: Callable[[int, Optional[int]], None]) -> None:
    with urllib.request.urlopen(_request(url), timeout=60) as r, open(dest, "wb") as f:
        total = r.headers.get("Content-Length")
        total_n = int(total) if total and total.isdigit() else None
        got = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            got += len(chunk)
            if got > MAX_DMG_BYTES:
                raise UpdateError("The download is larger than expected.")
            f.write(chunk)
            progress(got, total_n)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- where the app is -------------------------------------------------------------------

def installed_app(executable: Optional[str] = None) -> Optional[Path]:
    """The running .app bundle (…/SafeContext.app), or None outside the packaged app."""
    exe = Path(executable or sys.executable)
    if not env.is_app_launcher(str(exe)):
        return None
    app = exe.parent.parent.parent
    return app if app.suffix == ".app" else None


def why_cant_install(app: Optional[Path]) -> Optional[str]:
    """None if the app can replace itself in place, else the reason (the UI shows it)."""
    if app is None:
        return "Updates install into the SafeContext app, not a development copy."
    s = str(app)
    if "/AppTranslocation/" in s:
        return "Move SafeContext to your Applications folder first, then update."
    if s.startswith("/Volumes/"):
        return "SafeContext is running from the disk image. Drag it to Applications first."
    if not os.access(app.parent, os.W_OK) or not os.access(app, os.W_OK):
        return f"SafeContext can't write to {app.parent}, so it can't replace itself."
    return None


def updates_dir() -> Path:
    return Path.home() / "Library" / "Caches" / env.data_dir_name() / "updates"


def mark_launched(version: str = __version__, folder: Optional[Path] = None) -> None:
    """Called by the app as it opens: tells a waiting update helper the new version started."""
    folder = folder or updates_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"launched-{version}").write_text(str(int(time.time())))
    except OSError:
        log.warning("couldn't write the launch marker", exc_info=True)


# --- verifying the new app ------------------------------------------------------------------

Runner = Callable[[List[str]], "subprocess.CompletedProcess[str]"]


def run_cmd(argv: List[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, timeout=300)


def bundled_version(app: Path) -> Optional[str]:
    """The version in the app's own code (Contents/Resources/app/safecontext/__init__.py).

    Not Info.plist: Briefcase writes CFBundleShortVersionString only on `briefcase create`,
    so it goes stale across rebuilds (the 0.1.3 app's says 0.1.0). The code is sealed by
    the signature, so after verification this is what will actually run."""
    try:
        text = (app / "Contents" / "Resources" / "app" / "safecontext" / "__init__.py").read_text()
    except OSError:
        return None
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    return m.group(1) if m else None


def verify_app(app: Path, expected_version: str, run: Runner = run_cmd) -> None:
    """Raise UpdateError unless `app` is our signed, notarized app at `expected_version`."""
    try:
        with open(app / "Contents" / "Info.plist", "rb") as f:
            info = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        raise UpdateError("The downloaded app is incomplete.")
    if info.get("CFBundleIdentifier") != BUNDLE_ID:
        raise UpdateError("The downloaded app isn't SafeContext.")
    checks = [
        (["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
         "The downloaded app's signature is broken."),
        (["/usr/bin/codesign", "--verify", "-R=" + CODE_REQUIREMENT, str(app)],
         "The downloaded app isn't signed by SafeContext's developer."),
        (["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)],
         "macOS Gatekeeper rejected the downloaded app."),
    ]
    for argv, message in checks:
        r = run(argv)
        if r.returncode != 0:
            log.warning("%s failed: %s", argv[0], (r.stderr or "").strip()[:500])
            raise UpdateError(message)
    if bundled_version(app) != expected_version:  # read after the seal is checked
        raise UpdateError("The downloaded app's version doesn't match the update.")


def copy_app_from_dmg(dmg: Path, dest: Path, run: Runner = run_cmd) -> None:
    """Mount `dmg` read-only (no Finder window) and copy its SafeContext.app to `dest`."""
    mount = Path(tempfile.mkdtemp(prefix="safecontext-dmg-"))
    r = run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen",
             "-mountpoint", str(mount), str(dmg)])
    if r.returncode != 0:
        shutil.rmtree(mount, ignore_errors=True)
        raise UpdateError("The disk image couldn't be opened.")
    try:
        src = mount / APP_NAME
        if not src.is_dir():
            raise UpdateError("The disk image has no SafeContext app.")
        if dest.exists():
            shutil.rmtree(dest)
        if run(["/usr/bin/ditto", str(src), str(dest)]).returncode != 0:
            raise UpdateError("Couldn't copy the new app out of the disk image.")
    finally:
        run(["/usr/bin/hdiutil", "detach", "-force", str(mount)])
        shutil.rmtree(mount, ignore_errors=True)


# --- the swap helper ------------------------------------------------------------------------

# Runs after the app quits. Arguments: pid app staged backup failed marker rollback_note timeout version [mode].
# mode "restart" (the default) opens the new app. "quit" (Install when I quit) doesn't reopen anything:
# it runs the new app once, hidden, with --update-self-test, which writes the marker and exits.
_HELPER = r"""#!/bin/sh
PID="$1"; APP="$2"; NEW="$3"; OLD="$4"; FAILED="$5"; MARKER="$6"; NOTE="$7"; WAIT="$8"; VERSION="$9"
MODE="${10:-restart}"
OPEN="${SAFECONTEXT_OPEN:-/usr/bin/open}"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }
reopen() { if [ "$MODE" != quit ]; then "$OPEN" "$APP"; fi; }

i=0
while kill -0 "$PID" 2>/dev/null; do
  sleep 0.5; i=$((i+1))
  if [ "$i" -ge 240 ]; then log "app didn't quit; update cancelled"; rm -rf "$NEW"; exit 1; fi
done

rm -rf "$OLD" "$FAILED"
if ! mv "$APP" "$OLD"; then log "couldn't move the old app"; rm -rf "$NEW"; reopen; exit 1; fi
if ! mv "$NEW" "$APP"; then log "couldn't move the new app in; restoring"; mv "$OLD" "$APP"; reopen; exit 1; fi
rm -f "$MARKER" "$NOTE"
if [ "$MODE" = quit ]; then
  log "installed $VERSION; checking that it starts"
  "$OPEN" -g -j "$APP" --args --update-self-test
else
  log "installed $VERSION; opening it"
  "$OPEN" "$APP"
fi

i=0
while [ ! -f "$MARKER" ]; do
  sleep 1; i=$((i+1))
  if [ "$i" -ge "$WAIT" ]; then
    log "$VERSION didn't start within ${WAIT}s; rolling back"
    mv "$APP" "$FAILED" && mv "$OLD" "$APP"
    echo "$VERSION" > "$NOTE"
    reopen
    exit 1
  fi
done
rm -rf "$OLD"
log "update to $VERSION finished"
"""


@dataclass
class SwapPlan:
    app: Path
    staged: Path
    backup: Path
    failed: Path
    marker: Path
    note: Path
    version: str


def plan_swap(app: Path, version: str, folder: Path) -> SwapPlan:
    parent = app.parent  # same volume as the app, so the moves are atomic renames
    return SwapPlan(app=app, staged=parent / f".SafeContext-{version}.update.app",
                    backup=parent / ".SafeContext-previous.app", failed=parent / ".SafeContext-failed.app",
                    marker=folder / f"launched-{version}", note=folder / "rolled-back", version=version)


def spawn_helper(plan: SwapPlan, pid: int, folder: Path, timeout: int = LAUNCH_TIMEOUT,
                 extra_env: Optional[Dict[str, str]] = None, mode: str = "restart") -> subprocess.Popen:
    if mode not in ("restart", "quit"):
        raise ValueError(mode)
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "install-update.sh"
    script.write_text(_HELPER)
    logf = open(folder / "install.log", "a")
    try:
        # Also marks the log as fresh, so an app opened meanwhile leaves the staged copy alone.
        logf.write(time.strftime("%Y-%m-%d %H:%M:%S") + f" waiting for SafeContext to quit ({mode}, {plan.version})\n")
        logf.flush()
        return subprocess.Popen(
            ["/bin/sh", str(script), str(pid), str(plan.app), str(plan.staged), str(plan.backup),
             str(plan.failed), str(plan.marker), str(plan.note), str(timeout), plan.version, mode],
            stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True,
            env={**os.environ, **(extra_env or {})}, close_fds=True)
    finally:
        logf.close()


# --- the updater the app uses -------------------------------------------------------------

Fetch = Callable[[str, Optional[str]], Any]  # (url, etag) -> (body or None for 304, etag), or plain bytes


class Updater:
    """Thread-safe. Public methods return plain dicts for the window (see Backend.update_*).

    `now` is the wall clock and `mono` the monotonic one; tests pass fakes. The background loop
    calls tick() about once a minute (or at once when the window comes forward)."""

    def __init__(self, store: Any = None, *, current: str = __version__,
                 fetch: Fetch = http_fetch_manifest,
                 download: Callable[[str, Path, Callable[[int, Optional[int]], None]], None] = http_download,
                 run: Runner = run_cmd, app: Optional[Path] = "auto",  # type: ignore[assignment]
                 folder: Optional[Path] = None, verify: Callable[[Path, str], None] = None,  # type: ignore[assignment]
                 spawn: Callable[..., Any] = spawn_helper, now: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic, auto_default: Optional[bool] = None) -> None:
        self._store = store
        self.current = current
        self._fetch, self._download, self._run, self._spawn = fetch, download, run, spawn
        self._now, self._mono = now, mono
        self.app = installed_app() if app == "auto" else app
        self.folder = folder or updates_dir()
        self._verify = verify or (lambda path, version: verify_app(path, version, run))
        self._auto_default = (env.CURRENT == env.APP) if auto_default is None else auto_default
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._kick = threading.Event()  # the window came forward, or stop()
        self.manifest: Optional[Manifest] = None
        self.phase = "idle"  # idle | checking | downloading | verifying | ready | installing | error
        self.error: Optional[str] = None
        self.progress: Optional[float] = None
        self.checked_ms: Optional[int] = None
        self.rolled_back: Optional[str] = self._read_rollback_note()
        self._etag: Optional[str] = None  # memory only
        self._fails = 0  # failed checks in a row
        self._wake_at: Optional[float] = None
        self._clock: Optional[Tuple[float, float]] = None  # (wall, mono) at the last tick
        self._later: Optional[Tuple[str, float]] = None  # (version, until): memory only, so a relaunch ends it
        self._queued: Optional[str] = None  # the version the user chose "Install when I quit" for
        self._declined: Optional[str] = None  # cancelled "install when I quit" for this version: don't restage it
        self._stage_mode: Optional[str] = None  # "quit" or "restart" while a download is going or staged
        self._staged: Optional[SwapPlan] = None
        self._on_ready: Optional[Callable[[SwapPlan], None]] = None
        self._helper_started = False
        self._worker: Optional[threading.Thread] = None
        self.is_demo = False  # demo/fake_update.py: installing never quits the window
        self._clean_stale_staged()

    # settings
    @property
    def auto_check(self) -> bool:
        v = self._store.get_meta(META_AUTO) if self._store is not None else None
        return self._auto_default if v is None else v == "1"

    def set_auto_check(self, on: bool) -> None:
        if self._store is not None:
            self._store.set_meta(META_AUTO, "1" if on else "0")

    @property
    def install_on_quit(self) -> bool:
        """The "Install updates when I quit" setting. Off unless the user turns it on."""
        return self._store is not None and self._store.get_meta(META_ON_QUIT) == "1"

    def set_install_on_quit(self, on: bool) -> Dict[str, Any]:
        if self._store is not None:
            self._store.set_meta(META_ON_QUIT, "1" if on else "0")
        if on:
            self._declined = None
            self._maybe_stage_for_quit()
        elif self._queued is None:
            self._discard_staged()
        return self.status()

    def _skip(self) -> Optional[str]:
        v = self._store.get_meta(META_SKIP) if self._store is not None else None
        return v or None

    def skip(self) -> Dict[str, Any]:
        """Skip this version: no banner and no background install until a newer one comes out."""
        m = self.manifest
        if not m or not is_newer(m.version, self.current):
            raise UpdateError("There's no newer version to skip.")
        if self._store is not None:
            self._store.set_meta(META_SKIP, m.version)
        if self._queued == m.version or self._stage_mode == "quit":
            self._queued = None
            self._discard_staged()
        return self.status()

    def unskip(self) -> Dict[str, Any]:
        if self._store is not None:
            self._store.set_meta(META_SKIP, None)
        self._maybe_stage_for_quit()
        return self.status()

    def later(self) -> Dict[str, Any]:
        """Hide the offer until the next launch or LATER_SECONDS from now, whichever comes first."""
        m = self.manifest
        if m:
            self._later = (m.version, self._now() + LATER_SECONDS)
        return self.status()

    def _read_rollback_note(self) -> Optional[str]:
        note = self.folder / "rolled-back"
        try:
            v = note.read_text().strip()
        except OSError:
            return None
        try:
            note.unlink()
            parse_version(v)
        except (OSError, UpdateError):
            return None
        if self._store is not None:
            self._store.set_meta(META_SKIP, v)  # don't offer the broken version again on its own
        return v

    def _clean_stale_staged(self) -> None:
        """Remove staged copies an earlier run left next to the app (it quit without installing),
        unless a swap helper started in the last few minutes may still be moving one into place."""
        if self.app is None:
            return
        try:
            if self._now() - (self.folder / "install.log").stat().st_mtime < HELPER_FRESH_SECONDS:
                return
        except OSError:
            pass
        try:
            leftovers = list(self.app.parent.glob(".SafeContext-*.update.app"))
        except OSError:
            return
        for p in leftovers:
            shutil.rmtree(p, ignore_errors=True)

    # status
    def _offer(self) -> Optional[Manifest]:
        m = self.manifest
        return m if m and is_newer(m.version, self.current) else None

    def status(self) -> Dict[str, Any]:
        m = self._offer()
        skip = self._skip()
        later = bool(m and self._later and self._later[0] == m.version and self._now() < self._later[1])
        staging = self.phase in ("downloading", "verifying", "ready")
        on_quit = bool(m and staging and self._stage_mode == "quit")
        return {
            "current": self.current,
            "available": bool(m),
            "version": m.version if m else None,
            "notes": m.notes if m else "",
            "skipped": bool(m and skip == m.version),
            "skipped_version": skip if (skip and m and skip == m.version) else None,
            "later": later,
            "phase": self.phase,
            "progress": self.progress,
            "error": self.error,
            "checked_ms": self.checked_ms,
            "auto_check": self.auto_check,
            "install_on_quit": self.install_on_quit,
            "on_quit": on_quit,  # this version is downloading or staged, to install when the app quits
            "can_install": why_cant_install(self.app) is None,
            "cant_install_reason": why_cant_install(self.app),
            "download_page": DOWNLOAD_PAGE,
            "rolled_back": self.rolled_back,
        }

    # check
    def check(self, manual: bool = False) -> Dict[str, Any]:
        """Fetch the manifest. `manual` (the Check for updates button) also ends a "Later"."""
        if manual:
            self._later = None
        with self._lock:
            prev = self.phase
            if prev in ("downloading", "verifying", "installing"):
                return self.status()
            if prev != "ready":  # a staged update stays staged while we look for a newer one
                self.phase, self.error = "checking", None
        try:
            got = self._fetch(manifest_url(self.current), self._etag)
            body, etag = got if isinstance(got, tuple) else (got, None)
            if body is None and self.manifest is None:  # a 304 for a manifest we don't hold: ask again in full
                self._etag = None
                body, etag = self._fetch(manifest_url(self.current), None)
            m = parse_manifest(body) if body is not None else self.manifest
            with self._lock:
                self.manifest = m
                self._etag = etag
                if self.phase == "checking":
                    self.phase = "idle"
            self._fails = 0
        except UpdateError as e:
            self._fails += 1
            self._fail(str(e), keep_ready=prev == "ready")
        except Exception as e:  # network down, TLS, HTTP error: say so plainly
            log.info("update check failed: %s", e)
            self._fails += 1
            self._fail("Couldn't reach safecontext.me to check for updates.", keep_ready=prev == "ready")
        self.checked_ms = int(self._now() * 1000)
        self._after_check()
        return self.status()

    def _fail(self, message: str, keep_ready: bool = False) -> None:
        with self._lock:
            if keep_ready and self.phase == "ready":
                return  # a failed check doesn't undo a staged update; the next one tries again
            self.phase, self.error, self.progress = "error", message, None

    def _after_check(self) -> None:
        m = self._offer()
        if self._staged and (not m or self._staged.version != m.version):
            self._queued = None
            self._discard_staged()  # a newer release replaced the one we staged
        self._maybe_stage_for_quit()

    # scheduling
    def next_check_at(self) -> float:
        """Wall time of the next scheduled check (0 = now)."""
        if self.checked_ms is None:
            return 0.0
        gap = CHECK_EVERY_SECONDS if not self._fails else RETRY_SECONDS[min(self._fails, len(RETRY_SECONDS)) - 1]
        return self.checked_ms / 1000 + gap

    def on_focus(self) -> bool:
        """The window came forward. Ask the loop to check if the last check is old enough."""
        if not self.auto_check or self._stop.is_set():
            return False
        now = self._now()
        if self.checked_ms is not None and now - self.checked_ms / 1000 < FOCUS_MIN_GAP:
            return False
        if self._fails and now < self.next_check_at():
            return False
        self._kick.set()
        return True

    def tick(self, kicked: bool = False) -> bool:
        """One look at the clock. Checks if it's time, the window asked, or the Mac woke a moment ago."""
        wall, mono = self._now(), self._mono()
        if self._clock is not None:
            if (wall - self._clock[0]) - (mono - self._clock[1]) > WAKE_JUMP:
                self._wake_at = wall + WAKE_DELAY
        self._clock = (wall, mono)
        if not self.auto_check:
            return False
        due = kicked or wall >= self.next_check_at() or (self._wake_at is not None and wall >= self._wake_at)
        if not due:
            return False
        self._wake_at = None
        self.check()
        return True

    # download, verify, stage
    def start_install(self, on_ready: Callable[[SwapPlan], None]) -> Dict[str, Any]:
        """Install now: download and verify in the background (or use what's already staged),
        then call on_ready(plan), which starts the helper and quits."""
        return self._start(on_ready, "restart")

    def queue_for_quit(self) -> Dict[str, Any]:
        """Install when I quit: download and verify now, swap after the app quits."""
        m = self._offer()
        if m:
            self._queued, self._declined = m.version, None
        return self._start(None, "quit")

    def cancel_quit_install(self) -> Dict[str, Any]:
        m = self._offer()
        self._queued = None
        if m:
            self._declined = m.version
        self._discard_staged()
        return self.status()

    def _start(self, on_ready: Optional[Callable[[SwapPlan], None]], mode: str) -> Dict[str, Any]:
        reason = why_cant_install(self.app)
        if reason:
            raise UpdateError(reason)
        with self._lock:
            m = self._offer()
            if not m:
                raise UpdateError("There's no newer version to install.")
            if self.phase == "installing":
                return self.status()
            if self._worker and self._worker.is_alive():
                if on_ready is not None:
                    self._stage_mode = mode  # already downloading for later: install when it's done
                    self._on_ready = on_ready
                return self.status()
            self._stage_mode, self._on_ready = mode, on_ready
            if self.phase != "ready":
                self.phase, self.error, self.progress = "downloading", None, 0.0
            self._worker = threading.Thread(target=self._install, args=(m,), daemon=True, name="update-install")
            self._worker.start()
        return self.status()

    def _install(self, m: Manifest) -> None:
        try:
            plan = self._staged
            if not (plan and plan.version == m.version and plan.staged.exists()):
                plan = self.stage(m)
            with self._lock:
                on_ready = self._on_ready
                if on_ready is None:
                    if self._stage_mode == "quit":
                        self.phase = "ready"  # waits for on_quit()
                        return
                    # cancelled or skipped while it downloaded
                    self._staged, self.phase = None, "idle"
                    discard = plan
                else:
                    self.phase = "installing"
                    discard = None
            if discard is not None:
                shutil.rmtree(discard.staged, ignore_errors=True)
                return
            on_ready(plan)
        except UpdateError as e:
            self._stage_mode = None
            self._fail(str(e))
        except Exception as e:
            log.exception("update failed")
            self._stage_mode = None
            self._fail(f"The update couldn't be installed ({type(e).__name__}).")

    def _maybe_stage_for_quit(self) -> None:
        """With the setting on (or this version queued), get a new version ready in the background."""
        m = self._offer()
        if not m or self._skip() == m.version or self._declined == m.version:
            return
        if not (self.install_on_quit or self._queued == m.version):
            return
        if why_cant_install(self.app) or self.phase in ("downloading", "verifying", "ready", "installing"):
            return
        try:
            self._start(None, "quit")
        except UpdateError:
            pass

    def _discard_staged(self) -> None:
        with self._lock:
            if self.phase == "installing" or (self._worker and self._worker.is_alive()):
                if self._stage_mode == "quit":
                    self._stage_mode = None  # the download finishes, then nothing installs it
                    self._on_ready = None
                return
            plan, self._staged, self._stage_mode = self._staged, None, None
            if self.phase == "ready":
                self.phase = "idle"
        if plan is not None:
            shutil.rmtree(plan.staged, ignore_errors=True)

    def stage(self, m: Manifest) -> SwapPlan:
        """Download `m`, check it, and put the verified app next to the installed one."""
        assert self.app is not None
        self.folder.mkdir(parents=True, exist_ok=True)
        dmg = self.folder / f"SafeContext-{m.version}.dmg"
        plan = plan_swap(self.app, m.version, self.folder)
        try:
            def progress(got: int, total: Optional[int]) -> None:
                total = m.size or total
                self.progress = min(got / total, 1.0) if total else None

            self._download(m.url, dmg, progress)
            if m.size is not None and dmg.stat().st_size != m.size:
                raise UpdateError("The download was incomplete. Try again in a few minutes.")
            if sha256_file(dmg) != m.sha256:
                raise UpdateError("The download didn't match the published checksum. Try again in a few minutes.")
            with self._lock:
                self.phase, self.progress = "verifying", None
            copy_app_from_dmg(dmg, plan.staged, self._run)
            try:
                self._verify(plan.staged, m.version)
            except Exception:
                shutil.rmtree(plan.staged, ignore_errors=True)
                raise
        finally:
            dmg.unlink(missing_ok=True)
        self._staged = plan
        return plan

    def launch_helper(self, plan: SwapPlan, pid: Optional[int] = None) -> None:
        """Start the helper that swaps the app once this process (pid) has quit, then opens it."""
        with self._lock:
            if self._helper_started:
                return
            self._helper_started = True
        self._spawn(plan, pid or os.getpid(), self.folder)

    def on_quit(self, pid: Optional[int] = None) -> bool:
        """The app is quitting. If a verified version is staged for "install when I quit", start the
        helper: it swaps the app once this process is gone and doesn't reopen it. Safe to call twice."""
        with self._lock:
            plan = self._staged
            if (self._helper_started or self.phase != "ready" or self._stage_mode != "quit"
                    or plan is None or not plan.staged.exists()):
                return False
            if self._skip() == plan.version:
                return False
            self._helper_started = True
        self._spawn(plan, pid or os.getpid(), self.folder, mode="quit")
        return True

    # background
    def start_background(self, first_delay: float = FIRST_CHECK_DELAY) -> None:
        threading.Thread(target=self._loop, args=(first_delay,), daemon=True, name="update-check").start()

    def _loop(self, first_delay: float) -> None:
        if self._stop.wait(first_delay):
            return
        kicked = False
        while not self._stop.is_set():
            try:
                self.tick(kicked)
            except Exception:
                log.exception("update tick failed")
            kicked = self._kick.wait(TICK_SECONDS)
            self._kick.clear()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()


def run_self_test() -> int:
    """`SafeContext --update-self-test`: run by the swap helper after "Install when I quit" in place of
    reopening the app. Proves the new app's Python, the window toolkit and the app code load, tells the
    helper so, and exits without opening a window or touching the data folder."""
    import webview  # noqa: F401  (the window toolkit and its pyobjc wheels load)

    from .app import backend, main  # noqa: F401  (the app's own code imports)
    mark_launched()
    return 0
