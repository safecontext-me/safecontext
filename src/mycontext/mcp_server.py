"""MCP server over stdio exposing the same read-only mail tools.

Implemented directly on the MCP JSON-RPC protocol (newline-delimited
messages on stdin/stdout) so it has no extra dependencies. The official
`mcp` SDK needs Python 3.10+; the packaged app can switch to it later
without changing the tools.

Clients run it as:  python -m mycontext mcp
stdout carries protocol messages only; diagnostics go to stderr.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional, TextIO

from . import __version__
from .store import Store
from .tools import TOOLS, run_tool

SUPPORTED_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
INSTRUCTIONS = (
    "Read-only access to the user's scrubbed Gmail. Sensitive values appear as [REDACTED:TYPE]; never try to "
    "reconstruct them. Some mail (security codes, banking, tax, health, bulk) is excluded entirely. Cite each "
    "message you use by date, sender and its gmail_link.")


def _ok(id_: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


class MCPServer:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.protocol_version: Optional[str] = None

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
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mycontext", "title": "MyContext: private context layer (Gmail)", "version": __version__},
                "instructions": INSTRUCTIONS,
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
                for t in TOOLS]})
        if method == "tools/call":
            name = params.get("name")
            if name not in {t["name"] for t in TOOLS}:
                return _err(id_, -32602, f"Unknown tool: {name}")
            result = run_tool(self.store, name, params.get("arguments") or {})
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
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                reply: Optional[Dict[str, Any]] = _err(None, -32700, "Parse error")
            else:
                try:
                    reply = self.handle(msg)
                except Exception as e:  # never crash the client's connection
                    print(f"mycontext mcp: internal error {type(e).__name__}", file=sys.stderr)
                    reply = _err(msg.get("id") if isinstance(msg, dict) else None, -32603, "Internal error")
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()


def main(store: Store) -> None:
    MCPServer(store).serve()
