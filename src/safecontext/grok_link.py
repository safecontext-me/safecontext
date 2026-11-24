"""The MCP link: use SafeContext from cloud AI apps (Grok, Claude, Muse) through a live relay.

The app shows this as "MCP". (The module keeps its old name, and its Keychain
entry and settings keep theirs, so existing links keep working.)

Custom connectors in these apps call a remote MCP server from the vendor's cloud,
so they can't reach this Mac directly. Instead the app keeps one outbound WebSocket
open to SafeContext's relay (relay/ in this repo, a Cloudflare Worker). The
relay checks the app's sign-in, then forwards each MCP request down the socket.
This module answers it here, from the scrubbed store, and sends the answer
back. Nothing is copied to the relay: it stores no request or response bodies
and only works while the Mac is awake and the app is open.

    AI app cloud --HTTPS (MCP Streamable HTTP)--> relay --WebSocket--> this Mac

Pieces:
  * MCPHttpHandler: the MCP Streamable HTTP endpoint, reusing mcp_server/tools.
    Only the read-only primary tools are served (no aliases, nothing else).
  * ActivityLog: what AI apps asked for (time, tool, query, number of results),
    shown in the app. Results themselves are never kept.
  * RelayClient: the outbound socket, with reconnect and backoff.
  * KeepAwake: optional `caffeinate -s` (stop idle sleep while on power).
  * GrokLink: ties them together for the app window.

Relay protocol (JSON text frames):
  relay -> Mac  {"t":"hello","grants":n}           connected; n = sign-ins apps hold
                {"t":"grants","n":n}               that number changed
                {"t":"http","id":..,"method":..,"headers":{..},"body":".."}
                {"t":"approve","id":..,"client":..,"code":"1234"}   an app wants to connect
                {"t":"approve_cancel","id":..}
  Mac -> relay  {"t":"http_result","id":..,"status":200,"headers":{..},"body":".."}
                {"t":"approve_result","id":..,"ok":true}
                {"t":"settings","link_only":false}
                {"t":"revoke_all"}                 forget every sign-in and client
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import secrets as _secrets
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .env import keychain_service
from .mcp_server import SUPPORTED_VERSIONS, MCPServer, _err
from .migrate import getenv
from .store import Store
from .tools import PRIMARY_TOOLS

log = logging.getLogger("safecontext.grok")

DEFAULT_RELAY_URL = "https://relay.safecontext.me"
GROK_CONNECTORS_URL = "https://grok.com/connectors"
# Where each app the MCP card has setup steps for adds a custom connector. Muse has no
# such page: you ask Muse in chat to set one up.
CONNECTOR_PAGES = {
    "grok": GROK_CONNECTORS_URL,
    "claude": "https://claude.ai/customize/connectors",
}
KEYRING_SERVICE = keychain_service("SafeContext Grok")  # "SafeContext Grok (Dev)" in a checkout
KEYRING_USER = "relay-link"

GROK_TOOLS = PRIMARY_TOOLS  # search, read, list_items, list_sources (all read-only)
GROK_TOOL_NAMES = frozenset(t["name"] for t in GROK_TOOLS)
ALLOWED_METHODS = frozenset({"initialize", "ping", "tools/list", "tools/call", "notifications/initialized",
                             "notifications/cancelled"})
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 900 * 1024  # the relay's socket carries at most 1 MiB per message
ACTIVITY_KEEP = 200
APPROVAL_TTL_MS = 5 * 60_000  # the relay's sign-in page gives up after this long

_META_ENABLED = "grok_link_enabled"
_META_KEEP_AWAKE = "grok_keep_awake"
_META_LINK_ONLY = "grok_link_only"
_META_SINCE = "grok_link_since_ms"


def relay_url() -> str:
    return (getenv("RELAY_URL") or DEFAULT_RELAY_URL).rstrip("/")


def new_secret() -> str:
    """256 random bits, URL-safe (43 characters)."""
    return base64.urlsafe_b64encode(_secrets.token_bytes(32)).rstrip(b"=").decode()


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- secrets -----------------------------------------------------------------------

class KeychainSecrets:
    """The link id (part of the connector URL) and this Mac's relay secret, in the Keychain."""

    def load(self) -> Optional[Dict[str, str]]:
        import keyring
        try:
            raw = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            return None
        try:
            d = json.loads(raw) if raw else None
        except ValueError:
            return None
        return d if isinstance(d, dict) and d.get("link_id") and d.get("device_secret") else None

    def save(self, value: Dict[str, str]) -> None:
        import keyring
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, json.dumps(value))

    def delete(self) -> None:
        import keyring
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            pass


class MemorySecrets(KeychainSecrets):
    """Demo mode: nothing written to the Keychain."""

    def __init__(self) -> None:
        self.value: Optional[Dict[str, str]] = None

    def load(self):
        return self.value

    def save(self, value):
        self.value = dict(value)

    def delete(self):
        self.value = None


# --- activity ----------------------------------------------------------------------

def describe_query(tool: str, args: Dict[str, Any]) -> str:
    """A short, human description of a request: the words searched and the filters."""
    a = args if isinstance(args, dict) else {}

    def s(k):
        v = a.get(k)
        return v if isinstance(v, str) and v else ""
    filters = [f"{label} {s(k)}" for k, label in (("source", "in"), ("sender", "from"), ("after", "after"),
                                                    ("before", "before")) if s(k)]
    if tool == "search":
        text = f"“{s('query')}”" + (" · " + ", ".join(filters) if filters else "")
    elif tool == "read":
        text = "Opened a thread" + (f" in {s('source')}" if s("source") else "")
    elif tool == "list_items":
        text = "Listed items" + (" · " + ", ".join(filters) if filters else "")
    elif tool == "list_sources":
        text = "Checked which sources are connected"
    else:
        text = tool
    return text[:160]


def _result_count(result: Dict[str, Any]) -> Optional[int]:
    if "error" in result:
        return None
    for key in ("count",):
        if isinstance(result.get(key), int):
            return result[key]
    for key in ("results", "messages", "sources"):
        if isinstance(result.get(key), list):
            return len(result[key])
    return None


class ActivityLog:
    """The last ACTIVITY_KEEP requests Grok made: time, tool, query, number of results.
    Kept in a 0600 file so it survives a restart; never holds result content."""

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._entries: List[Dict[str, Any]] = []
        if path and path.exists():
            try:
                data = json.loads(path.read_text())
                self._entries = [e for e in data if isinstance(e, dict)][-ACTIVITY_KEEP:]
            except (OSError, ValueError):
                self._entries = []

    def record(self, tool: str, args: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        entry = {"ts_ms": _now_ms(), "tool": tool, "query": describe_query(tool, args),
                 "results": _result_count(result), "error": "error" in result}
        with self._lock:
            self._entries.append(entry)
            del self._entries[:-ACTIVITY_KEEP]
            self._save()
        return entry

    def entries(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            return list(reversed(self._entries[-limit:]))

    def last_used_ms(self) -> Optional[int]:
        with self._lock:
            return self._entries[-1]["ts_ms"] if self._entries else None

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._save()

    def _save(self) -> None:
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self._entries, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("couldn't save Grok activity: %s", e)


# --- MCP Streamable HTTP --------------------------------------------------------------

def _json(status: int, payload: Any) -> Tuple[int, Dict[str, str], str]:
    return status, {"content-type": "application/json"}, json.dumps(payload, ensure_ascii=False)


class MCPHttpHandler:
    """The MCP Streamable HTTP endpoint (spec 2025-06-18), stateless, JSON responses only.

    The relay passes on Grok's request (method, a few headers, body) and returns
    whatever this produces. Only ALLOWED_METHODS and GROK_TOOLS get through, even
    though the relay checks the same allowlist first."""

    def __init__(self, store: Store, activity: Optional[ActivityLog] = None) -> None:
        self.store = store
        self.activity = activity

    def handle(self, method: str, headers: Dict[str, str], body: str) -> Tuple[int, Dict[str, str], str]:
        method = (method or "").upper()
        if method != "POST":  # no server-initiated stream (GET) and no sessions to end (DELETE)
            return 405, {"allow": "POST", "content-type": "application/json"}, json.dumps(
                _err(None, -32000, "Method not allowed: send JSON-RPC with POST."))
        if len((body or "").encode()) > MAX_REQUEST_BYTES:
            return _json(413, _err(None, -32600, "Request too large."))
        try:
            payload = json.loads(body or "")
        except ValueError:
            return _json(400, _err(None, -32700, "Parse error"))
        batch = isinstance(payload, list)
        msgs = payload if batch else [payload]
        if not msgs:
            return _json(400, _err(None, -32600, "Invalid Request"))

        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        server = MCPServer(self.store, tools=GROK_TOOLS, on_call=self._on_call)
        # Stateless: each request says its protocol version (some clients forget; use the newest).
        version = hdrs.get("mcp-protocol-version")
        server.protocol_version = version if version in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        replies = []
        for msg in msgs:
            reply = self._one(server, msg)
            if reply is not None:
                replies.append(reply)
        if not replies:  # only notifications or responses
            return 202, {}, ""
        status, out_headers, text = _json(200, replies if batch else replies[0])
        if len(text.encode()) > MAX_RESPONSE_BYTES:
            first = replies[0].get("id") if isinstance(replies[0], dict) else None
            return _json(200, _err(first, -32603, "The result was too large to send. Ask for fewer items."))
        return status, out_headers, text

    def _one(self, server: MCPServer, msg: Any) -> Optional[Dict[str, Any]]:
        if isinstance(msg, dict) and isinstance(msg.get("method"), str) and msg["method"] not in ALLOWED_METHODS:
            return None if "id" not in msg else _err(msg.get("id"), -32601, f"Method not found: {msg['method']}")
        try:
            return server.handle(msg)
        except Exception as e:  # never send a traceback (or data) back up the relay
            log.warning("Grok request failed: %s", type(e).__name__)
            return _err(msg.get("id") if isinstance(msg, dict) else None, -32603, "Internal error")

    def _on_call(self, name: str, args: Dict[str, Any], result: Dict[str, Any]) -> None:
        if self.activity:
            self.activity.record(name, args, result)


# --- the socket to the relay -------------------------------------------------------------

class RelayRejected(Exception):
    """The relay refused this Mac's secret (another Mac holds this link)."""


def _ws_connect(url: str, secret: str):
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect
    try:
        return connect(url, additional_headers={"Authorization": f"Bearer {secret}"}, open_timeout=15,
                       close_timeout=5, ping_interval=25, ping_timeout=20, max_size=2 ** 20,
                       user_agent_header="SafeContext")
    except InvalidStatus as e:
        if e.response.status_code in (401, 403):
            raise RelayRejected() from None
        raise


class RelayClient:
    """Keeps one outbound WebSocket to the relay open, reconnecting with backoff.

    `connect(url, secret)` returns a connection with send(), close() and iteration
    over incoming text frames (websockets' sync client, or a fake in tests/demo)."""

    def __init__(self, url: str, secret: str, on_message: Callable[[Dict[str, Any]], None],
                 on_state: Callable[[str, str], None], connect: Optional[Callable] = None,
                 backoff: Tuple[float, float] = (1.0, 60.0)) -> None:
        self.url, self._secret = url, secret
        self._on_message, self._on_state = on_message, on_state
        self._connect = connect or _ws_connect
        self._backoff = backoff
        self._stop = threading.Event()
        self._conn = None
        self._send_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="grok-relay")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        conn = self._conn
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def send(self, msg: Dict[str, Any]) -> bool:
        conn = self._conn
        if conn is None:
            return False
        try:
            with self._send_lock:
                conn.send(json.dumps(msg, ensure_ascii=False))
            return True
        except Exception:
            return False

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            self._on_state("connecting", "")
            try:
                conn = self._connect(self.url, self._secret)
            except RelayRejected:
                self._on_state("rejected", "The relay didn't accept this Mac for this link. Regenerate the link.")
                delay = 300.0
            except Exception as e:
                self._on_state("offline", f"Can't reach the SafeContext relay ({type(e).__name__}). Retrying…")
                delay = self._delay(attempt)
                attempt += 1
            else:
                self._conn = conn
                started = time.monotonic()
                self._on_state("online", "")
                try:
                    for raw in conn:
                        if self._stop.is_set():
                            break
                        try:
                            msg = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(msg, dict):
                            self._on_message(msg)
                except Exception:
                    pass
                finally:
                    self._conn = None
                    try:
                        conn.close()
                    except Exception:
                        pass
                if time.monotonic() - started > 60:
                    attempt = 0  # it was a good connection; start the backoff over
                if self._stop.is_set():
                    break
                self._on_state("offline", "Reconnecting to the SafeContext relay…")
                delay = self._delay(attempt)
                attempt += 1
            self._stop.wait(delay)
        self._on_state("stopped", "")

    def _delay(self, attempt: int) -> float:
        lo, hi = self._backoff
        return min(hi, lo * (2 ** attempt)) * random.uniform(0.5, 1.0)


# --- keep awake ----------------------------------------------------------------------------

def _caffeinate() -> Any:
    # -s: prevent system sleep, honoured only while on AC power. -w: end with this process.
    return subprocess.Popen(["/usr/bin/caffeinate", "-s", "-w", str(os.getpid())],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class KeepAwake:
    def __init__(self, runner: Optional[Callable[[], Any]] = None) -> None:
        self._runner = runner or _caffeinate
        self._proc = None

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def set(self, on: bool) -> None:
        if on and not self.active:
            try:
                self._proc = self._runner()
            except OSError as e:
                log.warning("couldn't keep the Mac awake: %s", e)
                self._proc = None
        elif not on and self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None


# --- the feature, for the app window -----------------------------------------------------------

class GrokLink:
    def __init__(self, store: Store, data_dir: Optional[Path], *, secrets: Optional[KeychainSecrets] = None,
                 base_url: Optional[str] = None, connect: Optional[Callable] = None,
                 keep_awake_runner: Optional[Callable[[], Any]] = None,
                 backoff: Tuple[float, float] = (1.0, 60.0)) -> None:
        self.store = store
        self.base_url = (base_url or relay_url()).rstrip("/")
        self.secrets = secrets or KeychainSecrets()
        self.activity = ActivityLog(Path(data_dir) / "grok_activity.json" if data_dir else None)
        self.handler = MCPHttpHandler(store, self.activity)
        self._connect = connect
        self._backoff = backoff
        self._keep_awake = KeepAwake(keep_awake_runner)
        self._lock = threading.RLock()
        self._client: Optional[RelayClient] = None
        self._conn_state, self._conn_message = "off", ""
        self._grants = 0
        self._approvals: Dict[str, Dict[str, Any]] = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="grok-req")

    # settings
    @property
    def enabled(self) -> bool:
        return self.store.get_meta(_META_ENABLED) == "1"

    @property
    def keep_awake(self) -> bool:
        return self.store.get_meta(_META_KEEP_AWAKE) == "1"

    @property
    def link_only(self) -> bool:
        return self.store.get_meta(_META_LINK_ONLY) == "1"

    def _ids(self, create: bool = False) -> Optional[Dict[str, str]]:
        ids = self.secrets.load()
        if ids is None and create:
            ids = {"link_id": new_secret(), "device_secret": new_secret()}
            self.secrets.save(ids)
            self.store.set_meta(_META_SINCE, str(_now_ms()))
        return ids

    def connector_url(self) -> Optional[str]:
        ids = self._ids()
        return f"{self.base_url}/g/{ids['link_id']}/mcp" if ids else None

    def oauth_urls(self) -> Dict[str, str]:
        ids = self._ids()
        if not ids:
            return {}
        root = f"{self.base_url}/g/{ids['link_id']}"
        return {"client_id": "safecontext", "authorize": f"{root}/authorize", "token": f"{root}/token"}

    # lifecycle
    def start(self) -> None:
        """At app launch: reconnect if Grok was left on."""
        if self.enabled:
            self._start_client()

    def enable(self) -> str:
        with self._lock:
            self._ids(create=True)
            self.store.set_meta(_META_ENABLED, "1")
            self._start_client()
        return self.connector_url() or ""

    def disable(self) -> None:
        with self._lock:
            self.store.set_meta(_META_ENABLED, None)
            self._stop_client()

    def regenerate(self) -> str:
        """New link and Mac secret. The old link stops working at once and every sign-in
        Grok holds is forgotten (the relay deletes them when told; the old link's address
        is gone either way)."""
        with self._lock:
            client = self._client
            if client and client.connected:
                client.send({"t": "revoke_all"})
                time.sleep(0.3)  # let it reach the relay before the socket closes
            self._stop_client()
            self.secrets.delete()
            self._ids(create=True)
            self._grants = 0
            if self.enabled:
                self._start_client()
        return self.connector_url() or ""

    def set_keep_awake(self, on: bool) -> None:
        self.store.set_meta(_META_KEEP_AWAKE, "1" if on else None)
        self._keep_awake.set(bool(on) and self.enabled)

    def set_link_only(self, on: bool) -> None:
        self.store.set_meta(_META_LINK_ONLY, "1" if on else None)
        if self._client:
            self._client.send({"t": "settings", "link_only": bool(on)})

    def approve(self, approval_id: str, ok: bool) -> bool:
        with self._lock:
            pending = self._approvals.pop(approval_id, None)
        if pending is None or not self._client:
            return False
        return self._client.send({"t": "approve_result", "id": approval_id, "ok": bool(ok)})

    def shutdown(self) -> None:
        self._stop_client()
        self._pool.shutdown(wait=False)

    def _start_client(self) -> None:
        self._stop_client()
        ids = self._ids(create=True)
        ws_base = "wss://" + self.base_url[len("https://"):] if self.base_url.startswith("https://") else \
            "ws://" + self.base_url[len("http://"):]
        # Events from a client that has since been replaced are ignored.
        client = RelayClient(f"{ws_base}/g/{ids['link_id']}/connect", ids["device_secret"],
                             lambda m: self._on_message(m) if self._client is client else None,
                             lambda s, m: self._on_state(s, m) if self._client is client else None,
                             connect=self._connect, backoff=self._backoff)
        self._client = client
        client.start()
        self._keep_awake.set(self.keep_awake)

    def _stop_client(self) -> None:
        client, self._client = self._client, None
        if client:
            client.stop()
        self._keep_awake.set(False)
        self._approvals.clear()
        self._conn_state, self._conn_message = "off", ""

    # relay events
    def _on_state(self, state: str, message: str) -> None:
        if state == "stopped":
            return
        self._conn_state, self._conn_message = state, message
        if state == "online" and self._client:
            self._client.send({"t": "settings", "link_only": self.link_only})
        if state != "online":
            self._approvals.clear()

    def _on_message(self, msg: Dict[str, Any]) -> None:
        t = msg.get("t")
        if t in ("hello", "grants"):
            n = msg.get("grants", msg.get("n"))
            self._grants = n if isinstance(n, int) and n >= 0 else 0
        elif t == "http":
            self._pool.submit(self._answer, msg)
        elif t == "approve" and isinstance(msg.get("id"), str):
            with self._lock:
                self._approvals = {msg["id"]: {"id": msg["id"], "code": str(msg.get("code", ""))[:12],
                                               "client": str(msg.get("client") or "An AI app")[:60],
                                               "ts_ms": _now_ms()}}
        elif t == "approve_cancel":
            with self._lock:
                self._approvals.pop(msg.get("id"), None)

    def _answer(self, msg: Dict[str, Any]) -> None:
        headers = msg.get("headers") if isinstance(msg.get("headers"), dict) else {}
        body = msg.get("body") if isinstance(msg.get("body"), str) else ""
        status, out_headers, text = self.handler.handle(str(msg.get("method") or ""), headers, body)
        client = self._client
        if client:
            client.send({"t": "http_result", "id": msg.get("id"), "status": status, "headers": out_headers,
                         "body": text})

    # what the window shows
    def status(self) -> Dict[str, Any]:
        since = int(self.store.get_meta(_META_SINCE) or 0)
        last = self.activity.last_used_ms()
        last_used = last if last and last >= since else None
        if not self.enabled:
            state = "off"
        elif self._conn_state == "online":
            state = "connected" if (self._grants or last_used) else "waiting"
        elif self._conn_state in ("offline", "rejected"):
            state = self._conn_state
        else:
            state = "connecting"
        with self._lock:
            for k in [k for k, v in self._approvals.items() if _now_ms() - v["ts_ms"] > APPROVAL_TTL_MS]:
                del self._approvals[k]
            pending = next(iter(self._approvals.values()), None)
        return {"enabled": self.enabled, "state": state, "message": self._conn_message,
                "url": self.connector_url() if self.enabled else None, "last_used_ms": last_used,
                "grants": self._grants, "keep_awake": self.keep_awake, "keeping_awake": self._keep_awake.active,
                "link_only": self.link_only, "approval": pending, "oauth": self.oauth_urls() if self.enabled else {},
                "activity": self.activity.entries(30), "connectors": dict(CONNECTOR_PAGES)}


# --- demo ---------------------------------------------------------------------------------------

class DemoRelayConnection:
    """Stands in for the relay in demo mode: Grok asks to connect, then searches.
    Everything runs locally; nothing touches the network."""

    def __init__(self, script: Optional[List[Tuple[float, Dict[str, Any]]]] = None) -> None:
        import queue
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        self.sent: List[Dict[str, Any]] = []
        self._closed = threading.Event()
        self._after_approve = [
            {"t": "grants", "n": 1},
            {"t": "http", "id": "d1", "method": "POST", "headers": {},
             "body": json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-06-18"}})},
            {"t": "http", "id": "d2", "method": "POST", "headers": {},
             "body": json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "list_items", "arguments": {"source": "gcal", "limit": 5}}})},
            {"t": "http", "id": "d3", "method": "POST", "headers": {},
             "body": json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "search", "arguments": {"query": "dinner"}}})},
        ]
        self._q.put(json.dumps({"t": "hello", "grants": 0}))
        timer = threading.Timer(2.5, lambda: self._q.put(json.dumps(
            {"t": "approve", "id": "demo-approval", "client": "Grok", "code": "4821"})))
        timer.daemon = True
        timer.start()

    def send(self, text: str) -> None:
        msg = json.loads(text)
        self.sent.append(msg)
        if msg.get("t") == "approve_result" and msg.get("ok"):
            for m in self._after_approve:
                self._q.put(json.dumps(m))

    def close(self) -> None:
        self._closed.set()
        self._q.put(None)

    def __iter__(self):
        while not self._closed.is_set():
            item = self._q.get()
            if item is None:
                return
            yield item


def demo_link(store: Store, data_dir: Path) -> GrokLink:
    return GrokLink(store, data_dir, secrets=MemorySecrets(),
                    connect=lambda url, secret: (time.sleep(0.6), DemoRelayConnection())[1],
                    keep_awake_runner=lambda: None)
