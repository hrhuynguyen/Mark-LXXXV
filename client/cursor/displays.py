"""client.cursor.displays

macOS display geometry helpers (lifted verbatim from Wand). Used by the webcam
tracker to place its preview window on the built-in display (where the camera
lives). Forge maps the fingertip to the *Blender region*, not the screen, so the
calibration itself does not depend on these — but the tracker preview does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class DisplayGeometry:
    display_id: int
    x: int
    y: int
    width: int
    height: int
    is_main: bool = False

    def contains_point(self, px: int, py: int) -> bool:
        return self.x <= int(px) < self.x + self.width and self.y <= int(py) < self.y + self.height


def _fallback_main_display() -> DisplayGeometry:
    width = 1920
    height = 1080
    try:
        import Quartz  # type: ignore

        main_id = int(Quartz.CGMainDisplayID())
        bounds = Quartz.CGDisplayBounds(main_id)
        width = max(1, int(bounds.size.width))
        height = max(1, int(bounds.size.height))
        return DisplayGeometry(display_id=main_id, x=0, y=0, width=width, height=height, is_main=True)
    except Exception:
        return DisplayGeometry(display_id=0, x=0, y=0, width=width, height=height, is_main=True)


def get_active_displays(max_displays: int = 16) -> List[DisplayGeometry]:
    try:
        import Quartz  # type: ignore

        main_id = int(Quartz.CGMainDisplayID())
        err, display_ids, count = Quartz.CGGetActiveDisplayList(int(max_displays), None, None)
        if int(err) != 0 or int(count) <= 0:
            return [_fallback_main_display()]

        displays: list[DisplayGeometry] = []
        for display_id in list(display_ids)[: int(count)]:
            did = int(display_id)
            bounds = Quartz.CGDisplayBounds(did)
            displays.append(
                DisplayGeometry(
                    display_id=did,
                    x=int(bounds.origin.x),
                    y=int(bounds.origin.y),
                    width=max(1, int(bounds.size.width)),
                    height=max(1, int(bounds.size.height)),
                    is_main=(did == main_id),
                )
            )
        return displays or [_fallback_main_display()]
    except Exception:
        return [_fallback_main_display()]


def get_main_display_geometry() -> DisplayGeometry:
    displays = get_active_displays()
    for display in displays:
        if display.is_main:
            return display
    return displays[0]


def get_builtin_display_geometry() -> DisplayGeometry:
    """Geometry of the Mac's built-in display (where the camera lives), falling
    back to the main display."""
    try:
        import Quartz  # type: ignore

        displays = get_active_displays()
        for d in displays:
            if Quartz.CGDisplayIsBuiltin(d.display_id):
                return d
    except Exception:
        pass
    return get_main_display_geometry()
