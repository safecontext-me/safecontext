"""Dev checkout vs packaged app: separate data folders, Keychain names and MCP names."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from safecontext import auth, env, grok, migrate, store
from safecontext.readers import recordings

ROOT = Path(__file__).resolve().parents[1]
APP_CODE = Path("/Applications/SafeContext.app/Contents/Resources/app/safecontext/env.py")
REPO_CODE = ROOT / "src" / "safecontext" / "env.py"
PYTHON = "/Users/x/proj/.venv/bin/python"


# --- detection -----------------------------------------------------------------------

def test_checkout_is_dev():
    assert env.detect({}, REPO_CODE, PYTHON) == env.DEV
    assert env.CURRENT == env.DEV  # this test run


def test_code_inside_an_app_bundle_is_the_app():
    assert env.detect({}, APP_CODE, PYTHON) == env.APP


def test_bundle_launcher_is_the_app():
    assert env.detect({}, REPO_CODE, "/Applications/SafeContext.app/Contents/MacOS/SafeContext") == env.APP


def test_framework_python_app_is_not_the_bundle():
    framework = Path("/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python")
    assert env.detect({}, REPO_CODE, str(framework)) == env.DEV


def test_frozen_is_the_app(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert env.detect({}, REPO_CODE, PYTHON) == env.APP


@pytest.mark.parametrize("value, expected", [("dev", env.DEV), ("DEV", env.DEV), ("development", env.DEV),
                                             ("app", env.APP), ("packaged", env.APP), ("prod", env.APP)])
def test_override_wins_both_ways(value, expected):
    assert env.detect({env.ENV_VAR: value}, APP_CODE, PYTHON) == expected
    assert env.detect({env.ENV_VAR: value}, REPO_CODE, PYTHON) == expected


def test_bad_override_stops_instead_of_guessing():
    with pytest.raises(SystemExit):
        env.detect({env.ENV_VAR: "staging"}, REPO_CODE, PYTHON)


# --- names -----------------------------------------------------------------------------

def test_app_keeps_the_shipped_names():
    assert env.keychain_service("SafeContext Gmail", env.APP) == "SafeContext Gmail"
    assert env.default_data_dir(env.APP).name == "SafeContext"
    assert env.mcp_server_name(env.APP) == "safecontext"
    assert env.label(env.APP) == ""


def test_dev_gets_its_own_names():
    assert env.keychain_service("SafeContext Gmail", env.DEV) == "SafeContext Gmail (Dev)"
    assert env.default_data_dir(env.DEV).name == "SafeContext-Dev"
    assert env.mcp_server_name(env.DEV) == "safecontext-dev"


def test_checkout_constants_use_dev_names():
    assert auth.KEYRING_SERVICE == "SafeContext Gmail (Dev)"
    assert auth.CALENDAR_KEYRING_SERVICE == "SafeContext Google Calendar (Dev)"
    assert grok.KEYRING_SERVICE == "SafeContext xAI (Dev)"
    assert auth.TokenStore().service == "SafeContext Gmail (Dev)"
    assert auth.calendar_tokens().service == "SafeContext Google Calendar (Dev)"
    assert recordings.default_models_dir().parts[-3:] == ("SafeContext-Dev", "models", "whisper")


def _names_in_subprocess(env_value: str, home: Path) -> dict:
    code = ("import json; from safecontext import auth, grok, store, migrate, env;"
            "print(json.dumps({'env': env.CURRENT, 'gmail': auth.KEYRING_SERVICE, 'gcal': auth.CALENDAR_KEYRING_SERVICE,"
            "'xai': grok.KEYRING_SERVICE, 'legacy': [auth.LEGACY_KEYRING_SERVICE, grok.LEGACY_KEYRING_SERVICE],"
            "'data': str(store.DEFAULT_DATA_DIR)}))")
    environ = {**os.environ, "HOME": str(home), env.ENV_VAR: env_value, "PYTHONPATH": str(ROOT / "src")}
    out = subprocess.run([sys.executable, "-c", code], env=environ, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_the_two_builds_share_nothing(tmp_path):
    app, dev = _names_in_subprocess("app", tmp_path), _names_in_subprocess("dev", tmp_path)
    assert app == {"env": "app", "gmail": "SafeContext Gmail", "gcal": "SafeContext Google Calendar",
                   "xai": "SafeContext xAI", "legacy": [None, None],
                   "data": str(tmp_path / "Library" / "Application Support" / "SafeContext")}
    assert dev["data"] == str(tmp_path / "Library" / "Application Support" / "SafeContext-Dev")
    assert dev["legacy"] == ["MyContext Gmail", "MyContext xAI"]
    for key in ("gmail", "gcal", "xai", "data"):
        assert app[key] != dev[key]


def test_data_dir_flag_still_wins(tmp_path):
    from safecontext.cli import main
    assert main(["--data-dir", str(tmp_path / "elsewhere"), "status"]) == 0
    assert (tmp_path / "elsewhere" / "mail.db").exists()
    assert not store.DEFAULT_DATA_DIR.exists()


# --- MyContext migration ------------------------------------------------------------

def test_app_never_touches_mycontext_state(monkeypatch, fake_keyring):
    """The packaged app must not adopt the checkout's old folder or read its Keychain items
    (that would make macOS ask for the keychain password)."""
    monkeypatch.setattr(migrate, "MIGRATE_LEGACY", False)
    migrate.LEGACY_DATA_DIR.mkdir(parents=True)
    (migrate.LEGACY_DATA_DIR / "mail.db").write_bytes(b"x")
    assert migrate.migrate_data_dir() == "none"
    assert migrate.LEGACY_DATA_DIR.is_dir() and not migrate.LEGACY_DATA_DIR.is_symlink()
    assert not store.DEFAULT_DATA_DIR.exists()
    assert migrate.legacy_service("MyContext Gmail") is None

    reads = []
    orig = fake_keyring.get_password
    fake_keyring.get_password = lambda s, u: reads.append(s) or orig(s, u)
    fake_keyring.items[("MyContext xAI", "api_key")] = "xai-old"
    assert migrate.keychain_get("SafeContext xAI", "api_key", migrate.legacy_service("MyContext xAI")) is None
    assert reads == ["SafeContext xAI"]


def test_dev_carries_mycontext_into_dev_names(fake_keyring):
    fake_keyring.items[("MyContext Gmail", "a@example.org")] = "blob"
    assert auth.TokenStore().legacy_service == "MyContext Gmail"
    assert migrate.keychain_get(auth.KEYRING_SERVICE, "a@example.org", auth.LEGACY_KEYRING_SERVICE) == "blob"
    assert fake_keyring.items[("SafeContext Gmail (Dev)", "a@example.org")] == "blob"
    assert ("SafeContext Gmail", "a@example.org") not in fake_keyring.items  # the app's name is untouched
