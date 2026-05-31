"""Run the hand gesture mouse controller without the full companion app."""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.cursor.webcam_tracker import WebcamFingerTracker
from client.gestures.hand_mouse import (
    HandMouseController,
    HandMouseCursorProvider,
    PynputMouseBackend,
)


def main() -> int:
    tracker = WebcamFingerTracker(camera_index=0, num_hands=1)
    controller = HandMouseController(
        backend=PynputMouseBackend(),
        touch_threshold=0.19,
        mode_hold_frames=2,
    )
    provider = HandMouseCursorProvider(
        tracker=tracker,
        controller=controller,
        debug=True,
    )

    running = True

    def stop(_signum, _frame) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if not provider.start():
        status = provider.status()
        print(f"Gesture provider failed: {status.get('last_error')}")
        return 1

    print("Gesture mouse running. Press Ctrl+C to stop.")
    print("Peace sign = pan; open palm = point only.")
    try:
        while running:
            provider.get_cursor()
            provider.pump_ui()
            time.sleep(1 / 60)
    finally:
        provider.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
