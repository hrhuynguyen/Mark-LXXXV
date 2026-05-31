"""Interactive Text2Blender prompt test with hand gesture mouse control.

This bypasses the unfinished full Forge server/client stack. It is meant for
live testing the two working pieces together:

* type a prompt, then Text2Blender creates the object in Blender;
* keep the webcam hand mouse running so gestures can move/navigate Blender.
"""

from __future__ import annotations

import argparse
import subprocess
import signal
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.blender_bridge import BlenderConnection
from client.cursor.webcam_tracker import WebcamFingerTracker
from client.gestures.hand_mouse import (
    HandMouseController,
    HandMouseCursorProvider,
    PynputMouseBackend,
)
from server.agents.part_info_agent import GeminiLivePartInfoAgent
from server.build.text2blender_adapter import (
    Text2BlenderUnavailable,
    _run_text2blender_cli,
    resolve_text2blender_command,
    resolve_text2blender_path,
)
from server.tools.blender_selection import get_selected_object_snapshot


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
    parser.add_argument(
        "--no-auto-info",
        action="store_true",
        help="Do not ask Gemini Live when Blender's active selected object changes.",
    )
    parser.add_argument(
        "--part-info-model",
        default=None,
        help="Gemini Live model for selected-part explanations.",
    )
    parser.add_argument("--info-poll-seconds", type=float, default=1.0)
    parser.add_argument(
        "--speak-info",
        action="store_true",
        help="Also speak selected-part explanations through macOS say.",
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


def _run_prompt(prompt: str, busy_event: threading.Event | None = None) -> int:
    path = resolve_text2blender_path()
    command = resolve_text2blender_command(path)
    if busy_event:
        busy_event.set()
    try:
        reply = _run_text2blender_cli(path, command, prompt)
    except Text2BlenderUnavailable as exc:
        print(f"Text2Blender failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if busy_event:
            busy_event.clear()

    print("\nText2Blender reply:")
    print(reply)
    print()
    return 0


def _explain_selected_part(
    *,
    agent: GeminiLivePartInfoAgent,
    speak: bool,
    label: str = "Gemini Live part info",
) -> str | None:
    conn = BlenderConnection()
    try:
        snapshot = get_selected_object_snapshot(conn)
    finally:
        conn.disconnect()

    if not snapshot:
        print("No Blender object is selected.")
        return None

    name = snapshot.get("name", "selected object")
    print(f"\nSelected: {name}")
    print("Asking Gemini for part info...")
    reply = agent.explain_part(snapshot)
    if agent.live_available is False:
        label = "Gemini part info"
        print("Gemini Live text response unavailable here; using Gemini text fallback.")
    print(f"\n{label}:")
    print(reply)
    print()
    if speak:
        _speak(reply)
    return str(name)


def _start_part_info_monitor(
    args: argparse.Namespace,
    *,
    stop_event: threading.Event,
    busy_event: threading.Event,
) -> threading.Thread:
    agent = GeminiLivePartInfoAgent(model=args.part_info_model)
    poll_s = max(0.25, float(args.info_poll_seconds))

    def monitor() -> None:
        last_name: str | None = None
        last_error: str | None = None
        while not stop_event.is_set():
            if busy_event.is_set():
                time.sleep(poll_s)
                continue

            conn = BlenderConnection()
            try:
                snapshot = get_selected_object_snapshot(conn)
            except Exception as exc:
                message = str(exc)
                if message != last_error:
                    print(f"Part info monitor paused: {message}")
                    last_error = message
                time.sleep(poll_s)
                continue
            finally:
                conn.disconnect()

            last_error = None
            name = str(snapshot.get("name")) if snapshot else None
            if not name:
                last_name = None
                time.sleep(poll_s)
                continue
            if name == last_name:
                time.sleep(poll_s)
                continue

            last_name = name
            print(f"\nSelected: {name}")
            print("Asking Gemini for part info...")
            busy_event.set()
            try:
                reply = agent.explain_part(snapshot)
            except Exception as exc:
                print(f"Gemini Live part info failed: {exc}")
            else:
                if agent.live_available is False:
                    print("Gemini Live text response unavailable here; using Gemini text fallback.")
                    print("\nGemini part info:")
                else:
                    print("\nGemini Live part info:")
                print(reply)
                print()
                if args.speak_info:
                    _speak(reply)
            finally:
                busy_event.clear()
            time.sleep(poll_s)

    thread = threading.Thread(target=monitor, name="part-info-monitor", daemon=True)
    thread.start()
    return thread


def _speak(text: str) -> None:
    try:
        subprocess.Popen(["say", text])
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    args = build_arg_parser().parse_args(argv)
    stop_event = threading.Event()
    busy_event = threading.Event()
    gesture_thread: threading.Thread | None = None
    info_thread: threading.Thread | None = None

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

        info_agent: GeminiLivePartInfoAgent | None = None
        if not args.no_auto_info:
            try:
                info_thread = _start_part_info_monitor(
                    args,
                    stop_event=stop_event,
                    busy_event=busy_event,
                )
            except RuntimeError as exc:
                print(f"Part info monitor disabled: {exc}")
                print("Set GOOGLE_API_KEY or GEMINI_API_KEY to enable Gemini Live part info.")
                print()
            else:
                print("Part info monitor is running.")
                print("Select an object in Blender to ask Gemini Live what it is.")
                print("Use /info to explain the current selection manually.")
                print()
        else:
            info_agent = GeminiLivePartInfoAgent(model=args.part_info_model)

        if args.prompt:
            return _run_prompt(args.prompt, busy_event)

        print("Type a Blender object prompt. Empty line or /quit exits.")
        print("Commands: /info, /help, /quit")
        while not stop_event.is_set():
            try:
                prompt = input("prompt> ").strip()
            except EOFError:
                break
            if not prompt or prompt in {"/q", "/quit", "/exit"}:
                break
            if prompt == "/help":
                print("Type an object prompt to generate in Blender.")
                print("Select an object in Blender and type /info for Gemini Live part info.")
                print("Launch with --no-auto-info to disable automatic selection explanations.")
                continue
            if prompt == "/info":
                if info_agent is None:
                    info_agent = GeminiLivePartInfoAgent(model=args.part_info_model)
                busy_event.set()
                try:
                    _explain_selected_part(agent=info_agent, speak=args.speak_info)
                finally:
                    busy_event.clear()
                continue

            code = _run_prompt(prompt, busy_event)
            if code:
                return code
        return 0
    finally:
        stop_event.set()
        if gesture_thread and gesture_thread.is_alive():
            gesture_thread.join(timeout=1.5)
        if info_thread and info_thread.is_alive():
            info_thread.join(timeout=1.5)


if __name__ == "__main__":
    raise SystemExit(main())
