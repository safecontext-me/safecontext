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
    for name in ("components", "app", "mail"):
        css = (UI_DIR / f"{name}.css").read_text()
        js = (UI_DIR / f"{name}.js").read_text()
        html = html.replace(f'<link rel="stylesheet" href="{name}.css">', f"<style>\n{css}\n</style>")
        html = html.replace(f'<script src="{name}.js"></script>', f"<script>\n{js}\n</script>")
    return html


def _set_dock_icon() -> None:
    """Show the SafeContext icon in the Dock when running from a checkout, where macOS would
    otherwise show Python's. (The packaged app gets it from its .icns; this is harmless there.)
    Rebuild icon.png with packaging/icons/build_icons.sh."""
    icon = Path(__file__).with_name("icon.png")
    try:
        from AppKit import NSApplication, NSImage  # pyobjc, which pywebview uses on macOS
        from PyObjCTools import AppHelper
    except Exception:  # not on macOS, or no AppKit: keep the default icon
        return

    def apply() -> None:
        image = NSImage.alloc().initWithContentsOfFile_(str(icon))
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)

    # macOS resets the icon when the app finishes launching, so set it after that, on the main thread.
    AppHelper.callAfter(apply)


def run(demo: bool = False, data_dir: Optional[Path] = None, debug: bool = False) -> None:
    import webview

    from .. import updater

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if demo:
        from .demo import demo_backend_kwargs
        backend = Backend(**demo_backend_kwargs(data_dir))
    else:
        backend = Backend(data_dir)
    if not demo:
        updater.mark_launched()  # an update helper waiting on this version knows it started
    title = ("SafeContext (demo)" if demo else "SafeContext") + label()
    window = webview.create_window(title, html=build_html(), js_api=backend, width=1180, height=780,
                                   min_size=(860, 560), text_select=True)
    backend._quit_fn = window.destroy  # "Update and restart" closes the window; the helper does the rest
    window.events.shown += _set_dock_icon  # once the app is up; see _set_dock_icon
    try:
        webview.start(debug=debug, private_mode=True, icon=str(Path(__file__).with_name("icon.png")))
    finally:
        backend._shutdown()
