"""The MCP link (once "Use in Grok"): the Mac side of the relay (MCP over HTTP, the socket, activity, settings).

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


def test_email_aliases_are_on_the_grok_path_only_while_gmail_is_connected(store):
    h = MCPHttpHandler(store)
    _, _, body = h.handle("POST", {}, rpc("tools/list"))
    assert "search_email" not in {t["name"] for t in json.loads(body)["result"]["tools"]}
    _, _, body = h.handle("POST", {}, rpc("tools/call", {"name": "search_email", "arguments": {"query": "x"}}))
    assert json.loads(body)["error"]["code"] == -32602

    store.set_state("gmail", "account", "alex@example.org")
    _, _, body = h.handle("POST", {}, rpc("initialize", {"protocolVersion": "2025-06-18"}))
    instructions = json.loads(body)["result"]["instructions"]
    assert "read access to the user's email (Gmail" in instructions
    assert "prompt injection" not in instructions.lower()
    _, _, body = h.handle("POST", {}, rpc("tools/list"))
    tools = json.loads(body)["result"]["tools"]
    assert {t["name"] for t in tools} == {"search", "read", "list_items", "list_sources",
                                          "search_email", "read_email_thread"}
    search = next(t for t in tools if t["name"] == "search")
    assert "user's Gmail" in search["description"] and "inbox" in search["description"]
    assert "cannot send" in search["description"] and "draft" in search["description"]
    assert "label" in search["description"] and "delete" in search["description"]
    assert "[REDACTED:TYPE]" in search["description"] and "prompt injection" not in search["description"].lower()
    status, _, body = h.handle("POST", {"MCP-Protocol-Version": "2025-06-18"},
                               rpc("tools/call", {"name": "search_email", "arguments": {"query": "Nopa"}}))
    out = json.loads(body)["result"]
    assert status == 200 and out["isError"] is False
    assert out["structuredContent"]["results"]
    assert all(hit["source"] == "gmail" for hit in out["structuredContent"]["results"])

    store.set_state("gmail", "account", None)
    _, _, body = h.handle("POST", {}, rpc("tools/list"))
    assert "search_email" not in {t["name"] for t in json.loads(body)["result"]["tools"]}


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
    assert describe_query("search_email", {"query": "rent"}) == "“rent” · in gmail"
    assert describe_query("read_email_thread", {"thread_id": "t1"}) == "Opened a thread in gmail"
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
    assert link.status()["approval"]["client"] == "An AI app"  # no name given: don't assume Grok
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
        assert b.open_connectors("grok") == {"ok": True, "opened": False}  # no browser in demo mode
        assert b.open_connectors("claude") == {"ok": True, "opened": False}
        assert b.open_connectors("muse")["ok"] is False  # Muse is set up in chat; no page
        assert b.open_connectors("https://evil.example")["ok"] is False
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


def test_status_lists_connector_pages(link):
    assert link.status()["connectors"] == {"grok": "https://grok.com/connectors",
                                           "claude": "https://claude.ai/customize/connectors"}


def test_approval_shows_the_name_the_app_registered(link, relay):
    link.enable()
    wait(lambda: link.status()["state"] == "waiting")
    relay.send({"t": "approve", "id": "c1", "client": "Claude", "code": "1234"})
    wait(lambda: link.status()["approval"] is not None)
    assert link.status()["approval"]["client"] == "Claude"
    relay.send({"t": "approve_cancel", "id": "c1"})
    relay.send({"t": "approve", "id": "c2", "client": "x" * 200, "code": "5678"})
    wait(lambda: (link.status()["approval"] or {}).get("id") == "c2")
    assert len(link.status()["approval"]["client"]) == 60


def test_oauth_help_uses_the_neutral_client_id(link):
    link.enable()
    assert link.status()["oauth"]["client_id"] == "safecontext"


def _log(link):
    return [(e["kind"], e["outcome"], e.get("client", ""), e.get("note", "")) for e in reversed(link.permlog.entries())]


def test_permission_log_records_answers_and_settings_but_no_codes(link, relay, tmp_path):
    link.enable()
    wait(lambda: link.status()["state"] == "waiting")
    relay.send({"t": "approve", "id": "p1", "client": "Grok", "code": "4821"})
    wait(lambda: link.status()["approval"] is not None)
    link.approve("p1", True)
    relay.send({"t": "approve", "id": "p2", "client": "Claude", "code": "1111"})
    wait(lambda: (link.status()["approval"] or {}).get("id") == "p2")
    link.approve("p2", False)
    relay.send({"t": "approve", "id": "p3", "client": "Grok", "code": "2222"})
    wait(lambda: (link.status()["approval"] or {}).get("id") == "p3")
    relay.send({"t": "approve_cancel", "id": "p3"})
    wait(lambda: link.status()["approval"] is None)
    relay.send({"t": "grants", "n": 1})
    wait(lambda: link.status()["grants"] == 1)
    link.set_link_only(True)
    link.set_link_only(True)  # no change, no entry
    link.set_keep_awake(True)
    link.disable()
    assert _log(link) == [
        ("link", "on", "", ""), ("connect", "approved", "Grok", ""), ("connect", "denied", "Claude", ""),
        ("connect", "cancelled", "Grok", ""), ("signins", "", "", "0 → 1"), ("link_only", "on", "", ""),
        ("keep_awake", "on", "", ""), ("link", "off", "", "")]
    saved = (tmp_path / "permission_log.json").read_text()
    assert "4821" not in saved and "1111" not in saved and link.connector_url() not in saved
    assert oct((tmp_path / "permission_log.json").stat().st_mode & 0o777) == "0o600"


def test_permission_log_records_expired_and_replaced_requests(link, relay, monkeypatch):
    link.enable()
    wait(lambda: link.status()["state"] == "waiting")
    relay.send({"t": "approve", "id": "e1", "client": "Grok", "code": "1"})
    wait(lambda: link.status()["approval"] is not None)
    relay.send({"t": "approve", "id": "e2", "client": "Grok", "code": "2"})  # replaces e1 unanswered
    wait(lambda: link.status()["approval"]["id"] == "e2")
    later = grok_link._now_ms() + grok_link.APPROVAL_TTL_MS + 1000
    monkeypatch.setattr(grok_link, "_now_ms", lambda: later)
    assert link.status()["approval"] is None
    assert [e[:2] for e in _log(link)][-2:] == [("connect", "expired"), ("connect", "expired")]


def test_regenerate_is_logged(link, relay):
    link.enable()
    wait(lambda: link.status()["state"] == "waiting")
    relay.secret = None
    link.regenerate()
    assert _log(link)[-1][:2] == ("link", "regenerated")


# --- per-app sign-ins (relay with sign-in lists) ---------------------------------------------------

def _g(gid, client, created=1_700_000_000_000, last=None):
    return {"id": gid, "client": client, "created_ms": created, "last_used_ms": last}


def _online(link):
    link.enable()
    wait(lambda: link.status()["state"] == "waiting")


def test_old_relay_gives_only_a_count(link, relay):
    _online(link)
    assert link.status()["signins"] is None  # hello had no list
    relay.send({"t": "grants", "n": 2})
    wait(lambda: link.status()["grants"] == 2)
    assert link.status()["signins"] is None and link.revoke("anything") is False
    assert _log(link)[-1] == ("signins", "", "", "0 → 2")


def test_new_relay_lists_sign_ins(link, relay):
    _online(link)
    relay.send({"t": "hello", "grants": 2, "list": [_g("g1", "Grok", last=1_700_000_100_000),
                                                    _g("g2", ""), "junk", {"id": 5}, {"id": "x" * 65}]})
    wait(lambda: link.status()["signins"] is not None)
    st = link.status()
    assert st["grants"] == 2 and [g["id"] for g in st["signins"]] == ["g1", "g2"]  # bad entries dropped
    assert st["signins"][0] == _g("g1", "Grok", last=1_700_000_100_000)
    assert st["signins"][1]["client"] == "" and st["signins"][1]["last_used_ms"] is None
    relay.send({"t": "grants", "n": 3, "list": [_g("g1", "Grok"), _g("g2", ""), _g("g3", "Claude " * 20)]})
    wait(lambda: link.status()["grants"] == 3)
    assert len(link.status()["signins"][2]["client"]) == 60
    assert not [e for e in _log(link) if e[0] == "signins"]  # grant events say it instead


def test_grant_events_are_logged_by_app_without_ids(link, relay, tmp_path):
    _online(link)
    relay.send({"t": "hello", "grants": 1, "list": [_g("gid-secret-1", "Grok")]})
    for kind in ("created", "refreshed", "refreshed", "expired", "bogus"):
        relay.send({"t": "grant_event", "kind": kind, "id": "gid-secret-1", "client": "Grok"})
    relay.send({"t": "grant_event", "kind": "created", "id": "gid-secret-2", "client": ""})
    wait(lambda: len([e for e in _log(link) if e[0] == "signin"]) == 4)
    assert [e for e in _log(link) if e[0] == "signin"] == [
        ("signin", "created", "Grok", ""), ("signin", "refreshed", "Grok", ""),  # second renewal not logged
        ("signin", "expired", "Grok", ""), ("signin", "created", "", "")]
    assert link.status()["signins"] == []  # the expired one left the list
    texts = [e["text"] for e in link.permlog.entries()]
    assert "An unknown app signed in through your link" in texts and "Grok's sign-in expired" in texts
    assert "gid-secret" not in (tmp_path / "permission_log.json").read_text()


def test_revoke_round_trip(link, relay, tmp_path):
    _online(link)
    relay.send({"t": "hello", "grants": 2, "list": [_g("g1", "Grok"), _g("g2", "Claude")]})
    wait(lambda: link.status()["grants"] == 2)
    assert link.revoke("nope") is False  # not in the list: nothing sent
    assert link.revoke("g1") is True
    assert relay.wait_for("revoke") == {"t": "revoke", "id": "g1"}
    st = link.status()
    assert [g["id"] for g in st["signins"]] == ["g2"] and st["grants"] == 1  # gone at once
    relay.send({"t": "grant_event", "kind": "revoked", "id": "g1", "client": "Grok"})
    relay.send({"t": "grants", "n": 1, "list": [_g("g2", "Claude")]})
    wait(lambda: ("signin", "revoked", "Grok", "") in _log(link))
    assert link.permlog.entries()[0]["text"] == "You signed Grok out"
    assert [g["id"] for g in link.status()["signins"]] == ["g2"]
    assert "g1" not in (tmp_path / "permission_log.json").read_text()


def test_revoke_of_an_id_the_relay_no_longer_has(link, relay):
    _online(link)
    relay.send({"t": "hello", "grants": 1, "list": [_g("g1", "Grok")]})
    wait(lambda: link.status()["grants"] == 1)
    assert link.revoke("g1")
    relay.wait_for("revoke")
    relay.send({"t": "grants", "n": 1, "list": [_g("g1", "Grok")]})  # it says what it really has
    wait(lambda: link.status()["grants"] == 1)
    assert not [e for e in _log(link) if e[0] == "signin"]


def test_requests_say_which_app_asked(link, relay, tmp_path):
    _online(link)
    relay.send({"t": "hello", "grants": 1, "list": [_g("g1", "Grok", last=1_700_000_000_000)]})
    wait(lambda: link.status()["grants"] == 1)
    for i in range(3):
        relay.send({"t": "http", "id": f"r{i}", "method": "POST", "headers": {}, "client": "Grok", "grant": "g1",
                    "body": rpc("tools/call", {"name": "search", "arguments": {"query": "Nopa"}})})
        relay.wait_for("http_result")
    relay.send({"t": "http", "id": "lo", "method": "POST", "headers": {}, "client": "", "grant": None,
                "body": rpc("tools/call", {"name": "search", "arguments": {"query": "x"}})})
    relay.wait_for("http_result")
    st = link.status()
    assert st["activity"][0].get("client") is None and st["activity"][1]["client"] == "Grok"
    assert st["signins"][0]["last_used_ms"] > 1_700_000_000_000  # seen here, newer than the relay's hourly value
    assert [e for e in _log(link) if e[0] == "used"] == [("used", "", "Grok", "")]  # once a day, link-only not named
    saved = (tmp_path / "permission_log.json").read_text()
    assert "Nopa" not in saved and "g1" not in saved
    assert "g1" not in (tmp_path / "grok_activity.json").read_text()


def test_used_is_not_logged_twice_after_a_restart(store, tmp_path, relay):
    from safecontext.permlog import PermissionLog
    PermissionLog(tmp_path / "permission_log.json").record("used", client="Grok")
    g = GrokLink(store, tmp_path, base_url=relay.url, keep_awake_runner=lambda: None, backoff=(0.05, 0.2))
    try:
        g._log_use("Grok")
        g._log_use("Claude")
        assert [e["client"] for e in g.permlog.entries() if e["kind"] == "used"] == ["Claude", "Grok"]
    finally:
        g.shutdown()


def test_backend_revoke(backend, relay):
    backend.grok_enable()
    wait(lambda: backend.grok_status()["state"] == "waiting")
    relay.send({"t": "hello", "grants": 1, "list": [_g("g1", "Grok")]})
    wait(lambda: backend.grok_status()["grants"] == 1)
    assert backend.grok_revoke("zzz")["ok"] is False
    r = backend.grok_revoke("g1")
    assert r["ok"] and r["signins"] == []
    assert relay.wait_for("revoke")["id"] == "g1"


def test_demo_relay_signs_in_and_revokes(tmp_path):
    from safecontext.app.demo import demo_backend_kwargs
    b = Backend(**demo_backend_kwargs(tmp_path / "demo"), auto_sync=False)
    try:
        b.grok_enable()
        wait(lambda: b.grok_status()["signins"] is not None, timeout=8)
        assert [g["client"] for g in b.grok_status()["signins"]] == ["", "Claude"]
        review = b.review()
        sc = next(g for g in review["groups"] if g["id"] == "safecontext")
        rows = {c["title"]: c for c in sc["checks"] if c["grant"]}
        assert rows["Unknown app: not used for 41 days"]["colour"] == "yellow"
        assert rows["Claude is signed in"]["colour"] == "green"
        wait(lambda: (b.grok_status().get("approval") or {}).get("code") == "4821", timeout=8)
        b.grok_approve("demo-approval", True)
        wait(lambda: [g["client"] for g in b.grok_status()["signins"]] == ["", "Claude", "Grok"])
        wait(lambda: len(b.grok_status()["activity"]) == 2)
        assert b.grok_status()["activity"][0]["client"] == "Grok"
        assert b.grok_revoke("demo-old")["ok"]
        wait(lambda: any(e["kind"] == "signin" and e["outcome"] == "revoked" and e["ts_ms"] > time.time() * 1000 - 60_000
                         for e in b.permission_log()["entries"]))
        assert [g["client"] for g in b.grok_status()["signins"]] == ["Claude", "Grok"]
        texts = [e["text"] for e in b.permission_log()["entries"][:8]]
        assert "Grok signed in through your link" in texts and "Claude renewed its sign-in" in texts
        assert "You signed an unknown app out" in texts
    finally:
        b._shutdown()
