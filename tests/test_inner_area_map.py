"""Tests for the handTrack-style absolute linear inner-area mapping in
CursorMapper (the default hand->screen map, replacing the cv2 homography).
"""

from __future__ import annotations

import pytest

from client.cursor.mapper import CursorMapper, ScreenGeometry

W, H = 1512, 982


def _mapper(inner: float = 0.7) -> CursorMapper:
    # smoothing=1.0 -> no EMA lag, so we test the raw mapping
    return CursorMapper(
        screen_geometry=ScreenGeometry(W, H), smoothing=1.0, inner_area_percent=inner
    )


def test_no_homography_by_default():
    assert _mapper().has_calibration() is False


def test_center_maps_to_screen_center():
    c = _mapper().update_from_normalized(0.5, 0.5)
    assert c.x == pytest.approx((W - 1) // 2, abs=1)
    assert c.y == pytest.approx((H - 1) // 2, abs=1)


def test_inner_edges_map_to_screen_corners():
    m = _mapper(inner=0.7)
    margin = (1.0 - 0.7) / 2.0  # 0.15
    tl = m.update_from_normalized(margin, margin)
    m.reset()
    br = m.update_from_normalized(1.0 - margin, 1.0 - margin)
    assert (tl.x, tl.y) == (0, 0)
    assert (br.x, br.y) == (W - 1, H - 1)


def test_beyond_inner_area_clamps_to_screen():
    m = _mapper(inner=0.7)
    tl = m.update_from_normalized(0.0, 0.0)
    m.reset()
    br = m.update_from_normalized(1.0, 1.0)
    assert (tl.x, tl.y) == (0, 0)
    assert (br.x, br.y) == (W - 1, H - 1)
