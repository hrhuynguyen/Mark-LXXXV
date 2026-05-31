"""handTrack-style hand-to-mouse controller for Phase 3.

This is adapted from https://github.com/small-cactus/handTrack:
* ring-finger MCP drives the cursor target;
* a centered "inner camera area" maps to the full screen;
* thumb/index touch controls left click/drag;
* thumb/middle touch scrolls from vertical finger motion;
* thumb/ring touch orbits;
* thumb/pinky touch is an explicit neutral/release gesture;
* tiny motion is ignored to reduce jitter.

Forge maps those mouse gestures onto Blender-friendly defaults:
* thumb/index touch holds left mouse; release sends mouse up, matching handTrack;
* thumb/ring drag uses the middle mouse button, so Blender orbits;
* open-palm drag holds Shift+middle mouse, so Blender pans;
* thumb/middle vertical motion scrolls, so Blender zooms.

The gesture classification also follows the preprocessing pattern from
Kazuhito00/hand-gesture-recognition-using-mediapipe: landmarks are converted to
wrist-relative normalized coordinates before static hand-shape classification.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from client.cursor.displays import DisplayGeometry, get_builtin_display_geometry
from client.cursor.types import CursorSample, TrackerHealth


# Google MediaPipe Hand Landmarker emits 21 hand landmarks. The gesture layer
# only needs fingertips/PIP/MCP joints, but keeping the indexes named makes the
# thumb-touch mapping easy to audit while tuning live.
WRIST = 0
THUMB_TIP = 4
INDEX_MCP = 5
INDEX_PIP = 6
INDEX_TIP = 8
MIDDLE_MCP = 9
MIDDLE_PIP = 10
MIDDLE_TIP = 12
RING_MCP = 13
RING_PIP = 14
RING_TIP = 16
PINKY_MCP = 17
PINKY_PIP = 18
PINKY_TIP = 20

FINGER_SEGMENTS = {
    "index": (INDEX_TIP, INDEX_PIP, INDEX_MCP),
    "middle": (MIDDLE_TIP, MIDDLE_PIP, MIDDLE_MCP),
    "ring": (RING_TIP, RING_PIP, RING_MCP),
    "pinky": (PINKY_TIP, PINKY_PIP, PINKY_MCP),
}
THUMB_TOUCH_TIPS = {
    "index": INDEX_TIP,
    "middle": MIDDLE_TIP,
    "ring": RING_TIP,
    "pinky": PINKY_TIP,
}
PALM_CENTER_POINTS = (WRIST, INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP)


class HandMouseMode(str, Enum):
    IDLE = "idle"
    POINT = "point"
    PRECISION_POINT = "precision_point"
    LEFT_DRAG = "left_drag"
    ORBIT_DRAG = "orbit_drag"
    PAN_DRAG = "pan_drag"
    SCROLL = "scroll"
    FIST = "fist"
    RELEASE = "release"


@dataclass(frozen=True)
class HandMouseEvent:
    mode: HandMouseMode
    x: int | None = None
    y: int | None = None
    scroll_y: int = 0
    moved: bool = False
    handedness: str | None = None
    confidence: float | None = None
    touches: tuple[str, ...] = ()
    extended_fingers: tuple[str, ...] = ()
    touch_threshold: float | None = None


class MouseBackend(Protocol):
    def position(self) -> tuple[float, float]: ...

    def move_to(self, x: float, y: float) -> None: ...

    def press_middle(self) -> None: ...

    def release_middle(self) -> None: ...

    def press_left(self) -> None: ...

    def release_left(self) -> None: ...

    def press_shift(self) -> None: ...

    def release_shift(self) -> None: ...

    def scroll(self, amount: int) -> None: ...


class PynputMouseBackend:
    """Small wrapper over pynput, used instead of adding pyautogui."""

    def __init__(self) -> None:
        from pynput.keyboard import Controller as KeyboardController
        from pynput.keyboard import Key
        from pynput.mouse import Button, Controller as MouseController

        self._mouse = MouseController()
        self._keyboard = KeyboardController()
        self._button = Button
        self._key = Key

    def position(self) -> tuple[float, float]:
        x, y = self._mouse.position
        return float(x), float(y)

    def move_to(self, x: float, y: float) -> None:
        self._mouse.position = (int(round(x)), int(round(y)))

    def press_middle(self) -> None:
        self._mouse.press(self._button.middle)

    def release_middle(self) -> None:
        self._mouse.release(self._button.middle)

    def press_left(self) -> None:
        self._mouse.press(self._button.left)

    def release_left(self) -> None:
        self._mouse.release(self._button.left)

    def press_shift(self) -> None:
        self._keyboard.press(self._key.shift)

    def release_shift(self) -> None:
        self._keyboard.release(self._key.shift)

    def scroll(self, amount: int) -> None:
        self._mouse.scroll(0, int(amount))


@dataclass
class HandMouseController:
    """Translate 21 hand landmarks into real mouse movement/actions."""

    backend: MouseBackend
    screen: DisplayGeometry | None = None
    inner_area_percent: float = 0.70
    touch_threshold: float = 0.19
    min_touch_threshold: float = 0.025
    jitter_threshold: float = 0.003
    smoothing: float = 0.35
    precision_smoothing: float = 0.18
    scroll_threshold: float = 0.005
    scroll_sensitivity: float = 0.05
    mode_hold_frames: int = 2
    min_hand_confidence: float = 0.45

    _current_x: float | None = None
    _current_y: float | None = None
    _left_down: bool = False
    _middle_down: bool = False
    _shift_down: bool = False
    _previous_middle_y: float | None = None
    _active_mode: HandMouseMode = HandMouseMode.IDLE
    _pending_mode: HandMouseMode = HandMouseMode.IDLE
    _pending_count: int = 0
    _last_event: HandMouseEvent = HandMouseEvent(HandMouseMode.IDLE)

    def __post_init__(self) -> None:
        if self.screen is None:
            self.screen = get_builtin_display_geometry()
        self.inner_area_percent = max(0.25, min(1.0, float(self.inner_area_percent)))
        self.smoothing = max(0.0, min(1.0, float(self.smoothing)))
        self.precision_smoothing = max(0.0, min(1.0, float(self.precision_smoothing)))
        self.mode_hold_frames = max(1, int(self.mode_hold_frames))
        self.min_hand_confidence = max(0.0, min(1.0, float(self.min_hand_confidence)))

    def update(self, hand: Any | None) -> HandMouseEvent:
        landmarks, world_landmarks, handedness, confidence = _extract_hand_data(hand)
        if not landmarks or len(landmarks) < 21:
            self.release_all()
            self._previous_middle_y = None
            self._reset_mode_state()
            self._last_event = HandMouseEvent(HandMouseMode.IDLE)
            return self._last_event
        if confidence is not None and confidence < self.min_hand_confidence:
            self.release_all()
            self._previous_middle_y = None
            self._reset_mode_state()
            self._last_event = HandMouseEvent(
                HandMouseMode.IDLE,
                handedness=handedness,
                confidence=confidence,
            )
            return self._last_event

        pose = self._pose(landmarks, world_landmarks)
        desired_mode = self._desired_mode(pose)
        mode = self._stable_mode(desired_mode)
        smoothing = self.precision_smoothing if mode == HandMouseMode.PRECISION_POINT else None
        x, y, moved = self._move_pointer(pose.anchor_x, pose.anchor_y, smoothing=smoothing)

        def finish(event_mode: HandMouseMode, *, scroll_y: int = 0) -> HandMouseEvent:
            self._last_event = HandMouseEvent(
                event_mode,
                x=x,
                y=y,
                scroll_y=scroll_y,
                moved=moved,
                handedness=handedness,
                confidence=confidence,
                touches=pose.touches,
                extended_fingers=pose.extended_fingers,
                touch_threshold=pose.touch_threshold,
            )
            return self._last_event

        if mode in (HandMouseMode.FIST, HandMouseMode.RELEASE):
            self.release_all()
            self._previous_middle_y = None
            return finish(mode)

        if mode == HandMouseMode.SCROLL:
            self._set_left(False)
            self._set_middle(False)
            self._set_shift(False)
            scroll_y = self._scroll_from_middle_y(pose.middle_y)
            if scroll_y:
                self.backend.scroll(scroll_y)
            return finish(HandMouseMode.SCROLL, scroll_y=scroll_y)

        self._previous_middle_y = None

        if mode == HandMouseMode.LEFT_DRAG:
            self._set_middle(False)
            self._set_shift(False)
            self._set_left(True)
            return finish(HandMouseMode.LEFT_DRAG)

        if mode == HandMouseMode.ORBIT_DRAG:
            self._set_left(False)
            self._set_shift(False)
            self._set_middle(True)
            return finish(HandMouseMode.ORBIT_DRAG)

        if mode == HandMouseMode.PAN_DRAG:
            self._set_left(False)
            self._set_shift(True)
            self._set_middle(True)
            return finish(HandMouseMode.PAN_DRAG)

        self.release_all()
        return finish(mode)

    def release_all(self) -> None:
        self._set_left(False)
        self._set_middle(False)
        self._set_shift(False)

    def cursor_sample(self, *, source: str = "hand") -> CursorSample:
        x, y = self.backend.position()
        return CursorSample(x=int(round(x)), y=int(round(y)), ts=time.time(), source=source)

    def _pose(self, landmarks: list[Any], world_landmarks: list[Any] | None = None) -> "_Pose":
        metric_landmarks = world_landmarks if world_landmarks and len(world_landmarks) >= 21 else landmarks
        middle_tip = landmarks[MIDDLE_TIP]
        ring_mcp = landmarks[RING_MCP]

        palm_center_x = sum(float(landmarks[idx].x) for idx in PALM_CENTER_POINTS) / len(
            PALM_CENTER_POINTS
        )
        palm_center_y = sum(float(landmarks[idx].y) for idx in PALM_CENTER_POINTS) / len(
            PALM_CENTER_POINTS
        )
        normalized_landmarks = _normalize_landmarks(metric_landmarks)
        extended = {
            name: _finger_extended(landmarks, metric_landmarks, normalized_landmarks, *segment)
            for name, segment in FINGER_SEGMENTS.items()
        }
        hand_size = max(self.min_touch_threshold, _dist2d(landmarks[WRIST], middle_tip))
        touch_threshold = max(self.min_touch_threshold, self.touch_threshold * hand_size)
        touches = {
            name: _dist2d(landmarks[tip], landmarks[THUMB_TIP]) < touch_threshold
            for name, tip in THUMB_TOUCH_TIPS.items()
        }
        return _Pose(
            anchor_x=_clamp(
                float(ring_mcp.x) * 0.72 + palm_center_x * 0.28,
                0.0,
                1.0,
            ),
            anchor_y=_clamp(
                float(ring_mcp.y) * 0.72 + palm_center_y * 0.28,
                0.0,
                1.0,
            ),
            middle_y=float(middle_tip.y),
            touch_threshold=touch_threshold,
            index_thumb_touch=touches["index"],
            middle_thumb_touch=touches["middle"],
            ring_thumb_touch=touches["ring"],
            pinky_thumb_touch=touches["pinky"],
            index_extended=extended["index"],
            middle_extended=extended["middle"],
            ring_extended=extended["ring"],
            pinky_extended=extended["pinky"],
        )

    def _desired_mode(self, pose: "_Pose") -> HandMouseMode:
        if pose.is_release:
            return HandMouseMode.RELEASE
        if pose.is_fist:
            return HandMouseMode.FIST
        if pose.index_thumb_touch:
            return HandMouseMode.LEFT_DRAG
        if pose.middle_thumb_touch:
            return HandMouseMode.SCROLL
        if pose.ring_thumb_touch:
            return HandMouseMode.ORBIT_DRAG
        if pose.is_palm:
            return HandMouseMode.PAN_DRAG
        if pose.is_precision_point:
            return HandMouseMode.PRECISION_POINT
        return HandMouseMode.POINT

    def _stable_mode(self, desired: HandMouseMode) -> HandMouseMode:
        immediate = {
            HandMouseMode.IDLE,
            HandMouseMode.FIST,
            HandMouseMode.RELEASE,
            HandMouseMode.POINT,
            HandMouseMode.PRECISION_POINT,
            HandMouseMode.LEFT_DRAG,
        }
        if desired in immediate or desired == self._active_mode:
            self._active_mode = desired
            self._pending_mode = desired
            self._pending_count = 0
            return desired

        if desired == self._pending_mode:
            self._pending_count += 1
        else:
            self._pending_mode = desired
            self._pending_count = 1

        if self._pending_count >= self.mode_hold_frames:
            self._active_mode = desired
            self._pending_count = 0
            return desired
        return self._active_mode if self._active_mode != HandMouseMode.IDLE else HandMouseMode.POINT

    def _reset_mode_state(self) -> None:
        self._active_mode = HandMouseMode.IDLE
        self._pending_mode = HandMouseMode.IDLE
        self._pending_count = 0

    def _move_pointer(
        self,
        x_norm: float,
        y_norm: float,
        *,
        smoothing: float | None = None,
    ) -> tuple[int, int, bool]:
        assert self.screen is not None
        target_x, target_y = _inner_area_to_screen(
            float(x_norm),
            float(y_norm),
            self.screen,
            self.inner_area_percent,
        )
        if self._current_x is None or self._current_y is None:
            self._current_x, self._current_y = self.backend.position()

        dx = target_x - self._current_x
        dy = target_y - self._current_y
        diagonal = math.hypot(self.screen.width, self.screen.height)
        moved = False
        if diagonal <= 0 or math.hypot(dx, dy) / diagonal > self.jitter_threshold:
            alpha = self.smoothing if smoothing is None else smoothing
            self._current_x += dx * alpha
            self._current_y += dy * alpha
            self.backend.move_to(self._current_x, self._current_y)
            moved = True
        return int(round(self._current_x)), int(round(self._current_y)), moved

    def _scroll_from_middle_y(self, middle_y: float) -> int:
        if self._previous_middle_y is None:
            self._previous_middle_y = float(middle_y)
            return 0
        delta_y = float(middle_y) - self._previous_middle_y
        self._previous_middle_y = float(middle_y)
        if abs(delta_y) <= self.scroll_threshold:
            return 0
        assert self.screen is not None
        amount = -delta_y * self.screen.height * self.scroll_sensitivity
        if 0 < abs(amount) < 1:
            amount = 1 if amount > 0 else -1
        return int(round(amount))

    def _set_middle(self, down: bool) -> None:
        if down == self._middle_down:
            return
        if down:
            self.backend.press_middle()
        else:
            self.backend.release_middle()
        self._middle_down = down

    def _set_left(self, down: bool) -> None:
        if down == self._left_down:
            return
        if down:
            self.backend.press_left()
        else:
            self.backend.release_left()
        self._left_down = down

    def _set_shift(self, down: bool) -> None:
        if down == self._shift_down:
            return
        if down:
            self.backend.press_shift()
        else:
            self.backend.release_shift()
        self._shift_down = down


class HandMouseCursorProvider:
    """CursorProvider that controls and reports the real mouse from hand gestures."""

    def __init__(
        self,
        *,
        tracker,
        controller: HandMouseController | None = None,
        backend: MouseBackend | None = None,
        debug: bool = False,
    ) -> None:
        self.tracker = tracker
        self.controller = controller or HandMouseController(
            backend=backend or PynputMouseBackend(),
        )
        self.debug = bool(debug)
        self._last_error: str | None = None
        self._last_debug_signature: tuple[
            HandMouseMode,
            tuple[str, ...],
            tuple[str, ...],
        ] | None = None

    def start(self) -> bool:
        if not self.tracker.start():
            health = self.tracker.get_health()
            self._last_error = health.last_error or "hand mouse tracker failed to start"
            return False
        self._last_error = None
        return True

    def stop(self) -> None:
        self.controller.release_all()
        self.tracker.stop()

    def get_cursor(self) -> CursorSample | None:
        if hasattr(self.tracker, "get_hand_observations"):
            primary, _secondary = self.tracker.get_hand_observations()
        else:
            primary, _secondary = self.tracker.get_landmarks()
        event = self.controller.update(primary)
        signature = (event.mode, event.touches, event.extended_fingers)
        if self.debug and signature != self._last_debug_signature:
            conf = "" if event.confidence is None else f" confidence={event.confidence:.2f}"
            hand = "" if event.handedness is None else f" hand={event.handedness}"
            touches = "" if not event.touches else f" touch={'+'.join(event.touches)}"
            extended = (
                ""
                if not event.extended_fingers
                else f" extended={'+'.join(event.extended_fingers)}"
            )
            print(f"[Forge gesture] mode={event.mode.value}{touches}{extended}{hand}{conf}")
            self._last_debug_signature = signature
        return self.controller.cursor_sample(source="hand")

    def pump_ui(self) -> None:
        self.tracker.pump_preview()

    def status(self) -> dict[str, Any]:
        health: TrackerHealth = self.tracker.get_health()
        return {
            "source": "hand_mouse",
            "running": health.running,
            "last_error": self._last_error or health.last_error,
            "frames_seen": health.frames_seen,
            "mode": self.controller._last_event.mode.value,
            "handedness": self.controller._last_event.handedness,
            "confidence": self.controller._last_event.confidence,
            "touches": list(self.controller._last_event.touches),
            "extended_fingers": list(self.controller._last_event.extended_fingers),
            "touch_threshold": self.controller._last_event.touch_threshold,
        }


@dataclass(frozen=True)
class _Pose:
    anchor_x: float
    anchor_y: float
    middle_y: float
    touch_threshold: float
    index_thumb_touch: bool
    middle_thumb_touch: bool
    ring_thumb_touch: bool
    pinky_thumb_touch: bool
    index_extended: bool
    middle_extended: bool
    ring_extended: bool
    pinky_extended: bool

    @property
    def fingers_extended(self) -> int:
        return sum(
            1
            for value in (
                self.index_extended,
                self.middle_extended,
                self.ring_extended,
                self.pinky_extended,
            )
            if value
        )

    @property
    def extended_fingers(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, value in (
                ("index", self.index_extended),
                ("middle", self.middle_extended),
                ("ring", self.ring_extended),
                ("pinky", self.pinky_extended),
            )
            if value
        )

    @property
    def touches(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, value in (
                ("index", self.index_thumb_touch),
                ("middle", self.middle_thumb_touch),
                ("ring", self.ring_thumb_touch),
                ("pinky", self.pinky_thumb_touch),
            )
            if value
        )

    @property
    def is_fist(self) -> bool:
        return self.fingers_extended == 0 and not self.any_touch

    @property
    def is_palm(self) -> bool:
        return self.fingers_extended >= 4 and not self.any_touch

    @property
    def is_precision_point(self) -> bool:
        return (
            self.index_extended
            and self.middle_extended
            and not self.ring_extended
            and not self.pinky_extended
            and not self.any_touch
        )

    @property
    def is_release(self) -> bool:
        return self.pinky_thumb_touch

    @property
    def any_touch(self) -> bool:
        return (
            self.index_thumb_touch
            or self.middle_thumb_touch
            or self.ring_thumb_touch
            or self.pinky_thumb_touch
        )


def _inner_area_to_screen(
    x_norm: float,
    y_norm: float,
    screen: DisplayGeometry,
    inner_area_percent: float,
) -> tuple[float, float]:
    margin = (1.0 - inner_area_percent) / 2.0
    lo = margin
    hi = 1.0 - margin
    if hi <= lo:
        sx = x_norm
        sy = y_norm
    else:
        sx = (x_norm - lo) / (hi - lo)
        sy = (y_norm - lo) / (hi - lo)
    sx = _clamp(sx, 0.0, 1.0)
    sy = _clamp(sy, 0.0, 1.0)
    return screen.x + sx * (screen.width - 1), screen.y + sy * (screen.height - 1)


def _dist(a: Any, b: Any) -> float:
    dz = float(getattr(a, "z", 0.0)) - float(getattr(b, "z", 0.0))
    return math.sqrt(
        (float(a.x) - float(b.x)) ** 2
        + (float(a.y) - float(b.y)) ** 2
        + dz**2
    )


def _dist2d(a: Any, b: Any) -> float:
    return math.hypot(float(a.x) - float(b.x), float(a.y) - float(b.y))


def _finger_extended(
    image_landmarks: list[Any],
    metric_landmarks: list[Any],
    normalized_landmarks: list[tuple[float, float, float]],
    tip: int,
    pip: int,
    mcp: int,
) -> bool:
    """Classify extension from both vertical ordering and segment length.

    MediaPipe's image landmarks are normalized x/y camera coordinates. The
    y-order test keeps upright webcam use intuitive. When the Hand Landmarker
    also provides world landmarks, the wrist-distance check makes the
    classifier less sensitive to tilt and perspective.
    """

    image_tip = image_landmarks[tip]
    image_pip = image_landmarks[pip]
    metric_tip = metric_landmarks[tip]
    metric_pip = metric_landmarks[pip]
    metric_mcp = metric_landmarks[mcp]
    metric_wrist = metric_landmarks[0]
    vertical = float(image_tip.y) < float(image_pip.y) - 0.015
    length = _dist(metric_mcp, metric_tip) > _dist(metric_mcp, metric_pip) * 1.14
    away_from_wrist = _dist(metric_wrist, metric_tip) > _dist(metric_wrist, metric_pip) * 1.02
    normalized_length = _norm_dist(normalized_landmarks, tip, mcp) > _norm_dist(
        normalized_landmarks,
        pip,
        mcp,
    ) * 1.08
    return vertical and (length or normalized_length) and away_from_wrist


def _normalize_landmarks(landmarks: list[Any]) -> list[tuple[float, float, float]]:
    """Wrist-relative normalization inspired by Kazuhito00's MediaPipe sample."""

    if not landmarks:
        return []
    base_x = float(landmarks[0].x)
    base_y = float(landmarks[0].y)
    base_z = float(getattr(landmarks[0], "z", 0.0))
    relative = [
        (
            float(lm.x) - base_x,
            float(lm.y) - base_y,
            float(getattr(lm, "z", 0.0)) - base_z,
        )
        for lm in landmarks
    ]
    max_value = max(
        1e-6,
        max(abs(value) for point in relative for value in point),
    )
    return [(x / max_value, y / max_value, z / max_value) for x, y, z in relative]


def _norm_dist(points: list[tuple[float, float, float]], a: int, b: int) -> float:
    ax, ay, az = points[a]
    bx, by, bz = points[b]
    return math.sqrt((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2)


def _extract_hand_data(
    hand: Any | None,
) -> tuple[list[Any] | None, list[Any] | None, str | None, float | None]:
    if hand is None:
        return None, None, None, None
    if isinstance(hand, (list, tuple)):
        return list(hand), None, None, None
    image_landmarks = getattr(hand, "image_landmarks", None)
    world_landmarks = getattr(hand, "world_landmarks", None)
    handedness = getattr(hand, "handedness", None)
    confidence = getattr(hand, "handedness_score", None)
    return image_landmarks, world_landmarks, handedness, confidence


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))
