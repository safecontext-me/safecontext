"""Use in Grok: the Mac side of the relay (MCP over HTTP, the socket, activity, settings).

The relay is played by a local websockets server on 127.0.0.1; nothing here
touches the network, the real Keychain or the real data folder."""
import json
import os
import queue
import stat
import threading
import time

import pytest
from websockets.sync.server import serve

from safecontext import grok_link
from safecontext.app.backend import Backend
from safecontext.app.demo import DEMO_ACCOUNT, make_fake_gmail
from safecontext.grok_link import (ActivityLog, GrokLink, KeepAwake, MCPHttpHandler, MemorySecrets,
                                   describe_query)
from safecontext.demo.synthetic import msg
from safecontext.scrub import scrub
from safecontext.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "mail.db", account="alex@example.org")
    s.put_many([scrub(msg(1, frm="Jamie <jamie@example.com>", subject="Dinner Saturday",
                          text="Booked Nopa for 7pm. My card is 4111 1111 1111 1111 if you need it."))])
    s.set_state("gmail", "history_id", "1")
    yield s
    s.close()


def rpc(method, params=None, id_=1):
    m = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        m["id"] = id_
    if params is not None:
        m["params"] = params
    return json.dumps(m)


# --- MCP Streamable HTTP ----------------------------------------------------------------------

def test_initialize_and_only_read_only_primary_tools(store):
    h = MCPHttpHandler(store)
    status, headers, body = h.handle("POST", {}, rpc("initialize", {"protocolVersion": "2025-06-18"}))
    assert status == 200 and headers["content-type"] == "application/json"
    assert json.loads(body)["result"]["protocolVersion"] == "2025-06-18"
    _, _, body = h.handle("POST", {}, rpc("tools/list"))
    tools = json.loads(body)["result"]["tools"]
    assert {t["name"] for t in tools} == {"search", "read", "list_items", "list_sources"}
    assert all(t["annotations"]["readOnlyHint"] for t in tools)


def test_tool_call_is_scrubbed_and_logged_without_content(store, tmp_path):
    activity = ActivityLog(tmp_path / "grok_activity.json")
    h = MCPHttpHandler(store, activity)
    status, _, body = h.handle("POST", {"MCP-Protocol-Version": "2025-06-18"},
                               rpc("tools/call", {"name": "search", "arguments": {"query": "Nopa"}}))
    out = json.loads(body)["result"]
    assert status == 200 and not out["isError"] and out["structuredContent"]["count"] >= 1
    assert "4111 1111" not in body  # the store only ever held scrubbed text
    (entry,) = activity.entries()
    assert entry["tool"] == "search" and entry["query"] == "“Nopa”" and entry["results"] >= 1
    raw = (tmp_path / "grok_activity.json").read_text()
    assert "Booked" not in raw and "Dinner" not in raw  # no result content kept
    assert stat.S_IMODE(os.stat(tmp_path / "grok_activity.json").st_mode) == 0o600
    assert ActivityLog(tmp_path / "grok_activity.json").entries()[0]["query"] == "“Nopa”"  # survives restart


def test_aliases_and_other_methods_are_refused(store):
    h = MCPHttpHandler(store)
    _, _, body = h.handle("POST", {}, rpc("tools/call", {"name": "search_mail", "arguments": {"query": "x"}}))
    assert json.loads(body)["error"]["code"] == -32602
    for method in ("resources/list", "prompts/list", "sampling/createMessage", "logging/setLevel"):
        _, _, body = h.handle("POST", {}, rpc(method))
        assert json.loads(body)["error"]["code"] == -32601


def test_http_semantics(store):
    h = MCPHttpHandler(store)
    assert h.handle("POST", {}, rpc("notifications/initialized", id_=None))[0] == 202
    assert h.handle("GET", {}, "")[0] == 405
    assert h.handle("DELETE", {}, "")[0] == 405
    assert h.handle("POST", {}, "{not json")[0] == 400
    assert h.handle("POST", {}, "[]")[0] == 400
    assert h.handle("POST", {}, "x" * (grok_link.MAX_REQUEST_BYTES + 1))[0] == 413
    status, _, body = h.handle("POST", {}, json.dumps([json.loads(rpc("ping", id_=1)), json.loads(rpc("ping", id_=2))]))
    assert status == 200 and [r["id"] for r in json.loads(body)] == [1, 2]


def test_missing_protocol_header_still_works(store):
    """Grok has been seen leaving out MCP-Protocol-Version on reused sessions."""
    status, _, body = MCPHttpHandler(store).handle("POST", {}, rpc("tools/call", {"name": "list_sources",
                                                                                  "arguments": {}}))
    assert status == 200 and "result" in json.loads(body)


def test_oversized_result_becomes_an_error(store, monkeypatch):
    monkeypatch.setattr(grok_link, "MAX_RESPONSE_BYTES", 100)
    _, _, body = MCPHttpHandler(store).handle("POST", {}, rpc("tools/call", {"name": "search",
                                                                              "arguments": {"query": "Nopa"}}))
    assert "too large" in json.loads(body)["error"]["message"]


def test_describe_query():
    assert describe_query("search", {"query": "rent", "sender": "@bank.com"}) == "“rent” · from @bank.com"
    assert describe_query("list_items", {"source": "gcal", "after": "2026-01-01"}) == \
        "Listed items · in gcal, after 2026-01-01"
    assert describe_query("read", {"thread_id": "t1"}) == "Opened a thread"
    assert len(describe_query("search", {"query": "x" * 500})) <= 160


# --- a fake relay -------------------------------------------------------------------------------

class FakeRelay:
    """Accepts the Mac's socket like the real Durable Object: checks the Bearer secret
    (first secret seen wins), says hello, then lets the test drive it."""

    def __init__(self):
        self.secret = None
        self.received = queue.Queue()
        self.conns = []
        self.reject_all = False
        self.paths = []

        def process_request(conn, request):
            auth = request.headers.get("Authorization", "")
            if self.reject_all or not auth.startswith("Bearer "):
                return conn.respond(401, "no")
            if self.secret is None:
                self.secret = auth[7:]
            elif auth[7:] != self.secret:
                return conn.respond(401, "no")
            self.paths.append(request.path)

        def handler(ws):
            self.conns.append(ws)
            ws.send(json.dumps({"t": "hello", "grants": 0}))
            try:
                for raw in ws:
                    self.received.put(json.loads(raw))
            except Exception:
                pass

        self.server = serve(handler, "127.0.0.1", 0, process_request=process_request)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def send(self, msg):
        self.conns[-1].send(json.dumps(msg))

    def wait_for(self, t, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = self.received.get(timeout=0.2)
            except queue.Empty:
                continue
            if m.get("t") == t:
                return m
        raise AssertionError(f"relay never got {t!r}")

    def close(self):
        self.server.shutdown()


@pytest.fixture
def relay():
    r = FakeRelay()
    yield r
    r.close()


def wait(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    raise AssertionError("timed out")


@pytest.fixture
def link(store, tmp_path, relay):
    g = GrokLink(store, tmp_path, base_url=relay.url, keep_awake_runner=lambda: None, backoff=(0.05, 0.2))
    yield g
    g.shutdown()


def test_off_by_default(link):
    st = link.status()
    assert st["state"] == "off" and not st["enabled"] and st["url"] is None


def test_enable_connects_and_answers_grok(link, relay, fake_keyring):
    url = link.enable()
    assert grok_link.KEYRING_SERVICE == "SafeContext Grok (Dev)"  # tests run from a checkout
    ids = json.loads(fake_keyring.items[(grok_link.KEYRING_SERVICE, "relay-link")])  # in the (fake) Keychain
    assert url == f"{relay.url}/g/{ids['link_id']}/mcp" and len(ids["link_id"]) == 43
    assert ids["device_secret"] != ids["link_id"]
    wait(lambda: link.status()["state"] == "waiting")
    assert relay.secret == ids["device_secret"] and relay.paths == [f"/g/{ids['link_id']}/connect"]
    assert relay.wait_for("settings")["link_only"] is False

    relay.send({"t": "http", "id": "r1", "method": "POST", "headers": {"mcp-protocol-version": "2025-06-18"},
                "body": rpc("tools/call", {"name": "search", "arguments": {"query": "Nopa"}})})
    res = relay.wait_for("http_result")
    assert res["id"] == "r1" and res["status"] == 200 and "Nopa" in res["body"]
    st = link.status()
    assert st["state"] == "connected" and st["last_used_ms"] and st["activity"][0]["tool"] == "search"


def test_approval_round_trip(link, relay):
    link.enable()
    wait(lambda: link.status()["state"] == "waiting")
    relay.send({"t": "approve", "id": "a1", "client": "Grok", "code": "4821"})
    wait(lambda: link.status()["approval"] is not None)
    assert link.status()["approval"]["code"] == "4821"
    assert link.approve("a1", True)
    assert relay.wait_for("approve_result") == {"t": "approve_result", "id": "a1", "ok": True}
    assert link.status()["approval"] is None
    assert not link.approve("a1", True)  # only once
    relay.send({"t": "grants", "n": 1})
    wait(lambda: link.status()["state"] == "connected")


def test_approval_can_be_cancelled_by_relay(link, relay):
    link.enable()
    wait(lambda: link.status()["state"] == "waiting")
    relay.send({"t": "approve", "id": "a2", "code": "1"})
    wait(lambda: link.status()["approval"] is not None)
    relay.send({"t": "approve_cancel", "id": "a2"})
    wait(lambda: link.status()["approval"] is None)


def test_regenerate_revokes_and_changes_link(link, relay):
    old = link.enable()
    wait(lambda: link.status()["state"] == "waiting")
    relay.secret = None  # the new link is a different Durable Object, which hasn't met this Mac
    new = link.regenerate()
    assert relay.wait_for("revoke_all") == {"t": "revoke_all"}
    assert new != old
    wait(lambda: len(relay.paths) == 2 and link.status()["state"] == "waiting")
    assert relay.paths[1].removesuffix("/connect") in new


def test_disable_closes_the_socket(link, relay):
    link.enable()
    wait(lambda: link.status()["state"] == "waiting")
    link.disable()
    assert link.status()["state"] == "off"
    wait(lambda: relay.conns[-1].state.name == "CLOSED")


def test_reconnects_after_the_relay_drops(link, relay):
    link.enable()
    wait(lambda: link.status()["state"] == "waiting")
    relay.conns[-1].close()
    wait(lambda: len(relay.conns) == 2 and link.status()["state"] == "waiting")


def test_rejected_secret_is_reported(link, relay):
    relay.reject_all = True
    link.enable()
    wait(lambda: link.status()["state"] == "rejected")
    assert "Regenerate" in link.status()["message"]


def test_unreachable_relay(store, tmp_path):
    g = GrokLink(store, tmp_path, base_url="http://127.0.0.1:9", keep_awake_runner=lambda: None,
                 backoff=(0.05, 0.1))
    try:
        g.enable()
        wait(lambda: g.status()["state"] == "offline")
        assert "relay" in g.status()["message"]
    finally:
        g.shutdown()


def test_link_only_setting_is_sent(link, relay):
    link.enable()
    relay.wait_for("settings")
    link.set_link_only(True)
    assert relay.wait_for("settings")["link_only"] is True
    assert link.status()["link_only"]


def test_enabled_link_reconnects_on_launch(store, tmp_path, relay):
    secrets = MemorySecrets()
    g = GrokLink(store, tmp_path, secrets=secrets, base_url=relay.url, keep_awake_runner=lambda: None)
    url = g.enable()
    g.shutdown()
    g2 = GrokLink(store, tmp_path, secrets=secrets, base_url=relay.url, keep_awake_runner=lambda: None)
    try:
        g2.start()
        wait(lambda: g2.status()["state"] == "waiting")
        assert g2.connector_url() == url  # same link after a restart
    finally:
        g2.shutdown()


# --- keep awake ---------------------------------------------------------------------------------

class FakeProc:
    def __init__(self):
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False


def test_keep_awake_only_while_on(store, tmp_path, relay):
    procs = []
    g = GrokLink(store, tmp_path, base_url=relay.url, keep_awake_runner=lambda: procs.append(FakeProc()) or procs[-1])
    try:
        g.set_keep_awake(True)
        assert procs == []          # Grok is off: nothing to stay awake for
        g.enable()
        assert len(procs) == 1 and g.status()["keeping_awake"]
        g.set_keep_awake(False)
        assert not procs[0].alive
        g.set_keep_awake(True)
        g.disable()
        assert not procs[1].alive and not g.status()["keeping_awake"]
    finally:
        g.shutdown()


def test_keep_awake_uses_caffeinate_on_power_only(monkeypatch):
    calls = []
    monkeypatch.setattr(grok_link.subprocess, "Popen", lambda args, **kw: calls.append(args) or FakeProc())
    k = KeepAwake()
    k.set(True)
    assert calls[0][:2] == ["/usr/bin/caffeinate", "-s"] and "-w" in calls[0]


# --- the app window -----------------------------------------------------------------------------

@pytest.fixture
def backend(tmp_path, relay, fake_keyring):
    gmail = make_fake_gmail()
    fake_keyring.items[("SafeContext xAI (Dev)", "api_key")] = "xai-left-over-0123456789"
    b = Backend(tmp_path / "data", gmail_factory=lambda account, mode: gmail,
                connect_fn=lambda mode, email: DEMO_ACCOUNT, auto_sync=False,
                grok_link=lambda st, d: GrokLink(st, d, base_url=relay.url, keep_awake_runner=lambda: None))
    yield b
    b._shutdown()


def test_backend_removes_old_xai_key(backend, fake_keyring):
    assert ("SafeContext xAI (Dev)", "api_key") not in fake_keyring.items


def test_backend_grok_flow(backend, relay, monkeypatch):
    copied = []
    monkeypatch.setattr(backend, "_copy", lambda text: copied.append(text) or True)
    assert backend.grok_status()["state"] == "off"
    r = backend.grok_enable()
    assert r["ok"] and r["copied"] and copied == [r["url"]] and "/g/" in r["url"]
    wait(lambda: backend.grok_status()["state"] == "waiting")
    assert backend.grok_copy_link()["url"] == r["url"]
    assert backend.grok_approve("nope")["ok"] is False
    r2 = backend.grok_regenerate()
    assert r2["url"] != r["url"] and copied[-1] == r2["url"]
    assert backend.grok_disable()["state"] == "off"
    assert backend.grok_copy_link()["ok"] is False


def test_status_has_no_ask_fields(backend):
    s = backend.status()
    assert "has_xai_key" not in s and "model" not in s
    assert not hasattr(backend, "ask")


def test_demo_link_walks_through_approval(tmp_path):
    from safecontext.app.demo import demo_backend_kwargs
    b = Backend(**demo_backend_kwargs(tmp_path / "demo"), auto_sync=False)
    try:
        assert b.open_grok_connectors() == {"ok": True, "opened": False}  # no browser in demo mode
        r = b.grok_enable()
        assert r["ok"] and r["copied"] is False  # demo leaves the clipboard to the window
        wait(lambda: (b.grok_status().get("approval") or {}).get("code") == "4821", timeout=8)
        assert b.grok_approve("demo-approval", True)["ok"]
        wait(lambda: b.grok_status()["state"] == "connected" and len(b.grok_status()["activity"]) == 2)
    finally:
        b._shutdown()


def test_stale_approval_disappears(link, relay, monkeypatch):
    link.enable()
    wait(lambda: link.status()["state"] == "waiting")
    relay.send({"t": "approve", "id": "a3", "code": "7"})
    wait(lambda: link.status()["approval"] is not None)
    later = grok_link._now_ms() + grok_link.APPROVAL_TTL_MS + 1000
    monkeypatch.setattr(grok_link, "_now_ms", lambda: later)
    assert link.status()["approval"] is None
