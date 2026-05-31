"""handTrack-style hand mouse controller behavior (Phase 3)."""

from __future__ import annotations

from client.cursor.displays import DisplayGeometry
from client.cursor.types import TrackerHealth
from client.gestures.hand_mouse import (
    HandMouseController,
    HandMouseCursorProvider,
    HandMouseMode,
)


class _Lm:
    def __init__(self, x: float, y: float, z: float = 0.0):
        self.x = x
        self.y = y
        self.z = z


class _Obs:
    def __init__(
        self,
        image_landmarks: list[_Lm],
        world_landmarks: list[_Lm] | None = None,
        handedness: str = "Right",
        handedness_score: float = 0.9,
    ):
        self.image_landmarks = image_landmarks
        self.world_landmarks = world_landmarks
        self.handedness = handedness
        self.handedness_score = handedness_score


class FakeMouse:
    def __init__(self) -> None:
        self.x = 500.0
        self.y = 250.0
        self.left_down = False
        self.middle_down = False
        self.shift_down = False
        self.left_clicks = 0
        self.scrolls: list[int] = []
        self.moves: list[tuple[int, int]] = []

    def position(self):
        return self.x, self.y

    def move_to(self, x, y):
        self.x = float(x)
        self.y = float(y)
        self.moves.append((int(round(x)), int(round(y))))

    def press_middle(self):
        self.middle_down = True

    def release_middle(self):
        self.middle_down = False

    def press_left(self):
        self.left_down = True

    def release_left(self):
        self.left_down = False

    def press_shift(self):
        self.shift_down = True

    def release_shift(self):
        self.shift_down = False

    def scroll(self, amount):
        self.scrolls.append(int(amount))


class _Tracker:
    def __init__(self, obs: _Obs | None):
        self.obs = obs

    def start(self):
        return True

    def stop(self):
        pass

    def get_health(self):
        return TrackerHealth(
            running=True,
            last_error=None,
            last_seen_ts=1.0,
            frames_seen=1,
        )

    def get_hand_observations(self):
        return self.obs, None

    def pump_preview(self):
        pass


def _hand() -> list[_Lm]:
    pts = [_Lm(0.5, 0.5) for _ in range(21)]
    pts[0] = _Lm(0.50, 0.80)  # wrist
    pts[4] = _Lm(0.50, 0.50)  # thumb tip
    pts[8] = _Lm(0.72, 0.30)  # index tip
    pts[12] = _Lm(0.50, 0.20)  # middle tip
    pts[13] = _Lm(0.55, 0.45)  # ring MCP, handTrack cursor anchor

    # index extended; other fingers curled by default.
    pts[6] = _Lm(0.70, 0.46)
    pts[10] = _Lm(0.50, 0.10)
    pts[14] = _Lm(0.50, 0.10)
    pts[18] = _Lm(0.50, 0.10)
    pts[16] = _Lm(0.50, 0.70)
    pts[20] = _Lm(0.50, 0.70)
    return pts


def _pinch_hand() -> list[_Lm]:
    pts = _hand()
    pts[8] = _Lm(0.58, 0.50)  # adaptive threshold sees this as thumb/index touch.
    return pts


def _middle_pinch_hand(middle_y: float) -> list[_Lm]:
    pts = _hand()
    pts[12] = _Lm(0.52, middle_y)
    pts[8] = _Lm(0.75, 0.30)
    return pts


def _left_drag_hand() -> list[_Lm]:
    pts = _pinch_hand()
    pts[8] = _Lm(0.52, 0.50)
    pts[12] = _Lm(0.52, 0.50)
    return pts


def _ring_pinch_hand() -> list[_Lm]:
    pts = _hand()
    pts[16] = _Lm(0.55, 0.50)
    pts[8] = _Lm(0.75, 0.30)
    pts[12] = _Lm(0.50, 0.20)
    return pts


def _pinky_pinch_hand() -> list[_Lm]:
    pts = _hand()
    pts[20] = _Lm(0.55, 0.50)
    pts[8] = _Lm(0.75, 0.30)
    pts[12] = _Lm(0.50, 0.20)
    pts[16] = _Lm(0.50, 0.70)
    return pts


def _peace_hand() -> list[_Lm]:
    pts = _hand()
    pts[8] = _Lm(0.45, 0.20)
    pts[12] = _Lm(0.55, 0.20)
    pts[16] = _Lm(0.60, 0.72)
    pts[20] = _Lm(0.70, 0.72)
    pts[6] = _Lm(0.45, 0.40)
    pts[10] = _Lm(0.55, 0.40)
    pts[14] = _Lm(0.60, 0.45)
    pts[18] = _Lm(0.70, 0.45)
    return pts


def _palm_hand() -> list[_Lm]:
    pts = _hand()
    pts[8] = _Lm(0.40, 0.20)
    pts[12] = _Lm(0.50, 0.18)
    pts[16] = _Lm(0.60, 0.20)
    pts[20] = _Lm(0.70, 0.22)
    for tip, pip in ((8, 6), (12, 10), (16, 14), (20, 18)):
        pts[pip] = _Lm(pts[tip].x, pts[tip].y + 0.16)
    return pts


def _fist_hand() -> list[_Lm]:
    pts = _hand()
    for tip, pip in ((8, 6), (12, 10), (16, 14), (20, 18)):
        pts[tip] = _Lm(0.5, 0.75)
        pts[pip] = _Lm(0.5, 0.45)
    pts[4] = _Lm(0.2, 0.5)
    return pts


def _world_from_image(image: list[_Lm]) -> list[_Lm]:
    return [_Lm(p.x, p.y, p.z) for p in image]


def _controller(mouse: FakeMouse) -> HandMouseController:
    return HandMouseController(
        backend=mouse,
        screen=DisplayGeometry(display_id=1, x=0, y=0, width=1000, height=500),
        smoothing=1.0,
        jitter_threshold=0.0,
        mode_hold_frames=1,
    )


def test_point_moves_mouse_with_inner_area_mapping():
    mouse = FakeMouse()
    controller = _controller(mouse)

    event = controller.update(_hand())

    assert event.mode == HandMouseMode.POINT
    assert event.touches == ()
    assert event.extended_fingers == ("index",)
    assert mouse.moves[-1][0] > 500
    assert mouse.moves[-1][1] < 250
    assert not mouse.left_down
    assert not mouse.middle_down
    assert not mouse.shift_down


def test_index_thumb_touch_mouse_down_release_mouse_up_like_handtrack():
    mouse = FakeMouse()
    controller = HandMouseController(
        backend=mouse,
        screen=DisplayGeometry(display_id=1, x=0, y=0, width=1000, height=500),
        smoothing=1.0,
        jitter_threshold=0.0,
        mode_hold_frames=3,
    )

    first = controller.update(_pinch_hand())
    assert first.mode == HandMouseMode.LEFT_DRAG
    assert first.touches == ("index",)
    assert first.touch_threshold is not None and first.touch_threshold > 0
    assert mouse.left_down

    second = controller.update(_hand())
    assert second.mode == HandMouseMode.POINT
    assert second.touches == ()
    assert not mouse.left_down
    assert not mouse.middle_down


def test_thumb_index_hold_holds_left_drag():
    mouse = FakeMouse()
    controller = _controller(mouse)

    event = controller.update(_pinch_hand())

    assert event.mode == HandMouseMode.LEFT_DRAG
    assert event.touches == ("index",)
    assert mouse.left_down
    assert not mouse.middle_down
    assert not mouse.shift_down


def test_thumb_index_middle_still_prefers_left_drag_over_zoom():
    mouse = FakeMouse()
    controller = _controller(mouse)

    event = controller.update(_left_drag_hand())

    assert event.mode == HandMouseMode.LEFT_DRAG
    assert event.touches == ("index", "middle")
    assert mouse.left_down
    assert not mouse.middle_down
    assert not mouse.shift_down


def test_peace_sign_holds_shift_middle_for_blender_pan():
    mouse = FakeMouse()
    controller = _controller(mouse)

    event = controller.update(_peace_hand())

    assert event.mode == HandMouseMode.PAN_DRAG
    assert event.touches == ()
    assert event.extended_fingers == ("index", "middle")
    assert not mouse.left_down
    assert mouse.middle_down
    assert mouse.shift_down


def test_open_palm_only_points_to_avoid_accidental_pan():
    mouse = FakeMouse()
    controller = _controller(mouse)

    event = controller.update(_palm_hand())

    assert event.mode == HandMouseMode.POINT
    assert event.touches == ()
    assert event.extended_fingers == ("index", "middle", "ring", "pinky")
    assert not mouse.left_down
    assert not mouse.middle_down
    assert not mouse.shift_down


def test_ring_thumb_touch_holds_middle_drag_for_blender_orbit():
    mouse = FakeMouse()
    controller = _controller(mouse)

    event = controller.update(_ring_pinch_hand())

    assert event.mode == HandMouseMode.ORBIT_DRAG
    assert event.touches == ("ring",)
    assert not mouse.left_down
    assert mouse.middle_down
    assert not mouse.shift_down


def test_middle_thumb_vertical_motion_scrolls_for_blender_zoom():
    mouse = FakeMouse()
    controller = _controller(mouse)

    first = controller.update(_middle_pinch_hand(0.54))
    second = controller.update(_middle_pinch_hand(0.49))

    assert first.mode == HandMouseMode.SCROLL
    assert second.mode == HandMouseMode.SCROLL
    assert first.touches == ("middle",)
    assert second.touches == ("middle",)
    assert mouse.scrolls[-1] > 0
    assert not mouse.left_down
    assert not mouse.middle_down
    assert not mouse.shift_down


def test_peace_sign_replaces_precision_mode_for_pan():
    mouse = FakeMouse()
    controller = _controller(mouse)

    event = controller.update(_peace_hand())

    assert event.mode == HandMouseMode.PAN_DRAG
    assert event.touches == ()
    assert event.extended_fingers == ("index", "middle")
    assert not mouse.left_down
    assert mouse.middle_down
    assert mouse.shift_down


def test_pinky_thumb_touch_releases_all_buttons():
    mouse = FakeMouse()
    controller = _controller(mouse)
    controller.update(_pinch_hand())

    event = controller.update(_pinky_pinch_hand())

    assert event.mode == HandMouseMode.RELEASE
    assert event.touches == ("pinky",)
    assert not mouse.left_down
    assert not mouse.middle_down
    assert not mouse.shift_down


def test_low_confidence_hand_releases_buttons():
    mouse = FakeMouse()
    controller = _controller(mouse)
    controller.update(_pinch_hand())

    event = controller.update(_Obs(_pinch_hand(), handedness_score=0.2))

    assert event.mode == HandMouseMode.IDLE
    assert event.confidence == 0.2
    assert not mouse.left_down
    assert not mouse.middle_down
    assert not mouse.shift_down


def test_handtrack_click_uses_image_landmark_distance_not_world_override():
    mouse = FakeMouse()
    controller = _controller(mouse)
    image = _hand()
    image[8] = _Lm(0.72, 0.30)  # image landmarks alone are not pinched.
    world = _world_from_image(image)
    world[0] = _Lm(0.00, -0.08, 0.00)
    world[4] = _Lm(0.00, 0.00, 0.00)
    world[8] = _Lm(0.01, 0.00, 0.00)
    world[12] = _Lm(0.00, 0.25, 0.00)
    world[5] = _Lm(-0.05, 0.00, 0.00)
    world[17] = _Lm(0.05, 0.00, 0.00)

    event = controller.update(_Obs(image, world))

    assert event.mode != HandMouseMode.LEFT_DRAG
    assert event.handedness == "Right"
    assert event.confidence == 0.9
    assert not mouse.left_down
    assert not mouse.middle_down


def test_fist_releases_all_buttons():
    mouse = FakeMouse()
    controller = _controller(mouse)
    controller.update(_peace_hand())

    event = controller.update(_fist_hand())

    assert event.mode == HandMouseMode.FIST
    assert not mouse.left_down
    assert not mouse.middle_down
    assert not mouse.shift_down


def test_orbit_releases_middle_when_thumb_ring_separates():
    mouse = FakeMouse()
    controller = _controller(mouse)
    controller.update(_ring_pinch_hand())
    assert mouse.middle_down

    event = controller.update(_hand())

    assert event.mode == HandMouseMode.POINT
    assert event.touches == ()
    assert not mouse.left_down
    assert not mouse.middle_down
    assert not mouse.shift_down


def test_pan_releases_shift_middle_when_peace_sign_closes_to_point():
    mouse = FakeMouse()
    controller = _controller(mouse)
    controller.update(_peace_hand())
    assert mouse.middle_down
    assert mouse.shift_down

    event = controller.update(_hand())

    assert event.mode == HandMouseMode.POINT
    assert event.extended_fingers == ("index",)
    assert not mouse.left_down
    assert not mouse.middle_down
    assert not mouse.shift_down


def test_missing_hand_releases_all_buttons():
    mouse = FakeMouse()
    controller = _controller(mouse)
    controller.update(_pinch_hand())

    event = controller.update(None)

    assert event.mode == HandMouseMode.IDLE
    assert not mouse.left_down
    assert not mouse.middle_down
    assert not mouse.shift_down


def test_debug_monitor_reports_touch_and_extended_fingers(capsys):
    mouse = FakeMouse()
    tracker = _Tracker(_Obs(_pinch_hand()))
    provider = HandMouseCursorProvider(
        tracker=tracker,
        controller=_controller(mouse),
        debug=True,
    )

    sample = provider.get_cursor()
    output = capsys.readouterr().out
    status = provider.status()

    assert sample is not None
    assert "mode=left_drag" in output
    assert "touch=index" in output
    assert status["mode"] == "left_drag"
    assert status["touches"] == ["index"]
    assert status["extended_fingers"] == []


def test_left_drag_releases_on_fist():
    mouse = FakeMouse()
    controller = _controller(mouse)
    controller.update(_left_drag_hand())

    event = controller.update(_fist_hand())

    assert event.mode == HandMouseMode.FIST
    assert not mouse.left_down


def test_camera_drag_modes_require_consecutive_frames_before_pressing():
    mouse = FakeMouse()
    controller = HandMouseController(
        backend=mouse,
        screen=DisplayGeometry(display_id=1, x=0, y=0, width=1000, height=500),
        smoothing=1.0,
        jitter_threshold=0.0,
        mode_hold_frames=2,
    )

    first = controller.update(_ring_pinch_hand())
    assert first.mode == HandMouseMode.POINT
    assert first.touches == ("ring",)
    assert not mouse.middle_down

    second = controller.update(_ring_pinch_hand())
    assert second.mode == HandMouseMode.ORBIT_DRAG
    assert second.touches == ("ring",)
    assert not mouse.left_down
    assert mouse.middle_down
