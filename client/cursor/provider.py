"""client.cursor.provider

Forge region calibration (Step 3). Adapts Wand's guided 4-point calibration to
the Blender viewport.

The key difference from Wand: there is no pre-mapped on-screen cursor to move
into a target ring (we deliberately don't know where the Blender window sits), so
we can't draw a ring at a region corner. Instead we guide the user by prompt
("point at the TOP-LEFT corner of the Blender viewport and hold still") and
detect capture via **fingertip stability** — when the normalized fingertip stops
moving for a short dwell, we take its median and pair it with that region corner.

After four corners, the RegionMapper holds a fingertip→region homography that
feeds straight into the addon's ``pick_object_at``.
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from typing import Callable, List, Optional, Tuple

from .mapper import RegionMapper, region_corner_targets
from .webcam_tracker import WebcamFingerTracker

_PRETTY = {
    "top_left": "TOP-LEFT",
    "top_right": "TOP-RIGHT",
    "bottom_right": "BOTTOM-RIGHT",
    "bottom_left": "BOTTOM-LEFT",
}


class RegionCalibrator:
    """Drives a fingertip-stability 4-corner calibration against the Blender region."""

    def __init__(self, tracker: WebcamFingerTracker, mapper: RegionMapper) -> None:
        self.tracker = tracker
        self.mapper = mapper

    def current_region_point(self) -> Optional[Tuple[float, float]]:
        """Latest fingertip mapped to region px, or None if no hand is visible."""
        sample = self.tracker.get_latest_sample()
        if sample is None:
            return None
        return self.mapper.map_to_region(sample.x, sample.y)

    def run_calibration(
        self,
        *,
        announce: Optional[Callable[[str], None]] = None,
        margin_ratio: float = 0.08,
        dwell_s: float = 0.6,
        stability_eps: float = 0.02,
        min_samples: int = 6,
        target_timeout_s: float = 25.0,
        poll_dt_s: float = 0.02,
        pump_ui: Optional[Callable[[], None]] = None,
    ) -> Tuple[bool, str]:
        """Capture the 4 viewport corners and fit the homography.

        ``stability_eps`` is the max normalized spread (in x and y) over the dwell
        window for the fingertip to count as "held still".
        """
        say = announce or print
        pump = pump_ui or self.tracker.pump_preview
        targets = region_corner_targets(self.mapper.width, self.mapper.height, margin_ratio)

        fingertip_pts: List[Tuple[float, float]] = []
        region_pts: List[Tuple[float, float]] = []
        missing_notice_deadline = 0.0

        say("[calibration] 4-corner calibration. Point at each corner of the Blender")
        say("[calibration] 3D viewport (as you see it on screen) and hold your finger still.")

        for idx, (name, rx, ry) in enumerate(targets, start=1):
            say(f"[calibration] {idx}/4 — point at the {_PRETTY[name]} corner and hold still.")
            window: deque = deque()  # (ts, x_norm, y_norm)
            deadline = time.time() + target_timeout_s
            captured = False

            while time.time() < deadline:
                pump()
                sample = self.tracker.get_latest_sample()
                now = time.time()

                if sample is None:
                    window.clear()
                    if now >= missing_notice_deadline:
                        say("[calibration] hand not detected — keep one hand visible.")
                        missing_notice_deadline = now + 1.5
                    time.sleep(poll_dt_s)
                    continue

                window.append((sample.ts, float(sample.x), float(sample.y)))
                while window and now - window[0][0] > dwell_s:
                    window.popleft()

                if len(window) >= min_samples and (now - window[0][0]) >= dwell_s * 0.8:
                    xs = [p[1] for p in window]
                    ys = [p[2] for p in window]
                    if (max(xs) - min(xs)) <= stability_eps and (max(ys) - min(ys)) <= stability_eps:
                        fingertip_pts.append((statistics.median(xs), statistics.median(ys)))
                        region_pts.append((rx, ry))
                        say(f"[calibration] captured {_PRETTY[name]}.")
                        captured = True
                        break

                time.sleep(poll_dt_s)

            if not captured:
                return False, (
                    f"calibration timed out at {_PRETTY[name]} — hold your finger still on the corner."
                )

        ok, msg = self.mapper.calibrate(fingertip_pts, region_pts)
        if ok:
            say("[calibration] done — region calibration active for this session.")
        return ok, msg
