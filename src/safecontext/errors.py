"""Errors shared by every source's sync."""
from __future__ import annotations


class SourceError(Exception):
    pass


class TransientError(SourceError):
    """Network or server trouble that outlasted retries. Safe to try again later."""


class NeedsPermission(SourceError):
    """A macOS permission (Full Disk Access, Calendars, Contacts...) or a file
    the source reads is missing. The user has to act; retrying won't help."""

    def __init__(self, message: str, requirement: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.requirement = requirement
