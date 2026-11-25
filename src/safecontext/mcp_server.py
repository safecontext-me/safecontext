"""MCP server over stdio exposing the same read-only tools (every connected
source: Gmail, Google Calendar, ...).

Implemented directly on the MCP JSON-RPC protocol (newline-delimited
messages on stdin/stdout) so it has no extra dependencies. The official
`mcp` SDK needs Python 3.10+; the packaged app can switch to it later
without changing the tools.

Clients run it as:  python -m safecontext mcp
stdout carries protocol messages only; diagnostics go to stderr.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, List, Optional, TextIO

from . import __version__
from .store import Store
from .tools import instructions_for, run_tool, tools_for

SUPPORTED_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]


def _ok(id_: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


class MCPServer:
    """`tools` limits which tools are listed and callable. None means `tools_for(store)`
    on every request. A callable is called with the store each time (the Grok path
    passes `grok_tools`). A list is used as given.

    `on_call(name, args, result)` is told about every tool call.

    `listChanged` is true. The stdio loop emits `notifications/tools/list_changed`
    when the advertised names or descriptions change between messages (a source
    connecting or disconnecting). It can only do that when the client sends
    another message, because the loop blocks on stdin.
    """

    def __init__(self, store: Store, tools: Any = None,
                 on_call: Optional[Callable[[str, Dict[str, Any], Dict[str, Any]], None]] = None) -> None:
        self.store = store
        self._tools = tools
        self.on_call = on_call
        self.protocol_version: Optional[str] = None
        self._tool_sig: Optional[tuple] = None

    def current_tools(self) -> List[Dict[str, Any]]:
        if self._tools is None:
            return tools_for(self.store)
        if callable(self._tools):
            return self._tools(self.store)
        return self._tools

    def take_list_changed(self) -> Optional[Dict[str, Any]]:
        """A list-changed notification when the tool list changed since the last check.

        The first check only records the signature, so a session does not start
        with a notification for tools the client has not been shown yet.
        """
        sig = tuple((t["name"], t["description"]) for t in self.current_tools())
        previous = self._tool_sig
        self._tool_sig = sig
        if previous is not None and sig != previous:
            return {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
        return None

    def handle(self, msg: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                return None  # a response to something we never send; ignore
            return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
        method, id_ = msg["method"], msg.get("id")
        params = msg.get("params") or {}
        is_notification = "id" not in msg

        if method == "initialize":
            requested = params.get("protocolVersion")
            self.protocol_version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            return _ok(id_, {
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "safecontext", "title": "SafeContext: private context layer", "version": __version__},
                "instructions": instructions_for(self.store),
            })
        if is_notification:
            return None  # notifications/initialized, cancelled, etc.
        if method == "ping":
            return _ok(id_, {})
        if method == "tools/list":
            return _ok(id_, {"tools": [
                {"name": t["name"], "title": t["name"].replace("_", " ").capitalize(),
                 "description": t["description"], "inputSchema": t["parameters"],
                 "annotations": {"readOnlyHint": True, "openWorldHint": False}}
                for t in self.current_tools()]})
        if method == "tools/call":
            name = params.get("name")
            if name not in {t["name"] for t in self.current_tools()}:
                return _err(id_, -32602, f"Unknown tool: {name}")
            args = params.get("arguments") or {}
            result = run_tool(self.store, name, args)
            if self.on_call:
                self.on_call(name, args, result)
            text = json.dumps(result, ensure_ascii=False, indent=1)
            out: Dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": "error" in result}
            if self.protocol_version == "2025-06-18":
                out["structuredContent"] = result
            return _ok(id_, out)
        return _err(id_, -32601, f"Method not found: {method}")

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            note: Optional[Dict[str, Any]] = None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                reply: Optional[Dict[str, Any]] = _err(None, -32700, "Parse error")
            else:
                try:
                    note = self.take_list_changed()
                    reply = self.handle(msg)
                except Exception as e:  # never crash the client's connection
                    print(f"safecontext mcp: internal error {type(e).__name__}", file=sys.stderr)
                    reply = _err(msg.get("id") if isinstance(msg, dict) else None, -32603, "Internal error")
            for outgoing in (note, reply):
                if outgoing is not None:
                    stdout.write(json.dumps(outgoing, ensure_ascii=False) + "\n")
                    stdout.flush()


def main(store: Store) -> None:
    MCPServer(store).serve()
