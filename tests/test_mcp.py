"""MCP server: protocol behaviour in-process, plus one real stdio session."""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from safecontext.mcp_server import MCPServer
from safecontext.scrub import scrub
from safecontext.store import Store
from safecontext.demo.synthetic import mailbox

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data" / "mail.db", account="alex@example.org")
    s.put_many(scrub(m) for m in mailbox())
    s.set_state("gmail", "history_id", "1")
    yield s
    s.close()


def rpc(id_, method, params=None):
    m = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        m["params"] = params
    return m


def session(store, *messages):
    srv = MCPServer(store)
    inp = io.StringIO("".join(json.dumps(m) + "\n" if not isinstance(m, str) else m + "\n" for m in messages))
    out = io.StringIO()
    srv.serve(inp, out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def init(version="2025-06-18"):
    return rpc(1, "initialize", {"protocolVersion": version, "capabilities": {},
                                 "clientInfo": {"name": "test", "version": "0"}})


def test_initialize_and_list(store):
    r = session(store, init(), {"jsonrpc": "2.0", "method": "notifications/initialized"}, rpc(2, "tools/list"))
    assert len(r) == 2  # the notification gets no reply
    assert r[0]["result"]["protocolVersion"] == "2025-06-18"
    assert r[0]["result"]["capabilities"] == {"tools": {"listChanged": True}}
    assert "prompt injection" not in r[0]["result"]["instructions"].lower()
    # Mail is stored, but Gmail has no account, so this is not a live mailbox connection.
    assert "read access to the user's email" not in r[0]["result"]["instructions"]
    tools = r[1]["result"]["tools"]
    assert [t["name"] for t in tools] == ["search", "read", "list_items", "list_labels", "list_sources",
                                          "search_mail", "read_thread", "list_messages"]  # v1 names still work
    assert "search_email" not in [t["name"] for t in tools]
    search = next(t for t in tools if t["name"] == "search")
    assert "Gmail" in search["description"] and "not connected" in search["description"]
    assert "cannot send" in search["description"] and "[REDACTED:TYPE]" in search["description"]
    assert all(t["inputSchema"]["type"] == "object" and t["annotations"]["readOnlyHint"] for t in tools)


def test_version_negotiation(store):
    assert session(store, init("2024-11-05"))[0]["result"]["protocolVersion"] == "2024-11-05"
    assert session(store, init("1999-01-01"))[0]["result"]["protocolVersion"] == "2025-06-18"


def test_call_returns_text_and_structured_content(store):
    r = session(store, init(), rpc(2, "tools/call", {"name": "search_mail", "arguments": {"query": "rent"}}))[1]
    res = r["result"]
    assert res["isError"] is False
    data = json.loads(res["content"][0]["text"])
    assert data == res["structuredContent"]
    hit = data["results"][0]
    assert hit["date"] and hit["sender"] and hit["gmail_link"].startswith("https://mail.google.com/")


def test_no_structured_content_on_old_protocol(store):
    r = session(store, init("2025-03-26"), rpc(2, "tools/call", {"name": "list_messages", "arguments": {}}))[1]
    assert "structuredContent" not in r["result"]


def test_tool_error_is_reported_in_result(store):
    r = session(store, init(), rpc(2, "tools/call", {"name": "search_mail", "arguments": {}}))[1]
    assert r["result"]["isError"] is True and "required" in r["result"]["content"][0]["text"]


def test_protocol_errors(store):
    r = session(store, init(), rpc(2, "tools/call", {"name": "rm_rf"}), rpc(3, "resources/list"),
                "{not json", rpc(4, "ping"), [1, 2])
    assert r[1]["error"]["code"] == -32602
    assert r[2]["error"]["code"] == -32601
    assert r[3]["error"]["code"] == -32700 and r[3]["id"] is None
    assert r[4]["result"] == {}
    assert r[5]["error"]["code"] == -32600


def test_reconnect_notice(store):
    store.set_state("gmail", "auth_state", "needs_reconnect")
    r = session(store, init(), rpc(2, "tools/call", {"name": "search_mail", "arguments": {"query": "rent"}}))[1]
    assert "Reconnect Gmail" in r["result"]["structuredContent"]["notice"]


def test_instructions_and_email_aliases_when_gmail_connects(store):
    store.set_state("gmail", "account", "alex@example.org")
    init_result = session(store, init())[0]["result"]
    text = init_result["instructions"]
    assert "read access to the user's email (Gmail" in text
    assert "cannot send, draft, label, or delete" in text
    assert "prompt injection" not in text.lower()
    tools = session(store, init(), rpc(2, "tools/list"))[1]["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names[-2:] == ["search_email", "read_email_thread"]
    kept, _ = store.counts("gmail")
    search = next(t for t in tools if t["name"] == "search")
    assert f"Gmail ({kept} emails)" in search["description"]
    assert "inbox, emails, and messages" in search["description"]
    assert "not connected" not in search["description"]
    for tool in tools:
        assert "cannot send" in tool["description"] and "draft" in tool["description"]
        assert "label" in tool["description"] and "delete" in tool["description"]
        assert "prompt injection" not in tool["description"].lower()
    called = session(store, init(), rpc(2, "tools/call", {"name": "search_email",
                                                          "arguments": {"query": "rent"}}))[1]
    hits = called["result"]["structuredContent"]["results"]
    assert hits and all(h["source"] == "gmail" for h in hits)


def test_stdio_notifies_when_gmail_connects_and_disconnects(store):
    """The stdio loop can only notice a change when the client sends another message."""
    def script():
        yield json.dumps(init()) + "\n"
        yield json.dumps(rpc(2, "tools/list")) + "\n"
        store.set_state("gmail", "account", "alex@example.org")
        yield json.dumps(rpc(3, "tools/list")) + "\n"
        store.set_state("gmail", "account", None)
        yield json.dumps(rpc(4, "ping")) + "\n"

    out = io.StringIO()
    MCPServer(store).serve(script(), out)
    parsed = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [m.get("id") for m in parsed] == [1, 2, None, 3, None, 4]
    assert parsed[2]["method"] == "notifications/tools/list_changed"
    assert parsed[4]["method"] == "notifications/tools/list_changed"
    assert "search_email" not in [t["name"] for t in parsed[1]["result"]["tools"]]
    assert "search_email" in [t["name"] for t in parsed[3]["result"]["tools"]]
    assert parsed[5]["result"] == {}


def test_real_stdio_process(store, tmp_path):
    msgs = [init(), {"jsonrpc": "2.0", "method": "notifications/initialized"},
            rpc(2, "tools/call", {"name": "read_thread", "arguments": {"thread_id": "t0004"}})]
    env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}", "PYTHONWARNINGS": "ignore"}
    proc = subprocess.run([sys.executable, "-m", "safecontext", "--data-dir", str(tmp_path / "data"), "mcp"],
                          input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True,
                          env=env, timeout=60)
    lines = proc.stdout.splitlines()
    assert len(lines) == 2, proc.stderr  # stdout holds protocol messages only
    body = json.loads(lines[1])["result"]["structuredContent"]["messages"][0]["body"]
    assert "[REDACTED:DATE_OF_BIRTH]" in body and "[REDACTED:SSN]" in body and "1985" not in body
