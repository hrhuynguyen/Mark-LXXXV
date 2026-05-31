"""client.cursor.provider

Hand cursor provider + guided 4-point calibration (lifted from Wand).

The hand drives a visible on-screen dot (ScreenDotOverlay); calibration shows a
ring (ScreenTargetOverlay) at each screen corner and the user moves the dot into
it and holds briefly. This is the easy "hand acts as a mouse" UX. Forge then
converts the screen cursor to Blender region pixels for picking
(client/cursor/viewport.py).
"""

from __future__ import annotations

import math
import statistics
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from .mapper import CursorMapper
from .types import CursorSample
from .ui_overlay import ScreenDotOverlay, ScreenTargetOverlay
from .webcam_tracker import WebcamFingerTracker


class CursorProvider(Protocol):
    def start(self) -> bool: ...
    def stop(self) -> None: ...
    def get_cursor(self) -> Optional[CursorSample]: ...
    def pump_ui(self) -> None: ...
    def status(self) -> Dict[str, Any]: ...


class MouseCursorProvider:
    def __init__(self) -> None:
        try:
            from pynput.mouse import Controller
        except Exception as exc:
            raise RuntimeError(f"pynput is required for MouseCursorProvider: {exc}")
        self._mouse = Controller()

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        return None

    def get_cursor(self) -> Optional[CursorSample]:
        x, y = self._mouse.position
        return CursorSample(x=int(x), y=int(y), ts=time.time(), source="mouse", confidence=1.0)

    def pump_ui(self) -> None:
        return None

    def status(self) -> Dict[str, Any]:
        return {"source": "mouse", "running": True, "last_error": None}


class HandCursorProvider:
    def __init__(
        self,
        *,
        camera_index: int = 0,
        smoothing: float = 0.35,
        stale_timeout_s: float = 0.4,
        tracker_start_timeout_s: float = 8.0,
        mirror: bool = True,
        preview: bool = True,
        preview_window: bool = True,
        overlay: bool = True,
        overlay_radius: int = 10,
        calibration_target_radius: int = 48,
        tracker: Optional[WebcamFingerTracker] = None,
        mapper: Optional[CursorMapper] = None,
        overlay_ui: Optional[ScreenDotOverlay] = None,
        calibration_overlay_ui: Optional[ScreenTargetOverlay] = None,
    ) -> None:
        self.tracker = tracker or WebcamFingerTracker(
            camera_index=camera_index,
            mirror=mirror,
            preview_enabled=preview,
            preview_window_enabled=preview_window,
        )
        self.mapper = mapper or CursorMapper(smoothing=smoothing, stale_timeout_s=stale_timeout_s)

        self.overlay = overlay_ui
        if self.overlay is None and overlay:
            self.overlay = ScreenDotOverlay(radius=overlay_radius, visible=True)
        self.calibration_overlay = calibration_overlay_ui or ScreenTargetOverlay(
            radius=calibration_target_radius, visible=False
        )

        self.tracker_start_timeout_s = float(max(0.5, tracker_start_timeout_s))
        self._running = False
        self._last_error: Optional[str] = None

    def start(self) -> bool:
        if self.overlay is not None:
            if not self.overlay.start():
                self._last_error = self.overlay.get_last_error() or "overlay failed to start"
                return False

        if self.calibration_overlay is not None:
            if not self.calibration_overlay.start():
                self._last_error = (
                    self.calibration_overlay.get_last_error() or "calibration overlay failed to start"
                )
                if self.overlay is not None:
                    self.overlay.stop()
                return False

        if not self.tracker.start(timeout_s=self.tracker_start_timeout_s):
            health = self.tracker.get_health()
            self._last_error = self.tracker.get_last_error() or (
                f"tracker failed to start (running={health.running}, frames_seen={health.frames_seen})"
            )
            if self.overlay is not None:
                self.overlay.stop()
            if self.calibration_overlay is not None:
                self.calibration_overlay.stop()
            self._running = False
            return False

        self._running = True
        self._last_error = None
        return True

    def stop(self) -> None:
        self.tracker.stop()
        if self.overlay is not None:
            self.overlay.stop()
        if self.calibration_overlay is not None:
            self.calibration_overlay.stop()
        self._running = False

    def get_cursor(self) -> Optional[CursorSample]:
        return self._map_sample_to_cursor(self.tracker.get_latest_sample())

    def _map_sample_to_cursor(self, sample) -> Optional[CursorSample]:
        if sample is not None:
            cur = self.mapper.update_from_normalized(
                sample.x, sample.y, ts=sample.ts, source="hand", confidence=sample.confidence
            )
        else:
            cur = self.mapper.get_fallback()

        if self.overlay is not None:
            if cur is not None:
                self.overlay.update_position(cur.x, cur.y)
            else:
                self.overlay.pump()
        return cur

    def pump_ui(self) -> None:
        self.tracker.pump_preview()
        if self.overlay is not None:
            self.overlay.pump()
        if self.calibration_overlay is not None:
            self.calibration_overlay.pump()

    def set_overlay_visible(self, visible: bool) -> Optional[bool]:
        if self.overlay is None:
            return None
        self.overlay.set_visible(bool(visible))
        return bool(visible)

    def clear_calibration(self) -> Tuple[bool, str]:
        self.mapper.clear_calibration()
        return True, "calibration cleared"

    def run_guided_calibration(
        self,
        *,
        announce: Optional[Callable[[str], None]] = None,
        dwell_s: float = 0.30,
        target_timeout_s: float = 20.0,
        poll_dt_s: float = 0.02,
    ) -> Tuple[bool, str]:
        """Interactive 4-point calibration: move the dot into each ring and hold."""
        say = announce or print
        targets = self.mapper.get_calibration_targets()
        dwell_s = max(0.2, float(dwell_s))
        target_timeout_s = max(3.0, float(target_timeout_s))
        poll_dt_s = max(0.01, float(poll_dt_s))

        camera_points: List[Tuple[float, float]] = []
        screen_points: List[Tuple[int, int]] = []
        missing_notice_deadline = 0.0

        say("[calibration] Starting 4-point calibration.")
        say("[calibration] Move the small yellow cursor into each large ring and hold briefly.")

        try:
            for idx, (name, sx, sy) in enumerate(targets, start=1):
                pretty_name = name.replace("_", "-")
                if self.calibration_overlay is not None:
                    self.calibration_overlay.set_visible(True)
                    self.calibration_overlay.update_position(sx, sy)

                say(f"[calibration] {idx}/4 point to {pretty_name}.")

                inside_since: Optional[float] = None
                stable_camera_samples: List[Tuple[float, float]] = []
                step_deadline = time.time() + target_timeout_s

                while time.time() < step_deadline:
                    self.pump_ui()
                    sample = self.tracker.get_latest_sample()
                    cursor = self._map_sample_to_cursor(sample)
                    now = time.time()

                    if sample is None or cursor is None:
                        inside_since = None
                        stable_camera_samples.clear()
                        if now >= missing_notice_deadline:
                            say("[calibration] hand not detected. Keep one hand visible.")
                            missing_notice_deadline = now + 1.5
                        time.sleep(poll_dt_s)
                        continue

                    distance = math.hypot(float(cursor.x - sx), float(cursor.y - sy))
                    target_radius = float(getattr(self.calibration_overlay, "radius", 48))
                    if distance <= target_radius:
                        if inside_since is None:
                            inside_since = now
                            stable_camera_samples = []
                        stable_camera_samples.append((float(sample.x), float(sample.y)))

                        if now - inside_since >= dwell_s and len(stable_camera_samples) >= 3:
                            x_med = statistics.median(p[0] for p in stable_camera_samples)
                            y_med = statistics.median(p[1] for p in stable_camera_samples)
                            camera_points.append((x_med, y_med))
                            screen_points.append((sx, sy))
                            say(f"[calibration] captured {pretty_name}.")
                            break
                    else:
                        inside_since = None
                        stable_camera_samples.clear()

                    time.sleep(poll_dt_s)
                else:
                    return False, (
                        f"calibration failed at {pretty_name}: move the cursor into the ring and hold."
                    )
        finally:
            if self.calibration_overlay is not None:
                self.calibration_overlay.set_visible(False)
                self.calibration_overlay.pump()

        ok, msg = self.mapper.calibrate_from_correspondences(camera_points, screen_points)
        if not ok:
            return False, msg

        say("[calibration] Completed. Calibration is active for this session.")
        return True, msg

    def status(self) -> Dict[str, Any]:
        health = self.tracker.get_health()
        return {
            "source": "hand",
            "running": self._running and health.running,
            "last_error": self._last_error or health.last_error,
            "calibrated": self.mapper.has_calibration(),
        }
