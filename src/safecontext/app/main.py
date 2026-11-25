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
from .window_frame import fit_frame

UI_DIR = Path(__file__).with_name("ui")
LUCIDE_DIR = UI_DIR / "vendor" / "lucide"


def lucide_icons() -> dict:
    """The vendored Lucide icons (ui/vendor/lucide/*.svg, unchanged from lucide-icons/lucide) as
    {name: [[tag, attributes], ...]}: each icon's shapes, for icons.js to draw with createElementNS."""
    import xml.etree.ElementTree as ET
    icons = {}
    for f in sorted(LUCIDE_DIR.glob("*.svg")):
        root = ET.parse(f).getroot()
        icons[f.stem] = [[el.tag.split("}")[-1], dict(el.attrib)] for el in root]
    return icons


def build_html(mac_titlebar: bool = False) -> str:
    """Inline the CSS and JS into one page so the window needs no local web server.
    mac_titlebar: the window has no visible title bar (see _unified_title_bar), so the page leaves room
    for the traffic lights and keeps a draggable strip along the top."""
    html = (UI_DIR / "index.html").read_text()
    if mac_titlebar:
        html = html.replace('<html lang="en">', '<html lang="en" class="mac-titlebar">', 1)
    for name in ("tokens", "shadcn", "app", "components", "mail"):  # tokens.css first, then the component library
        css = (UI_DIR / f"{name}.css").read_text()
        html = html.replace(f'<link rel="stylesheet" href="{name}.css">', f"<style>\n{css}\n</style>")
    import json
    icons = json.dumps(lucide_icons(), separators=(",", ":")).replace("</", "<\\/")
    html = html.replace('<script src="icons.js"></script>',
                        f'<script>window.LUCIDE = {icons};</script>\n<script src="icons.js"></script>')
    for name in ("icons", "components", "app", "mail"):  # icons.js first: the others draw with icon()
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


def _start_window_drag(window) -> None:
    """The page calls this (as pywebview.api.start_window_drag) on a mouse-down in the header's empty
    space. macOS then moves the window itself, the same as dragging a normal title bar: it follows the
    mouse across displays and snaps to edges and tiles.

    This replaces pywebview's own drag region, which moved the window from JavaScript by absolute
    position: it added the page's screen coordinates (already relative to the main display) to the
    origin of the screen the window opened on. With a second display to the left of the main one, that
    adds the display's offset twice and throws the window off the left edge on the first drag."""
    try:
        import AppKit
        from PyObjCTools import AppHelper
    except Exception:  # not on macOS
        return

    def drag() -> None:
        w = window.native
        if w is None or not AppKit.NSEvent.pressedMouseButtons() & 1:
            return  # the button is already up: that was a click, not a drag
        event = AppKit.NSApp.currentEvent()
        if (event is None or event.window() != w
                or event.type() not in (AppKit.NSEventTypeLeftMouseDown, AppKit.NSEventTypeLeftMouseDragged)):
            event = AppKit.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                AppKit.NSEventTypeLeftMouseDown, w.mouseLocationOutsideOfEventStream(), 0,
                AppKit.NSProcessInfo.processInfo().systemUptime(), w.windowNumber(), None, 0, 1, 1.0)
        w.performWindowDragWithEvent_(event)

    AppHelper.callAfter(drag)


def _rect(r) -> tuple:
    return (r.origin.x, r.origin.y, r.size.width, r.size.height)


def _fit_on_screen(w) -> None:
    """Move the window back onto a visible screen if it isn't fully on one (say its display was
    unplugged or rearranged while it was open). A window that is fully visible is left alone."""
    import AppKit
    if w.styleMask() & AppKit.NSWindowStyleMaskFullScreen:
        return
    screens = [_rect(s.visibleFrame()) for s in AppKit.NSScreen.screens()]
    main = AppKit.NSScreen.mainScreen()
    frame = _rect(w.frame())
    fitted = fit_frame(frame, screens, _rect(main.visibleFrame()) if main is not None else None)
    if fitted != frame:
        w.setFrame_display_(AppKit.NSMakeRect(*fitted), True)


def _keep_window_on_screen(window) -> None:
    """Open fully on a visible screen, and stay on one when displays change. Runs just before show."""
    try:
        import AppKit
    except Exception:
        return
    w = window.native
    if w is None:
        return
    _fit_on_screen(w)
    _light_observers.append(AppKit.NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
        "NSApplicationDidChangeScreenParametersNotification", None, None, lambda _note: _fit_on_screen(w)))


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
    window = webview.create_window(title, html=build_html(mac_titlebar=mac), js_api=backend, width=1180, height=780,
                                   min_size=(860, 560), text_select=True, draggable=True)
    # The header's empty space drags the window natively (see _start_window_drag). The page marks it with
    # .window-drag, not pywebview's .pywebview-drag-region, so pywebview's JavaScript drag never runs.
    def start_window_drag() -> None:
        _start_window_drag(window)
    window.expose(start_window_drag)
    if mac:
        window.events.before_show += _unified_title_bar
        window.events.before_show += _keep_window_on_screen
    backend._quit_fn = window.destroy  # "Update and restart" closes the window; the helper does the rest
    window.events.shown += _set_dock_icon  # once the app is up; see _set_dock_icon
    try:
        webview.start(debug=debug, private_mode=True, icon=str(Path(__file__).with_name("icon.png")))
    finally:
        backend._shutdown()
