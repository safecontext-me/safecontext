"""Carry state over from when this app was called MyContext.

Three kinds of state had the old name in it:

* The data directory, ~/Library/Application Support/MyContext. `migrate_data_dir`
  renames it to the default data folder (an atomic rename, so the mail is never copied and
  there is never a second copy of it on disk) and leaves a symlink at the old
  path. The symlink keeps MCP client configs that pass
  `--data-dir …/MyContext` working, as well as anything still running the old
  code. `cli.main` calls it on every start; it is cheap and idempotent.

* Keychain entries ("MyContext Gmail" per account, "MyContext xAI"). These are
  copied lazily: `keychain_get` reads the new service and, if it is empty,
  falls back to the old one and copies the secret across. The Gmail token is
  per account and only the mail database knows the account, so doing it at read
  time covers every data directory. The old entry is kept so an older checkout
  still works; disconnecting removes both.

* Environment variables. `getenv("OAUTH_CLIENT")` reads SAFECONTEXT_OAUTH_CLIENT
  and falls back to MYCONTEXT_OAUTH_CLIENT.

Only a development checkout migrates. MyContext only ever ran from source, so
its folder and Keychain items were made by the checkout's Python. The packaged
app (see env.py) would have no MyContext state of its own, and reading Keychain
items another program created makes macOS ask for the keychain password, so it
never looks at them. A checkout carries them into its own dev names
(…/SafeContext-Dev, "SafeContext Gmail (Dev)"), which the packaged app never
reads either: neither build pulls the other's data or secrets.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import keyring
import keyring.errors

from . import env

log = logging.getLogger("safecontext.migrate")

LEGACY_NAME = "MyContext"
APP_SUPPORT = Path.home() / "Library" / "Application Support"
LEGACY_DATA_DIR = APP_SUPPORT / LEGACY_NAME
# Whether this build carries MyContext state over at all; see the module docstring.
MIGRATE_LEGACY = env.is_dev()

# Files a fresh, never-used data directory may hold; anything else means it has real data.
_DISPOSABLE = {"sync.lock", ".DS_Store"}


def getenv(name: str, default: Optional[str] = None) -> Optional[str]:
    """SAFECONTEXT_<name>, else the old MYCONTEXT_<name>, else default."""
    return os.environ.get(f"SAFECONTEXT_{name}") or os.environ.get(f"MYCONTEXT_{name}") or default


def legacy_service(name: str) -> Optional[str]:
    """The old Keychain service name to fall back to, or None when this build doesn't migrate."""
    return name if MIGRATE_LEGACY else None


def _is_unused(d: Path) -> bool:
    return all(p.name in _DISPOSABLE or (p.name.startswith("sync-") and p.name.endswith(".lock"))
               for p in d.iterdir())


def migrate_data_dir(new: Optional[Path] = None, old: Optional[Path] = None) -> str:
    """Move the old data directory to the new one and leave a symlink behind.

    Returns what happened: "moved", "already" (old path is already our symlink),
    "none" (no old data, or this build doesn't migrate), or "conflict" (both hold
    data; nothing is touched).
    """
    if not MIGRATE_LEGACY and old is None:
        return "none"
    if new is None:
        from .store import DEFAULT_DATA_DIR
        new = DEFAULT_DATA_DIR
    old = old if old is not None else LEGACY_DATA_DIR
    if old.is_symlink():
        return "already"
    if not old.is_dir():
        return "none"
    if new.is_dir() and not new.is_symlink():
        if not _is_unused(new):
            log.warning("Found data in both %s and %s; leaving both alone and using %s. "
                        "Move what you want to keep by hand.", old, new, new)
            return "conflict"
        for p in new.iterdir():
            p.unlink()
        new.rmdir()
    new.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(old, new)
    except FileNotFoundError:  # another process moved it first
        return "already" if old.is_symlink() or new.is_dir() else "none"
    except OSError:
        if new.exists():  # another process got there first and created the new one
            return "conflict" if old.is_dir() and not old.is_symlink() else "already"
        raise
    try:
        os.symlink(new, old)
    except FileExistsError:
        pass
    except OSError as e:  # the move is what matters; the link is for old configs
        log.warning("Moved data to %s but couldn't link the old path: %s", new, e)
    log.info("Moved data from %s to %s", old, new)
    return "moved"


def keychain_get(service: str, user: str, legacy_service: Optional[str]) -> Optional[str]:
    """Read a Keychain secret, copying it from `legacy_service` on first use.

    Raises keyring.errors.KeyringError if the Keychain can't be read at all."""
    value = keyring.get_password(service, user)
    if value or not legacy_service:
        return value
    try:
        value = keyring.get_password(legacy_service, user)
    except keyring.errors.KeyringError:
        return None
    if value:
        try:
            keyring.set_password(service, user, value)
            log.info("Copied Keychain item %r to %r", legacy_service, service)
        except keyring.errors.KeyringError:
            pass  # still usable this time; we'll try to copy again next read
    return value


def keychain_delete(service: str, user: str, legacy_service: Optional[str]) -> None:
    """Delete a secret under both names. Missing entries are fine."""
    for svc in (service, legacy_service):
        if not svc:
            continue
        try:
            keyring.delete_password(svc, user)
        except keyring.errors.KeyringError:  # includes "not found"
            pass
