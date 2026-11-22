"""Deprecated. Kept because `mycontext` console scripts installed before the rename
import `mycontext.cli:main`."""
from safecontext.cli import legacy_main as main

__all__ = ["main"]
