"""Tests the RegionCalibrator orchestration with a fake tracker (no camera).

Verifies the fingertip-stability dwell capture runs through all four corners and
produces a working homography, and that the per-corner timeout fails cleanly.
"""

from __future__ import annotations

import time

from client.cursor.mapper import RegionMapper, region_corner_targets
from client.cursor.provider import RegionCalibrator
from client.cursor.types import NormalizedSample

WIDTH, HEIGHT = 1000, 800

# Four distinct, well-separated fingertip positions, one per corner (CORNER_ORDER).
CORNER_FINGERTIPS = [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]


class FakeTracker:
    """Emits a steady fingertip for the current corner; advances when the
    calibrator announces a capture."""

    def __init__(self, points, jitter=0.0):
        self.points = points
        self.idx = 0
        self.jitter = jitter

    def advance_on_capture(self, msg: str) -> None:
        if "captured" in msg:
            self.idx += 1

    def get_latest_sample(self):
        x, y = self.points[min(self.idx, len(self.points) - 1)]
        return NormalizedSample(x=x, y=y, ts=time.time())

    def pump_preview(self):
        pass


class BlindTracker:
    """Never sees a hand."""

    def get_latest_sample(self):
        return None

    def pump_preview(self):
        pass


def test_full_calibration_then_mapping():
    tracker = FakeTracker(CORNER_FINGERTIPS)
    mapper = RegionMapper(WIDTH, HEIGHT, smoothing=1.0)
    calib = RegionCalibrator(tracker, mapper)

    ok, msg = calib.run_calibration(
        announce=tracker.advance_on_capture,
        dwell_s=0.1,
        min_samples=3,
        poll_dt_s=0.005,
        target_timeout_s=5.0,
    )
    assert ok, msg
    assert mapper.has_calibration()

    # Each calibration fingertip should now map close to its region corner.
    targets = region_corner_targets(WIDTH, HEIGHT, 0.08)
    for (fx, fy), (_name, rx, ry) in zip(CORNER_FINGERTIPS, targets):
        mapper.reset_smoothing()
        gx, gy = mapper.map_to_region(fx, fy)
        assert abs(gx - rx) < 1.0
        assert abs(gy - ry) < 1.0


def test_calibration_times_out_without_hand():
    mapper = RegionMapper(WIDTH, HEIGHT)
    calib = RegionCalibrator(BlindTracker(), mapper)
    ok, msg = calib.run_calibration(
        announce=lambda _m: None, dwell_s=0.1, target_timeout_s=0.3, poll_dt_s=0.01
    )
    assert not ok
    assert "timed out" in msg
    assert not mapper.has_calibration()
