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
    s.set_meta("history_id", "1")
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
    assert r[0]["result"]["capabilities"] == {"tools": {"listChanged": False}}
    tools = r[1]["result"]["tools"]
    assert [t["name"] for t in tools] == ["search_mail", "read_thread", "list_messages"]
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
    store.set_meta("auth_state", "needs_reconnect")
    r = session(store, init(), rpc(2, "tools/call", {"name": "search_mail", "arguments": {"query": "rent"}}))[1]
    assert "Reconnect Gmail" in r["result"]["structuredContent"]["notice"]


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
