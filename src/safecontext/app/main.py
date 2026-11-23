"""Open the SafeContext window.

    python -m safecontext app           # your real mailbox
    python -m safecontext app --demo    # synthetic mailbox, no network
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..env import label
from .backend import Backend

UI_DIR = Path(__file__).with_name("ui")


def build_html() -> str:
    """Inline the CSS and JS into one page so the window needs no local web server."""
    html = (UI_DIR / "index.html").read_text()
    for name in ("app", "mail"):
        css = (UI_DIR / f"{name}.css").read_text()
        js = (UI_DIR / f"{name}.js").read_text()
        html = html.replace(f'<link rel="stylesheet" href="{name}.css">', f"<style>\n{css}\n</style>")
        html = html.replace(f'<script src="{name}.js"></script>', f"<script>\n{js}\n</script>")
    return html


def run(demo: bool = False, data_dir: Optional[Path] = None, debug: bool = False) -> None:
    import webview

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if demo:
        from .demo import demo_backend_kwargs
        backend = Backend(**demo_backend_kwargs(data_dir))
    else:
        backend = Backend(data_dir)
    title = ("SafeContext (demo)" if demo else "SafeContext") + label()
    webview.create_window(title, html=build_html(), js_api=backend, width=1180, height=780,
                          min_size=(860, 560), text_select=True)
    try:
        webview.start(debug=debug, private_mode=True)
    finally:
        backend._shutdown()
