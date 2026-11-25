"""Dev only: serve the app UI in a normal browser, wired to a DEMO backend.

The real app uses pywebview's in-process bridge and no HTTP server. This
script exists only to preview and screenshot the UI. It binds to 127.0.0.1,
always runs in demo mode, and is not shipped.

    .venv/bin/python scripts/dev_preview.py [port] [--data-dir DIR]

--data-dir reuses a demo data folder, e.g. a large one from scripts/perf_fixture.py. Never point it
at a real SafeContext folder.
"""
import json
import sys
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src")]

from safecontext.app.backend import Backend  # noqa: E402
from safecontext.app.demo import demo_backend_kwargs  # noqa: E402
from safecontext.app.main import build_html  # noqa: E402

_args = sys.argv[1:]
_data_dir = None
if "--data-dir" in _args:
    i = _args.index("--data-dir")
    _data_dir = Path(_args[i + 1]).expanduser()
    del _args[i:i + 2]
backend = Backend(**demo_backend_kwargs(_data_dir), auto_sync=False)
PUBLIC = {n for n in dir(backend) if not n.startswith("_")}

SHIM = """<script>
window.pywebview = { api: new Proxy({}, { get: (_, name) => (...args) =>
  fetch('/api/' + name, { method: 'POST', body: JSON.stringify(args) }).then(r => r.json()) }) };
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            return self._send(404, "not found", "text/plain")
        html = build_html().replace("connect-src 'none'", "connect-src 'self'")
        html = html.replace("<script>\n", SHIM + "\n<script>\n", 1)
        self._send(200, html, "text/html; charset=utf-8")

    def do_POST(self):
        name = self.path.rsplit("/", 1)[-1]
        if not self.path.startswith("/api/") or name not in PUBLIC:
            return self._send(404, json.dumps({"ok": False, "error": "unknown"}), "application/json")
        args = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"[]")
        self._send(200, json.dumps(getattr(backend, name)(*args)), "application/json")


if __name__ == "__main__":
    port = int(_args[0]) if _args else 8765
    print(f"demo UI preview on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
