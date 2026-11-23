"""Keep every test away from the real data folder and the real Keychain.

`cli.main` moves ~/Library/Application Support/MyContext on startup, and reading
a Keychain secret may copy it from its old MyContext name. So each test gets a
fake home directory (also inherited by subprocesses, via HOME), data paths
inside it, and an in-memory keyring (subprocesses get keyring's null backend).
"""
import keyring
import keyring.errors
import pytest
from keyring.backend import KeyringBackend

from safecontext import env, migrate, store
from safecontext.app import backend


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.items:
            raise keyring.errors.PasswordDeleteError("missing")
        del self.items[(service, username)]


@pytest.fixture(autouse=True)
def _sandbox(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    app_support = home / "Library" / "Application Support"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    monkeypatch.setattr(migrate, "APP_SUPPORT", app_support)
    monkeypatch.setattr(migrate, "LEGACY_DATA_DIR", app_support / "MyContext")
    data_dir = app_support / env.data_dir_name()  # SafeContext-Dev: tests run from a checkout
    monkeypatch.setattr(store, "DEFAULT_DATA_DIR", data_dir)
    monkeypatch.setattr(backend, "DEFAULT_DATA_DIR", data_dir)
    old = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    yield kr
    keyring.set_keyring(old)


@pytest.fixture
def fake_keyring(_sandbox):
    return _sandbox
