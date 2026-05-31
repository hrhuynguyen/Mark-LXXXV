"""client.companion_app

Overlay-only macOS app window (browser view removed). Launches Blender, draws cursor overlay, hosts mic/agent. Step 4.
"""

<<<<<<< Updated upstream
# Scaffold stub — implement per plan.md. Not yet wired up.
=======
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import signal
import sys

from dotenv import load_dotenv

from .blender_bridge import DEFAULT_HOST, DEFAULT_PORT
from .blender_launcher import launch_blender
from .companion_runtime import (
    CompanionRuntime,
    generate_session_id,
    get_stable_user_id,
    with_identity_path,
)
from .local_executor import LocalToolExecutor

ROOT = pathlib.Path(__file__).resolve().parent.parent


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Forge overlay-only client.")
    parser.add_argument(
        "--ws-url",
        default=os.getenv("FORGE_WS_URL", "ws://127.0.0.1:8000/ws"),
        help="Forge server websocket base URL.",
    )
    parser.add_argument("--blender-path", default=os.getenv("BLENDER_APP_PATH"))
    parser.add_argument("--blender-host", default=os.getenv("BLENDER_HOST", DEFAULT_HOST))
    parser.add_argument("--blender-port", type=int, default=int(os.getenv("BLENDER_PORT", DEFAULT_PORT)))
    parser.add_argument("--no-blender-launch", action="store_true", help="Attach only.")
    parser.add_argument(
        "--cursor",
        choices=("hand", "mouse"),
        default="hand",
        help="Cursor source. Mouse is useful for socket/tool smoke tests.",
    )
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--calibrate", action="store_true", help="Run guided hand calibration first.")
    parser.add_argument("--no-audio", action="store_true", help="Disable mic upload and speaker playback.")
    parser.add_argument(
        "--gestures",
        action="store_true",
        help="Enable handTrack-style hand control of the real mouse for Blender navigation.",
    )
    parser.add_argument(
        "--gesture-debug",
        action="store_true",
        help="Print recognized gesture mode transitions while running.",
    )
    parser.add_argument("--gesture-touch-threshold", type=float, default=None)
    parser.add_argument("--gesture-hold-frames", type=int, default=None)
    parser.add_argument("--gesture-smoothing", type=float, default=None)
    parser.add_argument("--gesture-inner-area", type=float, default=None)
    parser.add_argument("--no-reconnect", action="store_true", help="Exit after the first websocket session.")
    parser.add_argument("--debug", action="store_true", help="Verbose client logging.")
    return parser


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.no_blender_launch:
        result = launch_blender(
            blender_path=args.blender_path,
            host=args.blender_host,
            port=args.blender_port,
            wait=True,
        )
        if not result.ok:
            print(f"Blender launch failed: {result.error}", file=sys.stderr)
            return 1
        verb = "attached to" if result.already_running else "launched"
        print(f"Forge {verb} Blender socket at {result.host}:{result.port}")

    if args.cursor == "mouse":
        from .cursor.provider import MouseCursorProvider

        cursor_provider = MouseCursorProvider()
    else:
        from .cursor.webcam_tracker import WebcamFingerTracker

        tracker = WebcamFingerTracker(
            camera_index=args.camera,
            num_hands=1,
        )
        if args.gestures:
            from .gestures.hand_mouse import (
                HandMouseController,
                HandMouseCursorProvider,
                PynputMouseBackend,
            )

            gesture_kwargs = {}
            if args.gesture_touch_threshold is not None:
                gesture_kwargs["touch_threshold"] = args.gesture_touch_threshold
            if args.gesture_hold_frames is not None:
                gesture_kwargs["mode_hold_frames"] = args.gesture_hold_frames
            if args.gesture_smoothing is not None:
                gesture_kwargs["smoothing"] = args.gesture_smoothing
            if args.gesture_inner_area is not None:
                gesture_kwargs["inner_area_percent"] = args.gesture_inner_area
            controller = HandMouseController(
                backend=PynputMouseBackend(),
                **gesture_kwargs,
            )
            cursor_provider = HandMouseCursorProvider(
                tracker=tracker,
                controller=controller,
                debug=args.gesture_debug or args.debug,
            )
        else:
            from .cursor.provider import HandCursorProvider

            cursor_provider = HandCursorProvider(camera_index=args.camera, tracker=tracker)

    if not cursor_provider.start():
        status = cursor_provider.status()
        print(f"Cursor provider failed: {status.get('last_error')}", file=sys.stderr)
        return 1

    try:
        if args.calibrate:
            if args.gestures:
                print("Hand mouse mode uses handTrack-style inner-area mapping; calibration skipped.")
            elif not hasattr(cursor_provider, "run_guided_calibration"):
                print("Calibration is only available for the hand cursor.", file=sys.stderr)
                return 1
            else:
                ok, msg = cursor_provider.run_guided_calibration()  # type: ignore[attr-defined]
                if not ok:
                    print(f"Calibration failed: {msg}", file=sys.stderr)
                    return 1

        user_id = get_stable_user_id()
        session_id = generate_session_id()
        ws_url = with_identity_path(args.ws_url, user_id, session_id)

        executor = LocalToolExecutor(cursor_provider=cursor_provider)
        runtime = CompanionRuntime(
            ws_url=ws_url,
            cursor_provider=cursor_provider,
            executor=executor,
            enable_audio=not args.no_audio,
            reconnect=not args.no_reconnect,
        )

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def request_stop() -> None:
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, request_stop)
            except NotImplementedError:
                pass

        runtime_task = asyncio.create_task(runtime.run_forever())
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            {runtime_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done:
            await runtime.stop()
            runtime_task.cancel()
        for task in pending:
            task.cancel()
        if runtime_task in done:
            exc = runtime_task.exception()
            if exc:
                raise exc
    finally:
        cursor_provider.stop()

    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
>>>>>>> Stashed changes
