"""Tests for ScreenToRegionAffine (screen point -> region pixel) and the
two-corner viewport anchoring (re-arm so both anchors aren't captured at once).
"""

from __future__ import annotations

import time

import pytest

from client.cursor.mapper import CursorMapper, ScreenGeometry, ScreenToRegionAffine
from client.cursor.provider import HandCursorProvider
from client.cursor.types import NormalizedSample

WIDTH, HEIGHT = 2477, 1462
# Plausible viewport-on-screen anchors (points): top-left and bottom-right.
TL, BR = (2.0, 26.0), (1240.0, 757.0)


def test_affine_maps_corners_and_center():
    a = ScreenToRegionAffine.from_anchors(TL, BR, WIDTH, HEIGHT)
    assert a is not None
    # corners clamp to the valid pixel range [0, W-1] x [0, H-1]
    assert a.map(*TL) == pytest.approx((0.0, HEIGHT - 1))  # top-left screen -> region top
    assert a.map(*BR) == pytest.approx((WIDTH - 1, 0.0))   # bottom-right -> region bottom-right
    cx, cy = (TL[0] + BR[0]) / 2, (TL[1] + BR[1]) / 2
    rx, ry = a.map(cx, cy)
    assert rx == pytest.approx(WIDTH / 2, abs=1.0)
    assert ry == pytest.approx(HEIGHT / 2, abs=1.0)


def test_affine_clamps_outside_viewport():
    a = ScreenToRegionAffine.from_anchors(TL, BR, WIDTH, HEIGHT)
    rx, ry = a.map(0.0, 0.0)  # above/left of viewport
    assert 0.0 <= rx <= WIDTH - 1
    assert 0.0 <= ry <= HEIGHT - 1


def test_affine_rejects_degenerate_anchors():
    assert ScreenToRegionAffine.from_anchors((100, 50), (100, 800), WIDTH, HEIGHT) is None


# ── viewport anchoring orchestration ────────────────────────────────────────
class FakeTracker:
    def __init__(self, points, advance=True):
        self.points = points
        self.idx = 0
        self.advance = advance

    def on_announce(self, msg: str) -> None:
        if self.advance and "captured" in msg:
            self.idx += 1

    def get_latest_sample(self):
        x, y = self.points[min(self.idx, len(self.points) - 1)]
        return NormalizedSample(x=x, y=y, ts=time.time())

    def pump_preview(self):
        pass


class _NoOverlay:
    def start(self):
        return True

    def stop(self):
        pass

    def pump(self):
        pass

    def set_visible(self, v):
        pass


def _provider(tracker) -> HandCursorProvider:
    return HandCursorProvider(
        tracker=tracker,
        overlay=False,
        mapper=CursorMapper(screen_geometry=ScreenGeometry(1512, 982), smoothing=1.0),
        calibration_overlay_ui=_NoOverlay(),
    )


def test_anchoring_captures_two_corners():
    tracker = FakeTracker([(0.1, 0.1), (0.9, 0.9)])  # well-separated -> arms fine
    p = _provider(tracker)
    ok, msg = p.calibrate_viewport_anchors(
        WIDTH, HEIGHT, announce=tracker.on_announce,
        dwell_s=0.15, min_samples=3, poll_dt_s=0.01, target_timeout_s=5.0,
    )
    assert ok, msg
    assert p.screen_to_region is not None


def test_anchoring_does_not_blast_through_when_still():
    tracker = FakeTracker([(0.5, 0.5)], advance=False)  # hand never moves
    p = _provider(tracker)
    ok, msg = p.calibrate_viewport_anchors(
        WIDTH, HEIGHT, announce=lambda _m: None,
        dwell_s=0.15, min_samples=3, poll_dt_s=0.01, target_timeout_s=0.4,
    )
    assert not ok
    assert "timed out" in msg
