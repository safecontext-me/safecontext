"""Auto-update: manifest, download checks, staging, the swap helper and the window calls.

Everything runs against fakes and temp folders. Nothing here touches the network,
/Applications, hdiutil, codesign or a real SafeContext.app.
"""
import hashlib
import json
import os
import plistlib
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from safecontext import updater as upd
from safecontext.app.backend import Backend
from safecontext.store import Store

DMG_BYTES = b"pretend this is a disk image" * 1000
SHA = hashlib.sha256(DMG_BYTES).hexdigest()


def manifest(**over):
    d = {"version": "9.0.0", "url": "https://safecontext.me/download", "sha256": SHA,
         "size": len(DMG_BYTES), "notes": "Faster sync.", "min_macos": "11.0"}
    d.update(over)
    return json.dumps(d).encode()


def make_app(path: Path, version: str, bundle_id: str = upd.BUNDLE_ID) -> Path:
    (path / "Contents" / "MacOS").mkdir(parents=True)
    with open(path / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version}, f)
    (path / "Contents" / "MacOS" / "SafeContext").write_text(f"app {version}")
    code = path / "Contents" / "Resources" / "app" / "safecontext"
    code.mkdir(parents=True)
    (code / "__init__.py").write_text(f'__version__ = "{version}"\n')
    with open(path / "Contents" / "Info.plist", "wb") as f:  # stale, like Briefcase leaves it
        plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": "0.1.0"}, f)
    return path


class FakeMac:
    """Stands in for hdiutil/ditto/codesign/spctl. The 'disk image' holds a prepared app."""

    def __init__(self, dmg_app: Path, fail=()):
        self.dmg_app = dmg_app
        self.fail = set(fail)
        self.calls = []
        self.mounted = None

    def __call__(self, argv):
        self.calls.append(argv)
        tool = Path(argv[0]).name
        rc = 0
        if tool == "hdiutil" and argv[1] == "attach":
            self.mounted = Path(argv[argv.index("-mountpoint") + 1])
            shutil.copytree(self.dmg_app, self.mounted / upd.APP_NAME)
        elif tool == "hdiutil" and argv[1] == "detach":
            shutil.rmtree(self.mounted / upd.APP_NAME, ignore_errors=True)
        elif tool == "ditto":
            shutil.copytree(argv[1], argv[2])
        if tool in self.fail or (tool == "codesign" and any(a.startswith("-R=") for a in argv)
                                 and "requirement" in self.fail):
            rc = 1
        return subprocess.CompletedProcess(argv, rc, "", "fake failure" if rc else "")


def fake_download(data=DMG_BYTES):
    def dl(url, dest, progress):
        assert url == "https://safecontext.me/download"
        dest.write_bytes(data)
        progress(len(data), len(data))
    return dl


@pytest.fixture
def env_(tmp_path):
    apps = tmp_path / "Applications"
    app = make_app(apps / upd.APP_NAME, "1.0.0")
    new = make_app(tmp_path / "dmg-contents" / upd.APP_NAME, "9.0.0")
    store = Store(tmp_path / "data" / "mail.db")
    return {"app": app, "new": new, "store": store, "folder": tmp_path / "updates"}


def make_updater(e, *, fetch=lambda url, etag=None: manifest(), download=None, run=None, verify=None, **kw):
    run = run or FakeMac(e["new"])
    return upd.Updater(e["store"], current="1.0.0", fetch=fetch, download=download or fake_download(),
                       run=run, app=e["app"], folder=e["folder"], verify=verify, **kw)


# --- versions and the manifest ---------------------------------------------------------

def test_versions():
    assert upd.is_newer("0.1.5", "0.1.4") and upd.is_newer("0.2", "0.1.9") and upd.is_newer("1.0.1", "1.0")
    assert not upd.is_newer("0.1.4", "0.1.4") and not upd.is_newer("0.1.4", "0.1.10")
    assert not upd.is_newer("1.0.0", "1.0")
    for bad in ("", "v1.0", "1", "1.0-beta", "1..2"):
        with pytest.raises(upd.UpdateError):
            upd.parse_version(bad)


def test_parse_manifest_ok():
    m = upd.parse_manifest(manifest())
    assert (m.version, m.sha256, m.size, m.notes) == ("9.0.0", SHA, len(DMG_BYTES), "Faster sync.")


@pytest.mark.parametrize("over", [
    {"url": "http://safecontext.me/download"},
    {"url": "https://evil.example/SafeContext.dmg"},
    {"url": "https://safecontext.me.evil.example/x"},
    {"sha256": "abc"},
    {"version": "latest"},
    {"size": -1},
    {"size": "big"},
])
def test_parse_manifest_rejects(over):
    with pytest.raises(upd.UpdateError):
        upd.parse_manifest(manifest(**over))


def test_parse_manifest_rejects_garbage():
    for raw in (b"not json", b"[]", b"\xff\xfe"):
        with pytest.raises(upd.UpdateError):
            upd.parse_manifest(raw)


def test_check_finds_update_and_sends_nothing_but_a_get(env_):
    urls = []
    u = make_updater(env_, fetch=lambda url, etag=None: urls.append(url) or manifest())
    s = u.check()
    assert urls == [upd.MANIFEST_URL + "?v=" + u.current]
    assert s["available"] and s["version"] == "9.0.0" and s["notes"] == "Faster sync."
    assert s["can_install"] and s["phase"] == "idle" and s["checked_ms"]
    req = upd._request(upd.MANIFEST_URL)
    assert req.get_method() == "GET" and req.data is None
    assert set(k.lower() for k in req.headers) == {"user-agent", "cache-control"}
    assert req.headers["User-agent"] == "SafeContext-Updater"  # no version or identifier


def test_check_up_to_date_and_offline(env_):
    u = make_updater(env_, fetch=lambda url, etag=None: manifest(version="1.0.0"))
    assert u.check()["available"] is False

    def offline(url, etag=None):
        raise OSError("no route to host")
    u = make_updater(env_, fetch=offline)
    s = u.check()
    assert s["phase"] == "error" and "Couldn't reach" in s["error"] and not s["available"]


def test_auto_check_defaults_off_outside_the_app_and_is_saved(env_):
    u = make_updater(env_)
    assert u.auto_check is False  # tests run from a checkout (dev)
    u.set_auto_check(True)
    assert make_updater(env_).auto_check is True
    assert make_updater(env_, auto_default=True).auto_check is True


# --- where the app can install -----------------------------------------------------------

def test_installed_app_from_launcher():
    assert upd.installed_app("/Applications/SafeContext.app/Contents/MacOS/SafeContext") == \
        Path("/Applications/SafeContext.app")
    assert upd.installed_app("/usr/local/bin/python3.12") is None


def test_why_cant_install(tmp_path):
    assert "development" in upd.why_cant_install(None)
    assert "Applications" in upd.why_cant_install(
        Path("/private/var/folders/x/AppTranslocation/ABC/d/SafeContext.app"))
    assert "disk image" in upd.why_cant_install(Path("/Volumes/SafeContext/SafeContext.app"))
    app = make_app(tmp_path / "ro" / upd.APP_NAME, "1.0.0")
    assert upd.why_cant_install(app) is None
    os.chmod(app.parent, stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert "can't write" in upd.why_cant_install(app)
    finally:
        os.chmod(app.parent, stat.S_IRWXU)


# --- download, verify, stage ------------------------------------------------------------------

def test_stage_happy_path(env_):
    fake = FakeMac(env_["new"])
    u = make_updater(env_, run=fake)
    u.check()
    plan = u.stage(u.manifest)
    assert plan.staged == env_["app"].parent / ".SafeContext-9.0.0.update.app"
    assert (plan.staged / "Contents" / "MacOS" / "SafeContext").read_text() == "app 9.0.0"
    assert (env_["app"] / "Contents" / "MacOS" / "SafeContext").read_text() == "app 1.0.0"  # untouched
    tools = [(Path(c[0]).name, c[1]) for c in fake.calls]
    assert tools[0] == ("hdiutil", "attach") and ("hdiutil", "detach") in tools
    attach = fake.calls[0]
    assert {"-nobrowse", "-readonly", "-noautoopen"} <= set(attach)
    req = [c for c in fake.calls if any(a.startswith("-R=") for a in c)][0]
    assert 'certificate leaf[subject.OU] = "VYY267ZENY"' in req[2] and "anchor apple generic" in req[2]
    assert any(Path(c[0]).name == "spctl" for c in fake.calls)
    assert not list(env_["folder"].glob("*.dmg"))  # the download is removed


@pytest.mark.parametrize("data,msg", [(DMG_BYTES[:-1], "incomplete"), (DMG_BYTES[::-1], "checksum")])
def test_stage_rejects_bad_download(env_, data, msg):
    fake = FakeMac(env_["new"])
    u = make_updater(env_, run=fake, download=fake_download(data))
    u.check()
    with pytest.raises(upd.UpdateError, match=msg):
        u.stage(u.manifest)
    assert fake.calls == []  # never mounted
    assert not (env_["app"].parent / ".SafeContext-9.0.0.update.app").exists()


@pytest.mark.parametrize("fail,msg", [("requirement", "signed by SafeContext"), ("spctl", "Gatekeeper"),
                                      ("codesign", "signature")])
def test_stage_rejects_unsigned_app(env_, fail, msg):
    u = make_updater(env_, run=FakeMac(env_["new"], fail={fail}))
    u.check()
    with pytest.raises(upd.UpdateError, match=msg):
        u.stage(u.manifest)
    assert not (env_["app"].parent / ".SafeContext-9.0.0.update.app").exists()


def test_verify_checks_bundle_id_and_version(tmp_path):
    ok = lambda argv: subprocess.CompletedProcess(argv, 0, "", "")  # noqa: E731
    with pytest.raises(upd.UpdateError, match="isn't SafeContext"):
        upd.verify_app(make_app(tmp_path / "a" / upd.APP_NAME, "9.0.0", "com.evil.app"), "9.0.0", ok)
    with pytest.raises(upd.UpdateError, match="version"):
        upd.verify_app(make_app(tmp_path / "b" / upd.APP_NAME, "8.0.0"), "9.0.0", ok)
    with pytest.raises(upd.UpdateError, match="incomplete"):
        upd.verify_app(tmp_path / "missing.app", "9.0.0", ok)
    upd.verify_app(make_app(tmp_path / "c" / upd.APP_NAME, "9.0.0"), "9.0.0", ok)


def test_bundled_version_ignores_stale_info_plist(tmp_path):
    assert upd.bundled_version(make_app(tmp_path / upd.APP_NAME, "0.1.3")) == "0.1.3"
    assert upd.bundled_version(tmp_path / "nothing.app") is None


def test_dmg_without_app(tmp_path):
    ok = lambda argv: subprocess.CompletedProcess(argv, 0, "", "")  # noqa: E731  (mounts an empty image)
    with pytest.raises(upd.UpdateError, match="no SafeContext app"):
        upd.copy_app_from_dmg(tmp_path / "x.dmg", tmp_path / "dest.app", ok)


# --- the swap helper (a real /bin/sh run, in a temp folder) --------------------------------------

def fake_open(tmp_path: Path, launches: bool) -> Path:
    """A stand-in for /usr/bin/open: records the call and, if `launches`, writes the marker
    the new app would write as it starts."""
    script = tmp_path / "fake-open.sh"
    script.write_text("#!/bin/sh\n"
                      f'echo "$*" >> "{tmp_path}/opened.txt"\n'
                      + (f'touch "{tmp_path}/updates/launched-9.0.0"\n' if launches else ""))
    script.chmod(0o755)
    return script


def run_helper(env_, tmp_path, launches, timeout=3):
    u = make_updater(env_)
    u.check()
    plan = u.stage(u.manifest)
    quitting = subprocess.Popen(["/bin/sleep", "0.5"])  # stands in for the app that's quitting
    helper = upd.spawn_helper(plan, quitting.pid, env_["folder"], timeout=timeout,
                              extra_env={"SAFECONTEXT_OPEN": str(fake_open(tmp_path, launches))})
    quitting.wait()
    assert helper.wait(timeout=30) == (0 if launches else 1)
    return plan


def test_helper_swaps_and_relaunches(env_, tmp_path):
    plan = run_helper(env_, tmp_path, launches=True)
    app = env_["app"]
    assert (app / "Contents" / "MacOS" / "SafeContext").read_text() == "app 9.0.0"
    assert not plan.staged.exists() and not plan.backup.exists()
    assert (tmp_path / "opened.txt").read_text().split() == [str(app)]
    assert "update to 9.0.0 finished" in (env_["folder"] / "install.log").read_text()


def test_helper_rolls_back_if_new_app_never_starts(env_, tmp_path):
    plan = run_helper(env_, tmp_path, launches=False, timeout=2)
    app = env_["app"]
    assert (app / "Contents" / "MacOS" / "SafeContext").read_text() == "app 1.0.0"
    assert (plan.failed / "Contents" / "MacOS" / "SafeContext").read_text() == "app 9.0.0"
    assert (tmp_path / "opened.txt").read_text().split() == [str(app), str(app)]
    assert (env_["folder"] / "rolled-back").read_text().strip() == "9.0.0"

    # The old app, opening again, says so and stops offering 9.0.0 on its own.
    u = make_updater(env_)
    assert u.rolled_back == "9.0.0" and not (env_["folder"] / "rolled-back").exists()
    s = u.check()
    assert s["available"] and s["skipped"] and s["rolled_back"] == "9.0.0"


def test_helper_gives_up_if_app_never_quits():
    # Not run for the full 2 minutes: check the script has the bail-out and leaves the old app alone.
    assert 'if [ "$i" -ge 240 ]; then log "app didn\'t quit; update cancelled"; rm -rf "$NEW"; exit 1; fi' in upd._HELPER


def test_mark_launched(tmp_path):
    upd.mark_launched("9.0.0", tmp_path / "u")
    assert (tmp_path / "u" / "launched-9.0.0").exists()


# --- the window's calls -------------------------------------------------------------------------

@pytest.fixture
def backend_with(tmp_path):
    made = []

    def make(updater):
        b = Backend(tmp_path / "data", demo=True, updater=updater, auto_sync=False)
        made.append(b)
        return b
    yield make
    for b in made:
        b._shutdown()


def test_demo_has_no_updater(tmp_path):
    b = Backend(tmp_path / "data", demo=True, auto_sync=False)
    try:
        assert b.update_status()["disabled"] is True
        assert b.update_check()["ok"] is False
        assert b.update_open_download() == {"ok": True, "opened": False}
    finally:
        b._shutdown()


def test_backend_check_and_install(env_, backend_with):
    spawned = []
    u = make_updater(env_, spawn=lambda plan, pid, folder: spawned.append((plan, pid)))
    b = backend_with(u)
    quit_called = []
    b._quit_fn = lambda: quit_called.append(True)
    assert b.update_check()["available"] is True
    r = b.update_install()
    assert r["ok"] and r["phase"] in ("downloading", "verifying", "installing")
    end = time.time() + 10
    while not quit_called and time.time() < end:
        time.sleep(0.02)
    assert quit_called and spawned[0][1] == os.getpid()
    assert spawned[0][0].staged.exists()
    assert b.update_status()["phase"] == "installing"


def test_backend_install_errors(env_, backend_with):
    u = make_updater(env_, fetch=lambda url, etag=None: manifest(version="1.0.0"))
    b = backend_with(u)
    b.update_check()
    assert b.update_install() == {"ok": False, "error": "There's no newer version to install."}
    u.app = None
    assert "development" in b.update_install()["error"]


def test_backend_failed_download_shows_error(env_, backend_with):
    u = make_updater(env_, download=fake_download(b"tampered"))
    b = backend_with(u)
    b._quit_fn = lambda: pytest.fail("must not quit")
    b.update_check()
    b.update_install()
    end = time.time() + 10
    while b.update_status()["phase"] != "error" and time.time() < end:
        time.sleep(0.02)
    s = b.update_status()
    assert s["phase"] == "error" and "Try again" in s["error"]
    assert (env_["app"] / "Contents" / "MacOS" / "SafeContext").read_text() == "app 1.0.0"


def test_backend_auto_toggle(env_, backend_with):
    b = backend_with(make_updater(env_))
    assert b.update_set_auto(True)["auto_check"] is True
    assert b.update_status()["auto_check"] is True
    assert b.update_set_auto(False)["auto_check"] is False


def test_real_backend_never_checks_from_a_checkout(tmp_path, monkeypatch):
    """A non-demo Backend gets an updater, but from a dev checkout it can't install and doesn't
    check on its own."""
    monkeypatch.setattr(upd, "http_get", lambda *a, **k: pytest.fail("network"))
    b = Backend(tmp_path / "data", auto_sync=False)
    try:
        s = b.update_status()
        assert s["can_install"] is False and s["auto_check"] is False
    finally:
        b._shutdown()


# --- the release script ---------------------------------------------------------------------------

def test_make_update_manifest(tmp_path, monkeypatch):
    import importlib.util
    import tomllib
    spec = importlib.util.spec_from_file_location(
        "make_update_manifest", Path(__file__).resolve().parents[1] / "scripts" / "make_update_manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    version = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]["version"]
    dmg = tmp_path / f"SafeContext-{version}.dmg"
    dmg.write_bytes(DMG_BYTES)
    monkeypatch.setattr(upd, "copy_app_from_dmg", lambda d, dest, run=None: make_app(dest, version))
    checked = []
    monkeypatch.setattr(upd, "verify_app", lambda app, v, run=None: checked.append(v))
    out = tmp_path / "update.json"
    mod.main([str(dmg), "--notes", "  Faster sync. ", "--out", str(out)])
    m = upd.parse_manifest(out.read_bytes())
    assert (m.version, m.sha256, m.size, m.notes, m.url) == (version, SHA, len(DMG_BYTES), "Faster sync.",
                                                             "https://safecontext.me/download")
    assert checked == [version]
    with pytest.raises(SystemExit, match="doesn't match"):
        mod.main([str(tmp_path / "SafeContext-0.0.1.dmg")])


def test_manifest_url_sends_only_a_plain_version():
    assert upd.manifest_url("0.1.6") == "https://safecontext.me/update.json?v=0.1.6"
    for bad in ("", "0.1.6&id=abc", "dev", "0.1.6-rc1"):
        assert upd.manifest_url(bad) == upd.MANIFEST_URL


# --- faster checks, user's choice (schedule, ETag, Later, Skip, install when I quit) ------------------

class Clock:
    """Fake wall and monotonic clocks. advance() moves both; sleep() moves only the wall clock,
    as a Mac's sleep does to time.monotonic()."""

    def __init__(self, t=1_800_000_000.0):
        self.wall, self.mono = t, 1000.0

    def now(self):
        return self.wall

    def mon(self):
        return self.mono

    def advance(self, s):
        self.wall += s
        self.mono += s

    def sleep(self, s):
        self.wall += s
        self.mono += 1


class Server:
    """A fake safecontext.me: serves `body` with an ETag and answers 304 when it still matches."""

    def __init__(self, body=None):
        self.body = body or manifest()
        self.calls = []
        self.down = False

    @property
    def etag(self):
        return '"' + hashlib.sha256(self.body).hexdigest()[:16] + '"'

    def __call__(self, url, etag=None):
        self.calls.append((url, etag))
        if self.down:
            raise OSError("offline")
        if etag == self.etag:
            return None, self.etag
        return self.body, self.etag


def wait_for(cond, timeout=10):
    end = time.time() + timeout
    while not cond() and time.time() < end:
        time.sleep(0.02)
    assert cond()


def timed_updater(env_, server=None, clock=None, **kw):
    clock = clock or Clock()
    server = server or Server()
    u = make_updater(env_, fetch=server, now=clock.now, mono=clock.mon, auto_default=True, **kw)
    return u, server, clock


def test_etag_makes_repeat_checks_a_304(env_):
    u, server, _ = timed_updater(env_)
    assert u.check()["version"] == "9.0.0"
    assert server.calls[0] == ("https://safecontext.me/update.json?v=1.0.0", None)
    s = u.check()
    assert server.calls[1][1] == server.etag  # asked "changed since?"
    assert s["available"] and s["version"] == "9.0.0" and s["phase"] == "idle"  # 304 kept what we had
    server.body = manifest(version="9.1.0")
    assert u.check()["version"] == "9.1.0"


def test_a_304_without_a_manifest_asks_again_in_full(env_):
    answers = [(None, '"x"'), (manifest(), '"y"')]
    seen = []

    def fetch(url, etag=None):
        seen.append(etag)
        return answers.pop(0)
    u = make_updater(env_, fetch=fetch)
    u._etag = '"x"'
    assert u.check()["version"] == "9.0.0" and seen == ['"x"', None]


def test_http_fetch_manifest_sends_only_the_version_and_the_etag(monkeypatch):
    import email.message
    import urllib.error
    sent = []

    class Resp:
        headers = {"ETag": '"abc"'}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return manifest()

    def urlopen(req, timeout):
        sent.append(req)
        if req.get_header("If-none-match") == '"abc"':
            h = email.message.Message()
            h["ETag"] = '"abc"'
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", h, None)
        return Resp()
    monkeypatch.setattr(upd.urllib.request, "urlopen", urlopen)
    body, etag = upd.http_fetch_manifest("https://safecontext.me/update.json?v=1.0.0")
    assert body == manifest() and etag == '"abc"'
    assert upd.http_fetch_manifest("https://safecontext.me/update.json?v=1.0.0", '"abc"') == (None, '"abc"')
    first, second = sent
    assert first.full_url == "https://safecontext.me/update.json?v=1.0.0"
    assert dict(first.header_items()) == {"User-agent": "SafeContext-Updater", "Cache-control": "no-cache"}
    assert dict(second.header_items()) == {"User-agent": "SafeContext-Updater", "Cache-control": "no-cache",
                                           "If-none-match": '"abc"'}


def test_checks_every_two_hours(env_):
    u, server, clock = timed_updater(env_)
    assert u.tick() is True  # first tick: never checked
    clock.advance(upd.TICK_SECONDS)
    assert u.tick() is False
    clock.advance(upd.CHECK_EVERY_SECONDS - 2 * upd.TICK_SECONDS)
    assert u.tick() is False
    clock.advance(2 * upd.TICK_SECONDS)
    assert u.tick() is True
    assert len(server.calls) == 2


def test_failed_checks_back_off(env_):
    u, server, clock = timed_updater(env_)
    server.down = True
    assert u.tick() and u.status()["phase"] == "error"
    for gap in upd.RETRY_SECONDS + (upd.RETRY_SECONDS[-1],):
        clock.advance(gap - 1)
        assert u.tick() is False
        clock.advance(1)
        assert u.tick() is True
    server.down = False
    clock.advance(upd.RETRY_SECONDS[-1])
    assert u.tick() and u.status()["phase"] == "idle"
    assert u.next_check_at() == clock.now() + upd.CHECK_EVERY_SECONDS  # back to every two hours


def test_focus_checks_after_fifteen_minutes(env_):
    u, server, clock = timed_updater(env_)
    assert u.on_focus() is True  # never checked
    assert u.tick(kicked=u._kick.is_set())
    u._kick.clear()
    clock.advance(upd.FOCUS_MIN_GAP - 1)
    assert u.on_focus() is False and not u._kick.is_set()
    clock.advance(1)
    assert u.on_focus() is True and u._kick.is_set()
    u.set_auto_check(False)
    u._kick.clear()
    assert u.on_focus() is False


def test_focus_respects_the_failure_backoff(env_):
    u, server, clock = timed_updater(env_)
    server.down = True
    u.check()
    clock.advance(upd.FOCUS_MIN_GAP)  # == RETRY_SECONDS[0]: due again
    assert u.on_focus() is True
    u.check()  # second failure: next try in 30 min
    clock.advance(upd.FOCUS_MIN_GAP)
    assert u.on_focus() is False


def test_checks_shortly_after_the_mac_wakes(env_):
    u, server, clock = timed_updater(env_)
    u.tick()
    clock.advance(20 * 60)
    assert u.tick() is False
    clock.sleep(40 * 60)  # an hour since the check: not due on the clock, but the Mac slept
    assert u.tick() is False  # gives the network WAKE_DELAY
    clock.advance(upd.WAKE_DELAY)
    assert u.tick() is True
    clock.advance(upd.TICK_SECONDS)
    assert u.tick() is False


def test_nothing_checks_on_its_own_with_auto_check_off(env_):
    u, server, clock = timed_updater(env_)
    u.set_auto_check(False)
    assert u.tick() is False and u.tick(kicked=True) is False and server.calls == []
    assert u.check(manual=True)["available"]  # the button still works


def test_background_loop_wakes_on_focus(env_):
    u, server, clock = timed_updater(env_)
    u.start_background(first_delay=0)
    try:
        wait_for(lambda: len(server.calls) == 1)
        clock.advance(upd.FOCUS_MIN_GAP)
        assert u.on_focus()
        wait_for(lambda: len(server.calls) == 2)
    finally:
        u.stop()


def test_later_hides_until_tomorrow_or_a_newer_version(env_):
    u, server, clock = timed_updater(env_)
    u.check()
    assert u.later()["later"] is True
    clock.advance(upd.LATER_SECONDS - 1)
    assert u.status()["later"] is True
    clock.advance(1)
    assert u.status()["later"] is False
    u.later()
    server.body = manifest(version="9.1.0")
    u.check()
    assert u.status()["later"] is False  # a newer release shows again
    u.later()
    assert u.check(manual=True)["later"] is False  # so does clicking Check for updates
    # and a relaunch: Later lives in memory only
    u.later()
    assert make_updater(env_).check()["later"] is False


def test_skip_and_undo(env_):
    u, server, _ = timed_updater(env_)
    u.check()
    s = u.skip()
    assert s["skipped"] and s["skipped_version"] == "9.0.0"
    assert make_updater(env_).check()["skipped"] is True  # saved
    s = u.unskip()
    assert not s["skipped"] and s["skipped_version"] is None
    server.body = manifest(version="1.0.0")
    u.check()
    with pytest.raises(upd.UpdateError):
        u.skip()


def queue_and_wait(u):
    u.queue_for_quit()
    wait_for(lambda: u.status()["phase"] in ("ready", "error"))
    return u.status()


def test_install_when_i_quit(env_):
    spawned = []
    u, server, _ = timed_updater(env_, spawn=lambda plan, pid, folder, **kw: spawned.append((plan, pid, kw)))
    u.check()
    s = queue_and_wait(u)
    assert s["phase"] == "ready" and s["on_quit"] is True
    staged = u._staged.staged
    assert staged.exists()
    assert (env_["app"] / "Contents" / "MacOS" / "SafeContext").read_text() == "app 1.0.0"  # not yet
    assert u.on_quit(pid=4242) is True
    assert spawned == [(u._staged, 4242, {"mode": "quit"})]
    assert u.on_quit() is False  # closing + finally both call it: one helper
    u.launch_helper(u._staged)
    assert len(spawned) == 1


def test_on_quit_does_nothing_unless_queued(env_):
    spawned = []
    u, _, _ = timed_updater(env_, spawn=lambda *a, **k: spawned.append(a))
    u.check()
    assert u.status()["phase"] == "idle" and u.on_quit() is False and spawned == []


def test_cancel_install_when_i_quit(env_):
    u, _, _ = timed_updater(env_, spawn=lambda *a, **k: pytest.fail("must not install"))
    u.check()
    queue_and_wait(u)
    staged = u._staged.staged
    s = u.cancel_quit_install()
    assert s["phase"] == "idle" and not s["on_quit"] and not staged.exists()
    assert u.on_quit() is False


def test_skip_drops_a_staged_update(env_):
    u, _, _ = timed_updater(env_, spawn=lambda *a, **k: pytest.fail("must not install"))
    u.check()
    queue_and_wait(u)
    staged = u._staged.staged
    u.skip()
    assert not staged.exists() and u.status()["phase"] == "idle" and u.on_quit() is False


def test_update_and_restart_reuses_the_staged_copy(env_):
    downloads = []
    dl = fake_download()
    u, _, _ = timed_updater(env_, download=lambda *a: (downloads.append(1), dl(*a)))
    u.check()
    queue_and_wait(u)
    ready = []
    u.start_install(ready.append)
    wait_for(lambda: ready)
    assert len(downloads) == 1 and ready[0].version == "9.0.0" and u.status()["phase"] == "installing"


def test_setting_off_by_default_then_stages_in_the_background(env_):
    u, server, _ = timed_updater(env_)
    assert u.status()["install_on_quit"] is False
    u.check()
    assert u.status()["phase"] == "idle"  # off: nothing downloads on its own
    u.set_install_on_quit(True)
    wait_for(lambda: u.status()["phase"] == "ready")
    assert u.status()["on_quit"] is True
    staged = u._staged.staged
    u.set_install_on_quit(False)
    assert u.status()["phase"] == "idle" and not staged.exists()
    # On at launch: the first check stages it.
    u2, _, _ = timed_updater(env_)
    u2.set_install_on_quit(True)
    u2.check()
    wait_for(lambda: u2.status()["phase"] == "ready")


def test_setting_skips_skipped_and_declined_versions(env_):
    u, _, _ = timed_updater(env_)
    u.set_install_on_quit(True)
    u.check()
    wait_for(lambda: u.status()["phase"] == "ready")
    u.cancel_quit_install()
    u.check()
    assert u.status()["phase"] == "idle"  # declined this one for this session
    u.skip()
    u2, _, _ = timed_updater(env_)
    u2.check()
    assert u2.status()["phase"] == "idle"


def test_a_newer_release_replaces_the_staged_one(env_):
    u, server, _ = timed_updater(env_)
    u.check()
    queue_and_wait(u)
    old = u._staged.staged
    server.body = manifest(version="9.1.0")
    s = u.check()  # checks still run while an update is staged
    assert s["version"] == "9.1.0" and not old.exists()
    assert s["phase"] == "idle" and u.on_quit() is False  # the user chose 9.0.0, not 9.1.0: ask again


def test_a_failed_check_keeps_the_staged_update(env_):
    u, server, _ = timed_updater(env_)
    u.check()
    queue_and_wait(u)
    server.down = True
    s = u.check()
    assert s["phase"] == "ready" and s["on_quit"]


def test_leftover_staged_copies_are_removed_at_launch(env_):
    leftover = env_["app"].parent / ".SafeContext-8.0.0.update.app"
    leftover.mkdir()
    make_updater(env_)
    assert not leftover.exists()


def test_leftover_kept_while_a_helper_may_be_running(env_):
    leftover = env_["app"].parent / ".SafeContext-8.0.0.update.app"
    leftover.mkdir()
    env_["folder"].mkdir(parents=True)
    (env_["folder"] / "install.log").write_text("waiting\n")
    make_updater(env_)
    assert leftover.exists()


def run_quit_helper(env_, tmp_path, launches, timeout=3):
    u = make_updater(env_)
    u.check()
    plan = u.stage(u.manifest)
    quitting = subprocess.Popen(["/bin/sleep", "0.5"])
    helper = upd.spawn_helper(plan, quitting.pid, env_["folder"], timeout=timeout, mode="quit",
                              extra_env={"SAFECONTEXT_OPEN": str(fake_open(tmp_path, launches))})
    quitting.wait()
    assert helper.wait(timeout=30) == (0 if launches else 1)
    return plan


def test_quit_helper_swaps_and_only_self_tests(env_, tmp_path):
    plan = run_quit_helper(env_, tmp_path, launches=True)
    app = env_["app"]
    assert (app / "Contents" / "MacOS" / "SafeContext").read_text() == "app 9.0.0"
    assert not plan.backup.exists()
    assert (tmp_path / "opened.txt").read_text().splitlines() == [f"-g -j {app} --args --update-self-test"]
    assert "(quit, 9.0.0)" in (env_["folder"] / "install.log").read_text()


def test_quit_helper_rolls_back_without_reopening(env_, tmp_path):
    plan = run_quit_helper(env_, tmp_path, launches=False, timeout=2)
    app = env_["app"]
    assert (app / "Contents" / "MacOS" / "SafeContext").read_text() == "app 1.0.0"
    assert (tmp_path / "opened.txt").read_text().splitlines() == [f"-g -j {app} --args --update-self-test"]
    assert (env_["folder"] / "rolled-back").read_text().strip() == "9.0.0"


def test_spawn_helper_rejects_unknown_modes(env_, tmp_path):
    plan = upd.plan_swap(env_["app"], "9.0.0", env_["folder"])
    with pytest.raises(ValueError):
        upd.spawn_helper(plan, 1, env_["folder"], mode="later")


def test_self_test_flag_marks_launched_and_exits(monkeypatch):
    from safecontext import cli
    marked = []
    monkeypatch.setattr(upd, "mark_launched", lambda *a, **k: marked.append(True))
    monkeypatch.setattr(cli, "cmd_app", lambda args: pytest.fail("must not open the window"))
    assert cli.main([upd.SELF_TEST_ARG]) == 0
    assert marked == [True]


def test_backend_update_choices(env_, backend_with):
    spawned = []
    u, server, clock = timed_updater(env_, spawn=lambda plan, pid, folder, **kw: spawned.append(kw))
    b = backend_with(u)
    b._quit_fn = lambda: pytest.fail("must not quit")
    assert b.update_check()["available"]
    assert b.update_later()["later"] is True
    assert b.update_check()["later"] is False
    assert b.update_skip()["skipped"] is True
    assert b.update_unskip()["skipped"] is False
    b.update_queue()
    wait_for(lambda: b.update_status()["phase"] == "ready")
    b._on_window_closing()
    b._on_window_closing()
    assert spawned == [{"mode": "quit"}]
    assert b.update_set_on_quit(True)["install_on_quit"] is True


def test_backend_cancel_and_errors(env_, backend_with):
    u, server, _ = timed_updater(env_)
    b = backend_with(u)
    b.update_check()
    b.update_queue()
    wait_for(lambda: b.update_status()["phase"] == "ready")
    assert b.update_cancel_queue()["on_quit"] is False
    server.body = manifest(version="1.0.0")
    b.update_check()
    assert b.update_queue() == {"ok": False, "error": "There's no newer version to install."}
    assert b.update_skip() == {"ok": False, "error": "There's no newer version to skip."}


def test_backend_window_focus_asks_for_a_check(env_, backend_with):
    u, server, clock = timed_updater(env_)
    b = backend_with(u)
    b.window_focused()
    assert u._kick.is_set()


def test_demo_update_flow(tmp_path):
    from safecontext.app.demo import demo_backend_kwargs
    from safecontext.demo.fake_update import next_version
    b = Backend(**demo_backend_kwargs(tmp_path / "demo"), auto_sync=False)
    b._quit_fn = lambda: pytest.fail("the demo must not quit")
    try:
        s = b.update_check()
        assert s["available"] and s["version"] == next_version() and s["can_install"] and s["notes"]
        assert b.update_check()["version"] == next_version()  # second check is a fake 304
        b.update_queue()
        wait_for(lambda: b.update_status()["phase"] == "ready", timeout=20)
        b.update_install()
        wait_for(lambda: b.update_status()["phase"] == "installing", timeout=20)
        b._on_window_closing()
    finally:
        b._shutdown()
