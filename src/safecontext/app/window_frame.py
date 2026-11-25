"""Keep the window on a screen the user can see.

Pure geometry, so it can be tested without a display. Rects are (x, y, width, height) in macOS
screen coordinates: points, origin at the bottom-left of the main display, y growing upward. Screens
to the left of or below the main display have negative x or y.
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

Rect = Tuple[float, float, float, float]


def _overlap(a: Rect, b: Rect) -> float:
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def is_fully_visible(frame: Rect, screens: Sequence[Rect]) -> bool:
    """True when the whole frame lies inside one screen's visible area."""
    return any(_overlap(frame, s) >= frame[2] * frame[3] - 0.5 for s in screens)


def fit_frame(frame: Rect, screens: Sequence[Rect], fallback: Optional[Rect] = None) -> Rect:
    """Return the frame moved (and shrunk if it must be) to lie fully on one visible screen.

    The screen is the one the frame overlaps most; if it overlaps none (say its display was
    unplugged), `fallback` or else the first screen, and the frame is centred there. A frame that is
    already fully visible comes back unchanged.
    """
    if not screens or is_fully_visible(frame, screens):
        return frame
    x, y, w, h = frame
    best = max(screens, key=lambda s: _overlap(frame, s))
    if _overlap(frame, best) <= 0:
        best = fallback if fallback is not None else screens[0]
        sx, sy, sw, sh = best
        w, h = min(w, sw), min(h, sh)
        return (sx + (sw - w) / 2, sy + (sh - h) / 2, w, h)
    sx, sy, sw, sh = best
    w, h = min(w, sw), min(h, sh)
    x = min(max(x, sx), sx + sw - w)
    y = min(max(y, sy), sy + sh - h)
    return (x, y, w, h)
