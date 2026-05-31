"""Unit tests for the fingertip->region homography (client.cursor.mapper).

No camera/Blender needed — we synthesize a known affine fingertip->region map
(a special homography), feed its 4 corner correspondences to the mapper, and
check that interior points are reproduced and that the bottom-left-origin /
clamping conventions hold.
"""

from __future__ import annotations

import pytest

from client.cursor.mapper import RegionMapper, region_corner_targets

WIDTH, HEIGHT = 1000, 800


# A non-trivial true map: scale+offset with the camera y-down -> region y-up flip.
def true_map(x_norm: float, y_norm: float) -> tuple[float, float]:
    rx = x_norm * 0.9 * (WIDTH - 1) + 0.05 * (WIDTH - 1)
    ry = (1.0 - y_norm) * 0.9 * (HEIGHT - 1) + 0.05 * (HEIGHT - 1)
    return rx, ry


def invert_true_map(rx: float, ry: float) -> tuple[float, float]:
    x_norm = (rx - 0.05 * (WIDTH - 1)) / (0.9 * (WIDTH - 1))
    y_norm = 1.0 - (ry - 0.05 * (HEIGHT - 1)) / (0.9 * (HEIGHT - 1))
    return x_norm, y_norm


def _calibrated_mapper() -> RegionMapper:
    targets = region_corner_targets(WIDTH, HEIGHT, margin_ratio=0.08)
    fingertip_pts = [invert_true_map(rx, ry) for _name, rx, ry in targets]
    region_pts = [(rx, ry) for _name, rx, ry in targets]
    m = RegionMapper(WIDTH, HEIGHT, smoothing=1.0)  # 1.0 = no EMA lag
    ok, _ = m.calibrate(fingertip_pts, region_pts)
    assert ok
    return m


def test_corner_targets_bottom_left_origin():
    targets = dict((n, (x, y)) for n, x, y in region_corner_targets(WIDTH, HEIGHT, 0.08))
    assert set(targets) == {"top_left", "top_right", "bottom_right", "bottom_left"}
    # bottom-left origin: "top" corners have larger y than "bottom" corners
    assert targets["top_left"][1] > targets["bottom_left"][1]
    assert targets["top_right"][0] > targets["top_left"][0]
    # margins symmetric
    assert targets["bottom_left"] == pytest.approx((80.0, 64.0))
    assert targets["top_right"] == pytest.approx((920.0, 736.0))


def test_homography_reproduces_interior_points():
    m = _calibrated_mapper()
    for x_norm, y_norm in [(0.5, 0.5), (0.3, 0.7), (0.65, 0.25), (0.5, 0.2)]:
        m.reset_smoothing()
        exp_x, exp_y = true_map(x_norm, y_norm)
        got_x, got_y = m.map_to_region(x_norm, y_norm)
        assert got_x == pytest.approx(exp_x, abs=0.5)
        assert got_y == pytest.approx(exp_y, abs=0.5)


def test_y_axis_flip_after_calibration():
    """Fingertip at top of camera (y_norm~0) maps to the top of the region (high y)."""
    m = _calibrated_mapper()
    m.reset_smoothing()
    _, top_y = m.map_to_region(0.5, 0.05)
    m.reset_smoothing()
    _, bottom_y = m.map_to_region(0.5, 0.95)
    assert top_y > bottom_y


def test_output_clamped_to_region():
    m = _calibrated_mapper()
    m.reset_smoothing()
    x, y = m.map_to_region(2.0, -1.0)  # inputs out of range -> clamped in & out
    assert 0.0 <= x <= WIDTH - 1
    assert 0.0 <= y <= HEIGHT - 1


def test_fallback_before_calibration_flips_y():
    m = RegionMapper(WIDTH, HEIGHT, smoothing=1.0)
    assert not m.has_calibration()
    _, top_y = m.map_to_region(0.5, 0.0)
    m.reset_smoothing()
    _, bottom_y = m.map_to_region(0.5, 1.0)
    assert top_y == pytest.approx(HEIGHT - 1)
    assert bottom_y == pytest.approx(0.0)


def test_calibrate_needs_four_points():
    m = RegionMapper(WIDTH, HEIGHT)
    ok, msg = m.calibrate([(0, 0), (1, 1)], [(0, 0), (1, 1)])
    assert not ok and "at least 4" in msg
