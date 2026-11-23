"""Moving from the MyContext name to SafeContext. Uses the fake home and keyring from conftest."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from safecontext import auth, migrate, store as store_mod
from safecontext.cli import main
from safecontext.store import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def paths():
    return store_mod.DEFAULT_DATA_DIR, migrate.LEGACY_DATA_DIR


def make_old_data(old: Path, account="alex@example.org"):
    s = Store(old / "mail.db")
    s.set_state("gmail", "account", account)
    s.close()
    (old / "sync.lock").touch()


def account_in(d: Path) -> str:
    s = Store(d / "mail.db")
    try:
        return s.get_state("gmail", "account")
    finally:
        s.close()


# --- data directory -----------------------------------------------------------------

def test_sandbox_is_not_the_real_home(paths):
    real = Path(os.path.expanduser("~"))  # HOME is patched, so this is the fake one
    assert all(str(p).startswith(str(real)) for p in paths)
    assert "Library/Application Support/SafeContext" in str(paths[0])


def test_moves_old_folder_and_links_the_old_path(paths):
    new, old = paths
    make_old_data(old)
    assert migrate.migrate_data_dir() == "moved"
    assert new.is_dir() and not new.is_symlink()
    assert old.is_symlink() and old.resolve() == new.resolve()
    assert account_in(new) == "alex@example.org"
    assert account_in(old) == "alex@example.org"  # old --data-dir in MCP configs still works
    assert (new / "sync.lock").exists()
    assert oct(new.stat().st_mode & 0o777) == "0o700"


def test_is_idempotent(paths):
    new, old = paths
    make_old_data(old)
    assert migrate.migrate_data_dir() == "moved"
    before = sorted(p.name for p in new.iterdir())
    assert migrate.migrate_data_dir() == "already"
    assert migrate.migrate_data_dir() == "already"
    assert sorted(p.name for p in new.iterdir()) == before
    assert old.is_symlink()


def test_nothing_to_do_on_a_fresh_install(paths):
    new, old = paths
    assert migrate.migrate_data_dir() == "none"
    assert not new.exists() and not old.exists()


def test_both_have_data_leaves_both_alone(paths):
    new, old = paths
    make_old_data(old, "old@example.org")
    make_old_data(new, "new@example.org")
    assert migrate.migrate_data_dir() == "conflict"
    assert not old.is_symlink()
    assert account_in(old) == "old@example.org" and account_in(new) == "new@example.org"


def test_replaces_an_unused_new_folder(paths):
    new, old = paths
    make_old_data(old)
    new.mkdir(parents=True)
    (new / "sync.lock").touch()
    assert migrate.migrate_data_dir() == "moved"
    assert account_in(new) == "alex@example.org"


def test_open_database_survives_the_move(paths):
    """An MCP server from before the rename may have the database open while the new app moves it."""
    new, old = paths
    make_old_data(old)
    running = Store(old / "mail.db")
    assert migrate.migrate_data_dir() == "moved"
    running.set_meta("written_after_move", "yes")
    running.close()
    s = Store(new / "mail.db")
    assert s.get_meta("written_after_move") == "yes"
    s.close()


def test_cli_moves_data_on_start(paths, capsys):
    new, old = paths
    make_old_data(old)
    assert main(["status"]) == 0
    assert old.is_symlink() and account_in(new) == "alex@example.org"
    assert "alex@example.org" in capsys.readouterr().out


def test_cli_with_old_data_dir_argument(paths, capsys):
    """What an MCP config written by MyContext passes: --data-dir …/MyContext."""
    new, old = paths
    make_old_data(old)
    assert main(["--data-dir", str(old), "status"]) == 0
    assert old.is_symlink() and account_in(new) == "alex@example.org"
    assert "alex@example.org" in capsys.readouterr().out


def test_cli_stops_if_the_move_fails(paths, monkeypatch, capsys):
    new, old = paths
    make_old_data(old)

    def boom(*a):
        raise PermissionError("denied")
    monkeypatch.setattr(migrate.os, "rename", boom)
    assert main(["status"]) == 1
    assert "Couldn't move your data" in capsys.readouterr().err
    assert not new.exists()  # no empty mailbox created in its place


# --- Keychain ------------------------------------------------------------------------

def test_gmail_token_is_copied_from_old_name(fake_keyring):
    blob = json.dumps({"token": "ya29.t", "refresh_token": "1//r", "client_id": "c", "client_secret": "s",
                       "token_uri": "https://oauth2.googleapis.com/token",
                       "scopes": [auth.SCOPE_READONLY]})
    fake_keyring.items[("MyContext Gmail", "a@example.org")] = blob
    creds = auth.TokenStore().load("a@example.org")
    assert creds.refresh_token == "1//r"
    assert fake_keyring.items[("SafeContext Gmail", "a@example.org")] == blob
    assert ("MyContext Gmail", "a@example.org") in fake_keyring.items  # kept for older checkouts
    auth.TokenStore().load("a@example.org")  # idempotent
    assert fake_keyring.items[("SafeContext Gmail", "a@example.org")] == blob


def test_new_token_wins_over_old(fake_keyring):
    fake_keyring.items[("MyContext Gmail", "a@example.org")] = "old"
    fake_keyring.items[("SafeContext Gmail", "a@example.org")] = "new"
    assert migrate.keychain_get("SafeContext Gmail", "a@example.org", "MyContext Gmail") == "new"
    assert fake_keyring.items[("MyContext Gmail", "a@example.org")] == "old"


def test_forgetting_a_token_removes_both_names(fake_keyring):
    fake_keyring.items[("MyContext Gmail", "a@example.org")] = "old"
    fake_keyring.items[("SafeContext Gmail", "a@example.org")] = "new"
    auth.TokenStore().delete("a@example.org")
    assert fake_keyring.items == {}


def test_leftover_xai_key_is_deleted_once(fake_keyring, tmp_path):
    """Ask (which sent questions to xAI's API) was removed; its saved key goes on first run."""
    from safecontext.store import Store
    fake_keyring.items[("SafeContext xAI", "api_key")] = "xai-new-key-0123456789"
    fake_keyring.items[("MyContext xAI", "api_key")] = "xai-old-key-0123456789"
    fake_keyring.items[("SafeContext Gmail", "a@example.org")] = "token"
    store = Store(tmp_path / "mail.db")
    store.set_meta("grok_model", "grok-x")
    assert migrate.remove_ask_leftovers(store) is True
    assert fake_keyring.items == {("SafeContext Gmail", "a@example.org"): "token"}  # Gmail untouched
    assert store.get_meta("grok_model") is None
    fake_keyring.items[("SafeContext xAI", "api_key")] = "xai-again-0123456789"
    assert migrate.remove_ask_leftovers(store) is False   # only once per data folder
    assert ("SafeContext xAI", "api_key") in fake_keyring.items


def test_leftover_cleanup_survives_a_locked_keychain(fake_keyring, tmp_path, monkeypatch):
    import keyring.errors
    from safecontext.store import Store

    def boom(*a):
        raise keyring.errors.KeyringError("locked")
    monkeypatch.setattr(migrate.keyring, "delete_password", boom)
    store = Store(tmp_path / "mail.db")
    assert migrate.remove_ask_leftovers(store) is True  # never raises


def test_no_old_secret_means_none(fake_keyring):
    assert migrate.keychain_get("SafeContext xAI", "api_key", "MyContext xAI") is None
    assert fake_keyring.items == {}


# --- environment and the old command -------------------------------------------------

def test_old_environment_variables_still_work(monkeypatch):
    monkeypatch.delenv("SAFECONTEXT_OAUTH_CLIENT", raising=False)
    monkeypatch.setenv("MYCONTEXT_OAUTH_CLIENT", "old")
    assert migrate.getenv("OAUTH_CLIENT") == "old"
    monkeypatch.setenv("SAFECONTEXT_OAUTH_CLIENT", "new")
    assert migrate.getenv("OAUTH_CLIENT") == "new"


def test_python_dash_m_mycontext_still_serves_mcp(tmp_path):
    """MCP configs written before the rename run `python -m mycontext … mcp`."""
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}}
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONWARNINGS": "ignore"}
    proc = subprocess.run([sys.executable, "-m", "mycontext", "--data-dir", str(tmp_path / "d"), "mcp"],
                          input=json.dumps(init) + "\n", capture_output=True, text=True, env=env, timeout=60)
    lines = proc.stdout.splitlines()
    assert len(lines) == 1, proc.stderr  # the deprecation notice must not reach stdout
    assert json.loads(lines[0])["result"]["serverInfo"]["name"] == "safecontext"
    assert "now SafeContext" in proc.stderr


def test_old_console_script_entry_point_still_resolves(capsys):
    """`mycontext` scripts installed before the rename call mycontext.cli:main."""
    from mycontext.cli import main as old_main
    assert old_main(["status"]) == 0
    captured = capsys.readouterr()
    assert "now SafeContext" in captured.err and "now SafeContext" not in captured.out
