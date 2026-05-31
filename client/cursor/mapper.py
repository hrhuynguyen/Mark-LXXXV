"""client.cursor.mapper

The fingertip→Blender-region homography (Forge Step 3).

Wand mapped the fingertip to *full-screen* pixels; Forge maps it directly to the
Blender VIEW_3D **region** (pixels, bottom-left origin — CONTRACTS.md §6). Fitting
a 4-corner homography straight to region pixels means we never need to know where
the Blender window sits on the OS screen, and the camera's y-down → region y-up
flip is absorbed by the correspondences themselves.

Same homography technique as Wand (``cv2.getPerspectiveTransform`` /
``cv2.perspectiveTransform``); ``get_main_display_size`` is kept here because the
webcam tracker imports it for preview-window placement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import cv2  # type: ignore
import numpy as np  # type: ignore

from .displays import get_builtin_display_geometry

# Region-corner labels, in the order calibration visits them.
CORNER_ORDER = ("top_left", "top_right", "bottom_right", "bottom_left")


@dataclass(frozen=True)
class ScreenGeometry:
    width: int
    height: int
    display_id: Optional[int] = None


def get_main_display_size() -> Tuple[int, int]:
    """Built-in display size (where the camera is), fallback 1920x1080."""
    geom = get_builtin_display_geometry()
    return geom.width, geom.height


def region_corner_targets(
    width: int, height: int, margin_ratio: float = 0.08
) -> List[Tuple[str, float, float]]:
    """Four labeled calibration targets in region pixels, **bottom-left origin**
    (y up), inset by ``margin_ratio``. Order matches CORNER_ORDER.

    These are the points the user is asked to point at — the visible corners of
    the Blender viewport — paired with the fingertip medians captured there.
    """
    mx = width * margin_ratio
    my = height * margin_ratio
    return [
        ("top_left", mx, height - my),
        ("top_right", width - mx, height - my),
        ("bottom_right", width - mx, my),
        ("bottom_left", mx, my),
    ]


class RegionMapper:
    """Maps a normalized fingertip (0..1, y-down) to Blender region pixels
    (bottom-left origin) via a 4-point homography, with optional EMA smoothing."""

    def __init__(self, width: int, height: int, smoothing: float = 0.35) -> None:
        self.width = int(width)
        self.height = int(height)
        self.smoothing = float(min(1.0, max(0.0, smoothing)))
        self._homography = None
        self._smoothed: Optional[Tuple[float, float]] = None

    def has_calibration(self) -> bool:
        return self._homography is not None

    def reset_smoothing(self) -> None:
        self._smoothed = None

    def clear_calibration(self) -> None:
        self._homography = None
        self.reset_smoothing()

    def calibrate(
        self,
        fingertip_points_norm: Sequence[Tuple[float, float]],
        region_points_px: Sequence[Tuple[float, float]],
    ) -> Tuple[bool, str]:
        """Fit the homography from >=4 (fingertip_norm -> region_px) pairs.

        Uses exactly the first 4 correspondences (a plane→plane projective map is
        determined by 4 point pairs), matching Wand's calibration.
        """
        if len(fingertip_points_norm) < 4 or len(region_points_px) < 4:
            return False, "need at least 4 correspondence points"
        try:
            src = np.array(fingertip_points_norm[:4], dtype=np.float32)
            dst = np.array(region_points_px[:4], dtype=np.float32)
            homography = cv2.getPerspectiveTransform(src, dst)
        except Exception as exc:  # noqa: BLE001
            return False, f"failed to estimate calibration transform: {exc}"
        self._homography = homography
        self.reset_smoothing()
        return True, "region calibration active"

    def map_to_region(self, x_norm: float, y_norm: float) -> Tuple[float, float]:
        """Map a normalized fingertip to region px (clamped to the region),
        applying EMA smoothing across calls. Before calibration, falls back to a
        simple stretch with the y-axis flipped to region (bottom-left) origin."""
        x_n = min(1.0, max(0.0, float(x_norm)))
        y_n = min(1.0, max(0.0, float(y_norm)))

        if self._homography is not None:
            pts = np.array([[[x_n, y_n]]], dtype=np.float32)
            mapped = cv2.perspectiveTransform(pts, self._homography)
            raw_x = float(mapped[0][0][0])
            raw_y = float(mapped[0][0][1])
        else:
            raw_x = x_n * (self.width - 1)
            raw_y = (1.0 - y_n) * (self.height - 1)  # flip to bottom-left origin

        if self._smoothed is None:
            sx, sy = raw_x, raw_y
        else:
            a = self.smoothing
            sx = a * raw_x + (1.0 - a) * self._smoothed[0]
            sy = a * raw_y + (1.0 - a) * self._smoothed[1]
        self._smoothed = (sx, sy)

        cx = min(max(sx, 0.0), float(self.width - 1))
        cy = min(max(sy, 0.0), float(self.height - 1))
        return cx, cy
