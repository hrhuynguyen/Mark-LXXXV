"""Spike S3 (plan.md Step 3): fingertip -> region calibration -> live picking.

Prereq: Blender open with the Forge addon enabled and **Connect** clicked, the
3D viewport filling its area, and a webcam + good lighting. Use the *one* monitor
the camera faces.

    python scripts/spike_pick.py                                  # live pick readout
    python scripts/spike_pick.py --target Cube                    # score until Ctrl-C
    python scripts/spike_pick.py --target Cube --samples 100      # pass/fail gate

Flow: reads the live region size from Blender, runs the 4-corner calibration,
then continuously raycasts under your fingertip and prints what it hits. Point at
an object and confirm it selects reliably (the Step 3 gate is >=90%).
Press Ctrl-C to stop, or use --samples for a bounded pass/fail run.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from client.blender_bridge import BlenderConnection, BlenderError  # noqa: E402
from client.cursor.mapper import RegionMapper  # noqa: E402
from client.cursor.provider import RegionCalibrator  # noqa: E402
from client.cursor.webcam_tracker import WebcamFingerTracker  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", help="object name to score pick accuracy against")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument(
        "--samples",
        type=int,
        default=0,
        help="with --target, stop after N scored picks and return pass/fail",
    )
    ap.add_argument(
        "--min-accuracy",
        type=float,
        default=90.0,
        help="required percentage for a bounded --target/--samples run",
    )
    args = ap.parse_args()
    if args.samples and not args.target:
        ap.error("--samples requires --target")

    conn = BlenderConnection()
    try:
        geo = conn.send_command("get_view_geometry")
    except BlenderError as exc:
        print(f"Could not reach Blender: {exc}\nIs the addon enabled and 'Connect' clicked?")
        return 1
    region = geo["region"]
    width, height = region["width"], region["height"]
    print(f"Blender VIEW_3D region: {width}x{height}")

    tracker = WebcamFingerTracker(camera_index=args.camera, preview_window_enabled=True)
    if not tracker.start(timeout_s=8.0):
        print(f"Camera/tracker failed to start: {tracker.get_last_error()}")
        return 1

    mapper = RegionMapper(width, height)
    calibrator = RegionCalibrator(tracker, mapper)

    ok, msg = calibrator.run_calibration()
    if not ok:
        print(f"Calibration failed: {msg}")
        tracker.stop()
        return 1

    print("\nLive picking — point at objects in the Blender viewport. Ctrl-C to stop.\n")
    last_name = object()
    hits = total = 0
    try:
        while True:
            tracker.pump_preview()
            point = calibrator.current_region_point()
            if point is None:
                time.sleep(0.03)
                continue
            rx, ry = point
            try:
                res = conn.send_command(
                    "pick_object_at", {"region_x": round(rx, 1), "region_y": round(ry, 1)}
                )
            except BlenderError as exc:
                print(f"pick error: {exc}")
                time.sleep(0.1)
                continue

            name = res.get("name") if res.get("hit") else None
            if args.target is not None:
                total += 1
                if name == args.target:
                    hits += 1
                if total % 20 == 0:
                    pct = 100.0 * hits / total
                    print(f"  accuracy on '{args.target}': {hits}/{total} ({pct:.0f}%)")
                if args.samples and total >= args.samples:
                    pct = 100.0 * hits / total
                    passed = pct >= args.min_accuracy
                    print(
                        f"\nFinal accuracy on '{args.target}': "
                        f"{hits}/{total} ({pct:.0f}%) "
                        f"against required {args.min_accuracy:.0f}%"
                    )
                    return 0 if passed else 2
            if name != last_name:
                print(f"  pick @ region ({rx:.0f},{ry:.0f}) -> {name or '(empty)'}")
                last_name = name
            time.sleep(0.05)
    except KeyboardInterrupt:
        if args.target and total:
            print(f"\nFinal accuracy on '{args.target}': {hits}/{total} ({100.0*hits/total:.0f}%)")
    finally:
        tracker.stop()
        conn.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
