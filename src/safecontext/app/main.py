"""Open the SafeContext window.

    python -m safecontext app           # your real mailbox
    python -m safecontext app --demo    # synthetic mailbox, no network
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from ..env import label
from .backend import Backend

UI_DIR = Path(__file__).with_name("ui")


def build_html(mac_titlebar: bool = False) -> str:
    """Inline the CSS and JS into one page so the window needs no local web server.
    mac_titlebar: the window has no visible title bar (see _unified_title_bar), so the page leaves room
    for the traffic lights and keeps a draggable strip along the top."""
    html = (UI_DIR / "index.html").read_text()
    if mac_titlebar:
        html = html.replace('<html lang="en">', '<html lang="en" class="mac-titlebar">', 1)
    for name in ("tokens", "app", "components", "mail"):  # tokens.css first: everything else uses it
        css = (UI_DIR / f"{name}.css").read_text()
        html = html.replace(f'<link rel="stylesheet" href="{name}.css">', f"<style>\n{css}\n</style>")
    for name in ("components", "app", "mail"):
        js = (UI_DIR / f"{name}.js").read_text()
        html = html.replace(f'<script src="{name}.js"></script>', f"<script>\n{js}\n</script>")
    return html


def _unified_title_bar(window) -> None:
    """Run the page up to the top of the window with the title bar hidden and the traffic lights
    floating over the sidebar, as most Mac apps do. Called by pywebview on the main thread just
    before the window first shows, so it never flashes the old title bar."""
    try:
        import AppKit
    except Exception:  # not on macOS: keep the normal title bar
        return
    w = window.native
    if w is None:
        return
    w.setStyleMask_(w.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView)
    w.setTitlebarAppearsTransparent_(True)
    w.setTitleVisibility_(AppKit.NSWindowTitleHidden)  # the title still names the window in the Dock and Window menu
    # An empty unified toolbar gives the roomier traffic-light spacing of current Mac apps (Notes, Mail,
    # Finder). Its title area is 52pt; our header is shorter, so the lights are moved up to its middle.
    toolbar = AppKit.NSToolbar.alloc().initWithIdentifier_("SafeContextWindow")
    toolbar.setShowsBaselineSeparator_(False)
    w.setToolbar_(toolbar)
    w.setToolbarStyle_(AppKit.NSWindowToolbarStyleUnified)
    # pywebview paints the title bar area opaque; let the page show through it.
    bar = w.contentView().superview().subviews().lastObject()
    if bar is not None and bar.respondsToSelector_("setBackgroundColor:"):
        bar.setBackgroundColor_(AppKit.NSColor.clearColor())
    _keep_traffic_lights_centered(w)


# Vertical middle of the page header, in points from the top of the window: half of --titlebar-height
# (44px) in tokens.css. Change both together.
_HEADER_MIDDLE = 22
_light_observers: list = []  # keeps the notification observers alive for the window's lifetime


def _place_traffic_lights(w) -> None:
    """Move the close / minimize / zoom buttons so they're centered in the page header."""
    import AppKit
    if w.styleMask() & AppKit.NSWindowStyleMaskFullScreen:  # full screen draws its own title bar
        return
    for kind in (AppKit.NSWindowCloseButton, AppKit.NSWindowMiniaturizeButton, AppKit.NSWindowZoomButton):
        b = w.standardWindowButton_(kind)
        if b is None or b.superview() is None:
            continue
        f, parent = b.frame(), b.superview()
        top = _HEADER_MIDDLE - f.size.height / 2  # from the top of the title area
        y = top if parent.isFlipped() else parent.frame().size.height - f.size.height - top
        if abs(f.origin.y - y) > 0.5:
            b.setFrameOrigin_(AppKit.NSMakePoint(f.origin.x, y))


def _keep_traffic_lights_centered(w) -> None:
    """macOS lays the buttons out again on resize, focus changes and leaving full screen, which puts
    them back at its default height. Re-place them after each of those (and once the window is up)."""
    import AppKit
    from PyObjCTools import AppHelper
    _place_traffic_lights(w)
    AppHelper.callAfter(_place_traffic_lights, w)
    center = AppKit.NSNotificationCenter.defaultCenter()
    for name in ("NSWindowDidResizeNotification", "NSWindowDidEndLiveResizeNotification",
                 "NSWindowDidBecomeKeyNotification", "NSWindowDidResignKeyNotification",
                 "NSWindowDidExitFullScreenNotification", "NSWindowDidChangeScreenNotification"):
        _light_observers.append(center.addObserverForName_object_queue_usingBlock_(
            name, w, None, lambda _note: _place_traffic_lights(w)))


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
    mac = sys.platform == "darwin"
    # Only the header's empty space drags the window, not the search field inside it.
    webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
    window = webview.create_window(title, html=build_html(mac_titlebar=mac), js_api=backend, width=1180, height=780,
                                   min_size=(860, 560), text_select=True, draggable=True)
    if mac:
        window.events.before_show += _unified_title_bar
    backend._quit_fn = window.destroy  # "Update and restart" closes the window; the helper does the rest
    window.events.shown += _set_dock_icon  # once the app is up; see _set_dock_icon
    try:
        webview.start(debug=debug, private_mode=True, icon=str(Path(__file__).with_name("icon.png")))
    finally:
        backend._shutdown()
