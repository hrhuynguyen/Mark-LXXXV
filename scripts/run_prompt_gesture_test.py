"""Interactive Text2Blender prompt test with hand gesture mouse control.

This bypasses the unfinished full Forge server/client stack. It is meant for
live testing the two working pieces together:

* type a prompt, then Text2Blender creates the object in Blender;
* keep the webcam hand mouse running so gestures can move/navigate Blender.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
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
from server.build.text2blender_adapter import (
    Text2BlenderUnavailable,
    _run_text2blender_cli,
    resolve_text2blender_command,
    resolve_text2blender_path,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Type Text2Blender prompts while hand gestures control the mouse."
    )
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--no-gestures", action="store_true")
    parser.add_argument("--gesture-debug", action="store_true")
    parser.add_argument("--gesture-touch-threshold", type=float, default=0.19)
    parser.add_argument("--gesture-hold-frames", type=int, default=2)
    parser.add_argument("--gesture-smoothing", type=float, default=0.35)
    parser.add_argument("--gesture-inner-area", type=float, default=0.70)
    parser.add_argument(
        "--prompt",
        help="Run one prompt and exit. Without this flag, prompts are read interactively.",
    )
    return parser


def _start_gesture_provider(args: argparse.Namespace, stop_event: threading.Event):
    tracker = WebcamFingerTracker(camera_index=args.camera, num_hands=1)
    controller = HandMouseController(
        backend=PynputMouseBackend(),
        touch_threshold=args.gesture_touch_threshold,
        mode_hold_frames=args.gesture_hold_frames,
        smoothing=args.gesture_smoothing,
        inner_area_percent=args.gesture_inner_area,
    )
    provider = HandMouseCursorProvider(
        tracker=tracker,
        controller=controller,
        debug=args.gesture_debug,
    )

    if not provider.start():
        status = provider.status()
        raise RuntimeError(f"gesture provider failed: {status.get('last_error')}")

    def pump() -> None:
        try:
            while not stop_event.is_set():
                provider.get_cursor()
                provider.pump_ui()
                time.sleep(1 / 60)
        finally:
            provider.stop()

    thread = threading.Thread(target=pump, name="gesture-mouse", daemon=True)
    thread.start()
    return provider, thread


def _run_prompt(prompt: str) -> int:
    path = resolve_text2blender_path()
    command = resolve_text2blender_command(path)
    try:
        reply = _run_text2blender_cli(path, command, prompt)
    except Text2BlenderUnavailable as exc:
        print(f"Text2Blender failed: {exc}", file=sys.stderr)
        return 1

    print("\nText2Blender reply:")
    print(reply)
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    stop_event = threading.Event()
    gesture_thread: threading.Thread | None = None

    def stop(_signum=None, _frame=None) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        if not args.no_gestures:
            _provider, gesture_thread = _start_gesture_provider(args, stop_event)
            print("Gesture mouse is running.")
            print("Point/move: hand movement")
            print("Click/drag: thumb + index")
            print("Zoom: thumb + middle, move vertically")
            print("Orbit: thumb + ring, drag")
            print("Pan: peace sign, drag")
            print("Release: thumb + pinky or fist")
            print()

        if args.prompt:
            return _run_prompt(args.prompt)

        print("Type a Blender object prompt. Empty line or /quit exits.")
        while not stop_event.is_set():
            try:
                prompt = input("prompt> ").strip()
            except EOFError:
                break
            if not prompt or prompt in {"/q", "/quit", "/exit"}:
                break
            code = _run_prompt(prompt)
            if code:
                return code
        return 0
    finally:
        stop_event.set()
        if gesture_thread and gesture_thread.is_alive():
            gesture_thread.join(timeout=1.5)


if __name__ == "__main__":
    raise SystemExit(main())
