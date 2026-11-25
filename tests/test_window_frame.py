"""The window always ends up fully on a screen the user can see."""
from pathlib import Path

from safecontext.app.window_frame import fit_frame, is_fully_visible

# This setup: a laptop as the main display, and an external display to its left (negative x).
LAPTOP = (0, 0, 1512, 949)
EXTERNAL = (-2560, 0, 2560, 1415)
SCREENS = [LAPTOP, EXTERNAL]


def test_fully_visible_frame_is_left_alone():
    frame = (100, 100, 1180, 780)
    assert fit_frame(frame, SCREENS) == frame
    on_external = (-2000, 300, 1180, 780)
    assert fit_frame(on_external, SCREENS) == on_external


def test_frame_off_the_left_edge_comes_back():
    # The reported bug: only the window's right edge still showing at the left of the laptop.
    frame = (-1120, 100, 1180, 780)  # mostly on the external display
    fitted = fit_frame(frame, SCREENS)
    assert is_fully_visible(fitted, SCREENS)
    assert fitted == (-1180, 100, 1180, 780)  # moved the shortest way, onto the external display


def test_frame_on_no_screen_is_centred_on_the_fallback():
    gone = (-6000, 200, 1180, 780)  # its display was unplugged
    assert fit_frame(gone, SCREENS, fallback=LAPTOP) == (166, 84.5, 1180, 780)
    assert fit_frame(gone, SCREENS) == (166, 84.5, 1180, 780)  # first screen when no fallback


def test_frame_bigger_than_the_screen_is_shrunk_to_it():
    big = (-100, -100, 2000, 1200)
    assert fit_frame(big, [LAPTOP]) == LAPTOP


def test_frame_under_the_menu_bar_is_moved_down():
    frame = (100, 400, 1180, 780)  # top at 1180, above the visible area's 949
    assert fit_frame(frame, [LAPTOP]) == (100, 169, 1180, 780)


def test_no_screens_returns_frame_unchanged():
    assert fit_frame((1, 2, 3, 4), []) == (1, 2, 3, 4)


def test_page_does_not_use_pywebviews_javascript_drag():
    """pywebview's .pywebview-drag-region moves the window from JavaScript by absolute position and
    throws it off screen with a display left of the main one; the header must use our native drag."""
    ui = Path(__file__).parents[1] / "src" / "safecontext" / "app" / "ui"
    for name in ("index.html", "app.js", "app.css"):
        assert "pywebview-drag-region" not in (ui / name).read_text()
    assert "window-drag" in (ui / "index.html").read_text()
    assert "start_window_drag" in (ui / "app.js").read_text()
