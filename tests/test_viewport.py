"""Unit tests for screen-point -> region-pixel conversion (cursor.viewport).

Uses the exact geometry measured from the live session: window (0,0) 1512x838 pt,
region offset (4,165) size 2477x1458 px, pixel_size 2.0 (Retina).
"""

from __future__ import annotations

import pytest

from client.cursor.viewport import ViewportScreenRect

GEO = {
    "window": {"x": 0, "y": 0, "width": 1512, "height": 838},
    "region": {"x": 4, "y": 165, "width": 2477, "height": 1458},
    "pixel_size": 2.0,
}


def _rect() -> ViewportScreenRect:
    return ViewportScreenRect.from_geometry(GEO)


def test_region_edges_in_points():
    r = _rect()
    assert r.left_pt == pytest.approx(2.0)
    assert r.top_pt == pytest.approx(26.5)
    assert r.bottom_pt == pytest.approx(755.5)


def test_region_corners_map_to_pixel_corners():
    r = _rect()
    # screen top-left of region -> region px (0, height) since region y is bottom-up
    assert r.screen_to_region(2.0, 26.5) == pytest.approx((0.0, 1458.0))
    # screen bottom-right of region -> (width, 0)
    assert r.screen_to_region(1240.5, 755.5) == pytest.approx((2477.0, 0.0))


def test_region_center_maps_to_center():
    r = _rect()
    cx_pt = 2.0 + (1240.5 - 2.0) / 2
    cy_pt = 26.5 + (755.5 - 26.5) / 2
    rx, ry = r.screen_to_region(cx_pt, cy_pt)
    assert rx == pytest.approx(2477 / 2, abs=1.0)
    assert ry == pytest.approx(1458 / 2, abs=1.0)


def test_contains_screen_point():
    r = _rect()
    assert r.contains_screen_point(600, 400)  # inside the viewport
    assert not r.contains_screen_point(1400, 400)  # right of the region (N-panel area)
    assert not r.contains_screen_point(600, 10)  # above the region (header)
