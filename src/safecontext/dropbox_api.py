"""Read-only Dropbox HTTP API.

Lists files and downloads text. There is no upload, delete, move, or share
call in this module: Dropbox is only ever read. Tokens are passed in; sign-in
lives in dropbox_auth.py. Responses are not logged — a listing contains names
and paths.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional, Protocol

from .auth import NeedsReconnect
from .dropbox_auth import RECONNECT_MESSAGE
from .errors import TransientError

API = "https://api.dropboxapi.com"
CONTENT = "https://content.dropboxapi.com"


class CursorExpired(Exception):
    """Dropbox forgot the listing cursor. The sync starts again from the top."""


class NotDownloadable(Exception):
    """This file has no bytes Dropbox will return. Keep the name and size only."""


class DropboxAPI(Protocol):
    def list_folder(self, cursor: Optional[str]) -> dict:
        """One page. `cursor` None starts a listing; otherwise it continues one."""

    def download(self, file_id: str) -> bytes:
        """The file's bytes. Callers decide whether those bytes are text."""


class DropboxHTTP:
    """The real client. `opener` is urllib.request.urlopen, replaced in tests."""

    def __init__(self, access_token: str, *, opener=None, timeout: float = 60) -> None:
        self._token = access_token
        self._open = opener or urllib.request.urlopen
        self._timeout = timeout

    def list_folder(self, cursor: Optional[str]) -> dict:
        if cursor:
            url = f"{API}/2/files/list_folder/continue"
            payload = {"cursor": cursor}
        else:
            url = f"{API}/2/files/list_folder"
            # include_deleted so a later page of this same cursor reports removals.
            payload = {"path": "", "recursive": True, "include_deleted": True,
                       "include_non_downloadable_files": True, "limit": 500}
        return json.loads(self._json(url, payload).decode("utf-8"))

    def download(self, file_id: str) -> bytes:
        # The id is ASCII (`id:...`), so it is safe in a header. The path is not used:
        # a name with spaces or non-ASCII would break an HTTP header, and we don't need it.
        req = urllib.request.Request(f"{CONTENT}/2/files/download", data=b"", method="POST")
        req.add_header("Authorization", "Bearer " + self._token)
        req.add_header("Dropbox-API-Arg", json.dumps({"path": file_id}, separators=(",", ":")))
        return self._read(req, download=True)

    def _json(self, url: str, payload: dict) -> bytes:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Authorization", "Bearer " + self._token)
        req.add_header("Content-Type", "application/json")
        return self._read(req, download=False)

    def _read(self, req: urllib.request.Request, *, download: bool) -> bytes:
        try:
            with self._open(req, timeout=self._timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            code = getattr(e, "code", 0) or 0
            body = _error_body(e)
            if code == 401:
                raise NeedsReconnect("revoked_or_expired", RECONNECT_MESSAGE) from None
            if code == 409:
                tag = _error_tag(body)
                if tag == "reset":
                    raise CursorExpired() from None
                if download:
                    raise NotDownloadable() from None
            raise TransientError(f"Dropbox returned HTTP {code}") from None
        except NeedsReconnect:
            raise
        except (CursorExpired, NotDownloadable, TransientError):
            raise
        except Exception:
            raise TransientError("Couldn't reach Dropbox.") from None


def _error_body(exc: urllib.error.HTTPError) -> bytes:
    try:
        return exc.read() or b""
    except Exception:
        return b""


def _error_tag(body: bytes) -> str:
    try:
        err = json.loads(body.decode("utf-8") or "{}").get("error") or {}
    except Exception:
        return ""
    if isinstance(err, dict):
        return str(err.get(".tag") or "")
    return ""
