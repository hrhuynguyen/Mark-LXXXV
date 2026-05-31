"""client.cursor.mapper

Homography cursor mapper + smoothing (reused from Wand). Used by Step 3 calibration.
"""

from __future__ import annotations

from .displays import get_builtin_display_geometry


def get_main_display_size() -> tuple[int, int]:
    display = get_builtin_display_geometry()
    return display.width, display.height
