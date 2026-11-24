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


def make_updater(e, *, fetch=lambda url: manifest(), download=None, run=None, verify=None, **kw):
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
    u = make_updater(env_, fetch=lambda url: urls.append(url) or manifest())
    s = u.check()
    assert urls == [upd.MANIFEST_URL]
    assert s["available"] and s["version"] == "9.0.0" and s["notes"] == "Faster sync."
    assert s["can_install"] and s["phase"] == "idle" and s["checked_ms"]
    req = upd._request(upd.MANIFEST_URL)
    assert req.get_method() == "GET" and req.data is None
    assert set(k.lower() for k in req.headers) == {"user-agent", "cache-control"}
    assert req.headers["User-agent"] == "SafeContext-Updater"  # no version or identifier


def test_check_up_to_date_and_offline(env_):
    u = make_updater(env_, fetch=lambda url: manifest(version="1.0.0"))
    assert u.check()["available"] is False

    def offline(url):
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
                      f'echo "$1" >> "{tmp_path}/opened.txt"\n'
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
    u = make_updater(env_, fetch=lambda url: manifest(version="1.0.0"))
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
