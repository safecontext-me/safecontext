"""Which SafeContext is this: the packaged Mac app, or a development checkout?

The two keep separate state so they can run side by side without touching each
other's data folder, sync locks or Keychain items:

=====================  ==========================================  ===================================
                       packaged app (SafeContext.app)              development (source checkout)
=====================  ==========================================  ===================================
data folder            ~/Library/Application Support/SafeContext   …/Application Support/SafeContext-Dev
Keychain services      "SafeContext Gmail", …                      "SafeContext Gmail (Dev)", …
MCP server name        safecontext                                 safecontext-dev
=====================  ==========================================  ===================================

How it's decided: the packaged app is the one whose code lives inside a macOS
``.app`` bundle (Briefcase puts it at ``SafeContext.app/Contents/Resources/app``)
or that was launched by the bundle's own launcher. Everything else (an editable
install, ``python -m safecontext`` from the repo, ``briefcase dev``, the test
suite) is development. This is read from where the code physically is, not from
an environment variable, because an MCP client or a double-click in Finder
starts SafeContext without any of your shell's variables; a forgotten
``export`` must never make a dev build open the real app's data or secrets.

``SAFECONTEXT_ENV=dev`` or ``SAFECONTEXT_ENV=app`` overrides the detection (for
example to test the packaged layout from a checkout). ``--data-dir`` still wins
over the default data folder either way.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

ENV_VAR = "SAFECONTEXT_ENV"
DEV, APP = "dev", "app"
_ALIASES = {"dev": DEV, "development": DEV, "app": APP, "packaged": APP, "prod": APP, "production": APP}

APP_NAME = "SafeContext"
DEV_SUFFIX = " (Dev)"


def _in_app_bundle(path: Path) -> bool:
    """True if `path` is inside a macOS .app bundle (…/X.app/Contents/…)."""
    parts = path.parts
    return any(p.endswith(".app") and i + 1 < len(parts) and parts[i + 1] == "Contents"
               for i, p in enumerate(parts))


def is_app_launcher(executable: str) -> bool:
    """True for a Briefcase .app launcher (…/SafeContext.app/Contents/MacOS/SafeContext),
    false for a python interpreter, including a framework build's Python.app."""
    exe = Path(executable)
    return (exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents"
            and not exe.name.lower().startswith("python"))


def detect(environ: Optional[dict] = None, code_path: Optional[Path] = None,
           executable: Optional[str] = None) -> str:
    """Return APP or DEV. Arguments exist for tests; by default reads this process."""
    environ = os.environ if environ is None else environ
    override = (environ.get(ENV_VAR) or "").strip().lower()
    if override:
        if override not in _ALIASES:
            raise SystemExit(f"{ENV_VAR}={override!r} isn't valid; use 'dev' or 'app'.")
        return _ALIASES[override]
    code_path = Path(__file__).resolve() if code_path is None else code_path
    executable = sys.executable if executable is None else executable
    if getattr(sys, "frozen", False) or _in_app_bundle(code_path) or is_app_launcher(executable or ""):
        return APP
    return DEV


CURRENT = detect()


def is_dev() -> bool:
    return CURRENT == DEV


def label(env: Optional[str] = None) -> str:
    """'' for the app, ' (Dev)' for development. Appended to Keychain service names and titles."""
    return DEV_SUFFIX if (env or CURRENT) == DEV else ""


def keychain_service(base: str, env: Optional[str] = None) -> str:
    """'SafeContext Gmail' → 'SafeContext Gmail (Dev)' in development."""
    return base + label(env)


def data_dir_name(env: Optional[str] = None) -> str:
    return APP_NAME + ("-Dev" if (env or CURRENT) == DEV else "")


def default_data_dir(env: Optional[str] = None) -> Path:
    return Path.home() / "Library" / "Application Support" / data_dir_name(env)


def mcp_server_name(env: Optional[str] = None) -> str:
    return "safecontext-dev" if (env or CURRENT) == DEV else "safecontext"
