"""client.cursor.displays

macOS display geometry helpers (reused from Wand).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DisplayGeometry:
    display_id: int
    x: int
    y: int
    width: int
    height: int
    scale: float = 1.0


def get_builtin_display_geometry() -> DisplayGeometry:
    """Return the main display bounds used for hand-to-screen mapping."""

    try:
        from AppKit import NSScreen

        screen = NSScreen.mainScreen()
        if screen is not None:
            frame = screen.frame()
            description = screen.deviceDescription()
            display_id = int(description.get("NSScreenNumber", 1))
            return DisplayGeometry(
                display_id=display_id,
                x=int(round(frame.origin.x)),
                y=int(round(frame.origin.y)),
                width=max(1, int(round(frame.size.width))),
                height=max(1, int(round(frame.size.height))),
                scale=float(screen.backingScaleFactor()),
            )
    except Exception:
        pass

    return DisplayGeometry(display_id=1, x=0, y=0, width=1920, height=1080)
