"""client.cursor.webcam_tracker

MediaPipe fingertip + full-landmark tracker (reused from Wand; extended in Step 10).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import cv2  # type: ignore
import mediapipe as mp  # type: ignore
from mediapipe.tasks import python as mp_tasks_python  # type: ignore
from mediapipe.tasks.python import vision as mp_vision  # type: ignore

from .mapper import get_main_display_size
from .types import HandObservation, NormalizedSample, TrackerHealth


def _bundled_model_path() -> Path:
    return Path(__file__).resolve().parents[1] / "models" / "hand_landmarker.task"


def _resolve_model_path() -> Path:
    path = _bundled_model_path()
    if not path.exists():
        raise FileNotFoundError(
            f"bundled hand landmarker model missing. Expected read-only asset at: {path}"
        )
    return path


class WebcamFingerTracker:
    """Detect the index fingertip in webcam frames; output normalized coords."""

    def __init__(
        self,
        camera_index: int = 0,
        mirror: bool = True,
        preview_enabled: bool = True,
        preview_window_enabled: bool = True,
        min_detection_confidence: float = 0.6,
        min_tracking_confidence: float = 0.5,
        index_tip_id: int = 8,
        num_hands: int = 1,
        preview_scale: float = 0.24,
        preview_margin: int = 24,
    ) -> None:
        self.camera_index = int(camera_index)
        self.mirror = bool(mirror)
        self.preview_enabled = bool(preview_enabled)
        self.preview_window_enabled = bool(preview_window_enabled)

        self.min_detection_confidence = float(min_detection_confidence)
        self.min_tracking_confidence = float(min_tracking_confidence)
        self.index_tip_id = int(index_tip_id)
        self.num_hands = int(max(1, num_hands))
        self.preview_scale = float(min(0.9, max(0.1, preview_scale)))
        self.preview_margin = int(max(0, preview_margin))

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._running = False
        self._last_error: Optional[str] = None
        self._latest: Optional[NormalizedSample] = None
        self._last_landmarks: list | None = None
        self._second_landmarks: list | None = None
        self._last_observation: HandObservation | None = None
        self._second_observation: HandObservation | None = None
        self._last_handedness: str | None = None
        self._second_handedness: str | None = None
        self._last_seen_ts: Optional[float] = None
        self._frames_seen = 0

        self._preview_window_name = "Hand Cursor Preview"
        self._preview_window_open = False
        self._preview_frame = None

    def start(self, timeout_s: float = 3.0) -> bool:
        if self._thread and self._thread.is_alive():
            return True

        self._last_error = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

        deadline = time.time() + max(0.1, timeout_s)
        while time.time() < deadline:
            if self._running:
                return True
            if self._last_error:
                return False
            if self._thread and not self._thread.is_alive():
                self._last_error = self._last_error or "tracker thread exited during startup"
                return False
            time.sleep(0.02)

        if self._thread and self._thread.is_alive() and self._last_error is None:
            return True

        self._last_error = self._last_error or f"tracker startup timed out after {timeout_s:.1f}s"
        return False

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self.close_preview_window()

    def is_running(self) -> bool:
        return self._running

    def get_last_error(self) -> Optional[str]:
        return self._last_error

    def get_latest_sample(self) -> Optional[NormalizedSample]:
        with self._lock:
            return self._latest

    def get_preview_frame(self):
        with self._lock:
            if self._preview_frame is None:
                return None
            return self._preview_frame.copy()

    def get_health(self) -> TrackerHealth:
        with self._lock:
            return TrackerHealth(
                running=self._running,
                last_error=self._last_error,
                last_seen_ts=self._last_seen_ts,
                frames_seen=self._frames_seen,
            )

    def pump_preview(self) -> None:
        if not self.preview_enabled or not self.preview_window_enabled:
            if self._preview_window_open:
                self.close_preview_window()
            return

        frame = self.get_preview_frame()
        if frame is None:
            return

        try:
            if not self._preview_window_open:
                cv2.namedWindow(self._preview_window_name, cv2.WINDOW_NORMAL)
                height, width = frame.shape[:2]
                preview_w = max(180, int(width * self.preview_scale))
                preview_h = max(120, int(height * self.preview_scale))
                cv2.resizeWindow(self._preview_window_name, preview_w, preview_h)

                screen_w, screen_h = get_main_display_size()
                pos_x = max(0, screen_w - preview_w - self.preview_margin)
                pos_y = max(0, screen_h - preview_h - self.preview_margin)
                cv2.moveWindow(self._preview_window_name, pos_x, pos_y)
                self._preview_window_open = True

            cv2.imshow(self._preview_window_name, frame)
            cv2.waitKey(1)
        except Exception as exc:
            self._last_error = str(exc)

    def close_preview_window(self) -> None:
        if self._preview_window_open:
            try:
                cv2.destroyWindow(self._preview_window_name)
            except Exception:
                pass
            self._preview_window_open = False

    def _create_landmarker(self):
        model_path = _resolve_model_path()
        options = mp_vision.HandLandmarkerOptions(
            base_options=mp_tasks_python.BaseOptions(model_asset_path=str(model_path)),
            running_mode=mp_vision.RunningMode.VIDEO,
            num_hands=self.num_hands,
            min_hand_detection_confidence=self.min_detection_confidence,
            min_hand_presence_confidence=self.min_tracking_confidence,
            min_tracking_confidence=self.min_tracking_confidence,
        )
        return mp_vision.HandLandmarker.create_from_options(options)

    def get_landmarks(self) -> tuple[list | None, list | None]:
        """Latest primary/secondary hand landmark lists (21 points each).

        Hands are ordered by MediaPipe handedness confidence. Phase 3's
        handTrack-style mouse mode uses one active hand by default; the second
        slot remains available for later experiments without changing the API.
        """
        with self._lock:
            return self._last_landmarks, self._second_landmarks

    def get_hand_observations(self) -> tuple[HandObservation | None, HandObservation | None]:
        """Latest MediaPipe hand observations.

        This exposes the full Hand Landmarker output used by Phase 3 gestures:
        image landmarks for cursor placement, world landmarks for shape checks,
        and handedness/confidence for filtering/debugging.
        """
        with self._lock:
            return self._last_observation, self._second_observation

    def _detect_fingertip(self, frame_bgr, landmarker) -> Optional[Tuple[float, float, float]]:
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        ts_ms = int(time.monotonic() * 1000)

        result = landmarker.detect_for_video(mp_image, ts_ms)
        if not result.hand_landmarks:
            with self._lock:
                self._last_landmarks = None
                self._second_landmarks = None
                self._last_observation = None
                self._second_observation = None
                self._last_handedness = None
                self._second_handedness = None
            return None

        now = time.time()
        world_landmarks = getattr(result, "hand_world_landmarks", None) or []
        hands = sorted(
            (
                (
                    landmarks,
                    world_landmarks[idx] if len(world_landmarks) > idx else None,
                    _hand_score(result.handedness, idx),
                    _hand_label(result.handedness, idx),
                    _palm_span(landmarks),
                )
                for idx, landmarks in enumerate(result.hand_landmarks)
            ),
            key=lambda item: (item[2], item[4]),
            reverse=True,
        )
        primary_landmarks, primary_world, primary_score, primary_label, _primary_span = hands[0]
        secondary = hands[1] if len(hands) > 1 else None
        primary_observation = HandObservation(
            image_landmarks=primary_landmarks,
            world_landmarks=primary_world,
            handedness=primary_label,
            handedness_score=primary_score,
            ts=now,
        )
        secondary_observation = (
            HandObservation(
                image_landmarks=secondary[0],
                world_landmarks=secondary[1],
                handedness=secondary[3],
                handedness_score=secondary[2],
                ts=now,
            )
            if secondary is not None
            else None
        )

        with self._lock:
            self._last_landmarks = primary_landmarks
            self._last_observation = primary_observation
            self._last_handedness = primary_label
            if secondary is not None:
                self._second_landmarks = secondary[0]
                self._second_observation = secondary_observation
                self._second_handedness = secondary[3]
            else:
                self._second_landmarks = None
                self._second_observation = None
                self._second_handedness = None

        landmarks = primary_landmarks
        if not 0 <= self.index_tip_id < len(landmarks):
            return None

        tip = landmarks[self.index_tip_id]
        score = primary_score

        return (
            min(1.0, max(0.0, float(tip.x))),
            min(1.0, max(0.0, float(tip.y))),
            score,
        )

    def _run(self) -> None:
        cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            self._last_error = f"failed to open camera index {self.camera_index}"
            self._running = False
            return

        landmarker = None
        try:
            landmarker = self._create_landmarker()
            self._running = True

            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.01)
                    continue

                if self.mirror:
                    frame = cv2.flip(frame, 1)

                result = self._detect_fingertip(frame, landmarker)
                preview_frame = frame.copy() if self.preview_enabled else None

                if result is not None:
                    x_norm, y_norm, score = result
                    sample = NormalizedSample(x=x_norm, y=y_norm, ts=time.time(), confidence=score)
                    with self._lock:
                        self._latest = sample
                        self._last_seen_ts = sample.ts
                        self._frames_seen += 1

                    if preview_frame is not None:
                        height, width = preview_frame.shape[:2]
                        _draw_hand_landmarks(preview_frame, self._last_landmarks)
                        px = int(x_norm * (width - 1))
                        py = int(y_norm * (height - 1))
                        cv2.circle(preview_frame, (px, py), 10, (0, 255, 255), -1)

                if preview_frame is not None:
                    with self._lock:
                        self._preview_frame = preview_frame
        except Exception as exc:
            self._last_error = str(exc)
        finally:
            self._running = False
            if landmarker is not None:
                try:
                    landmarker.close()
                except Exception:
                    pass
            cap.release()


def _hand_score(handedness, index: int) -> float:
    try:
        if handedness and len(handedness) > index and handedness[index]:
            return float(handedness[index][0].score)
    except Exception:
        pass
    return 1.0


def _hand_label(handedness, index: int) -> str | None:
    try:
        if handedness and len(handedness) > index and handedness[index]:
            return str(handedness[index][0].category_name)
    except Exception:
        pass
    return None


def _palm_span(landmarks) -> float:
    try:
        if len(landmarks) > 17:
            return _dist2(landmarks[5], landmarks[17])
    except Exception:
        pass
    return 0.0


def _dist2(a, b) -> float:
    return ((float(a.x) - float(b.x)) ** 2 + (float(a.y) - float(b.y)) ** 2) ** 0.5


def _draw_hand_landmarks(frame, landmarks) -> None:
    if not landmarks:
        return
    height, width = frame.shape[:2]
    points: list[tuple[int, int]] = []
    for lm in landmarks:
        points.append(
            (
                int(min(1.0, max(0.0, float(lm.x))) * (width - 1)),
                int(min(1.0, max(0.0, float(lm.y))) * (height - 1)),
            )
        )
    for a, b in (
        (0, 1),
        (1, 2),
        (2, 3),
        (3, 4),
        (0, 5),
        (5, 6),
        (6, 7),
        (7, 8),
        (5, 9),
        (9, 10),
        (10, 11),
        (11, 12),
        (9, 13),
        (13, 14),
        (14, 15),
        (15, 16),
        (13, 17),
        (17, 18),
        (18, 19),
        (19, 20),
        (0, 17),
    ):
        cv2.line(frame, points[a], points[b], (80, 200, 255), 1)
    for point in points:
        cv2.circle(frame, point, 2, (0, 220, 120), -1)
