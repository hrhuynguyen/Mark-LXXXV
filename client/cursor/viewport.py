"""client.cursor.viewport

Convert a screen-space hand cursor (points, top-left origin — what the overlay /
CursorMapper produce) into Blender VIEW_3D **region pixels** (bottom-left origin —
what pick_object_at wants).

Blender reports the window in points but the region in physical pixels, so we
fold in HiDPI ``pixel_size`` (2.0 on Retina). Origin convention (verified against
a live session): Blender's ``window.x/window.y`` are screen points with a
top-left origin, and ``region.x/region.y`` are pixels offset from the window's
**bottom-left**.

Build one from the addon's ``get_window_geometry`` response::

    geo = conn.send_command("get_window_geometry")
    rect = ViewportScreenRect.from_geometry(geo)
    rx, ry = rect.screen_to_region(cursor.x, cursor.y)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class ViewportScreenRect:
    # Region edges in screen points (top-left origin).
    left_pt: float
    top_pt: float
    bottom_pt: float
    # Region size in pixels + the HiDPI factor.
    region_w_px: int
    region_h_px: int
    pixel_size: float

    @classmethod
    def from_geometry(cls, geo: dict) -> "ViewportScreenRect":
        win = geo["window"]
        region = geo["region"]
        ps = float(geo["pixel_size"]) or 1.0

        left_pt = win["x"] + region["x"] / ps
        region_h_pt = region["height"] / ps
        # region.y is from the window bottom; window.y is the window top on screen.
        top_pt = win["y"] + (win["height"] - region["y"] / ps - region_h_pt)
        bottom_pt = top_pt + region_h_pt
        return cls(
            left_pt=left_pt,
            top_pt=top_pt,
            bottom_pt=bottom_pt,
            region_w_px=int(region["width"]),
            region_h_px=int(region["height"]),
            pixel_size=ps,
        )

    def screen_to_region(self, screen_x_pt: float, screen_y_pt: float) -> Tuple[float, float]:
        """Screen point (top-left origin) -> region pixel (bottom-left origin)."""
        rx = (screen_x_pt - self.left_pt) * self.pixel_size
        ry = (self.bottom_pt - screen_y_pt) * self.pixel_size  # flip to bottom-left origin
        return rx, ry

    def contains_screen_point(self, screen_x_pt: float, screen_y_pt: float) -> bool:
        rx, ry = self.screen_to_region(screen_x_pt, screen_y_pt)
        return 0.0 <= rx <= self.region_w_px and 0.0 <= ry <= self.region_h_px
