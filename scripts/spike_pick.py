"""Spike S3 (plan.md Step 3): hand-as-cursor calibration -> live picking.

Easy Wand-style calibration: your hand drives a yellow dot on screen; move it
into each ring at the screen corners and hold briefly. Then the dot acts as a
mouse over the Blender viewport, and we raycast-pick whatever it's over.

Prereq: Blender open with the Forge addon enabled and **Connect** clicked, the
3D viewport filling its area, on the monitor the webcam faces.

    python scripts/spike_pick.py                 # live pick readout
    python scripts/spike_pick.py --target Cube   # also score hits on an object

Point at an object and confirm it selects reliably (Step 3 gate: >=90%).
Press Ctrl-C to stop.
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
from client.cursor.provider import HandCursorProvider  # noqa: E402
from client.cursor.viewport import ViewportScreenRect  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", help="object name to score pick accuracy against")
    ap.add_argument("--camera", type=int, default=0)
    args = ap.parse_args()

    conn = BlenderConnection()
    try:
        geo = conn.send_command("get_window_geometry")
    except BlenderError as exc:
        print(f"Could not reach Blender: {exc}\nIs the addon enabled and 'Connect' clicked?")
        return 1
    rect = ViewportScreenRect.from_geometry(geo)
    print(
        f"Viewport region: {rect.region_w_px}x{rect.region_h_px}px @ pixel_size {rect.pixel_size}; "
        f"screen rect x[{rect.left_pt:.0f}..{rect.left_pt + rect.region_w_px / rect.pixel_size:.0f}]pt "
        f"y[{rect.top_pt:.0f}..{rect.bottom_pt:.0f}]pt"
    )

    provider = HandCursorProvider(camera_index=args.camera)
    if not provider.start():
        print(f"Hand cursor failed to start: {provider._last_error}")
        return 1

    ok, msg = provider.run_guided_calibration()
    if not ok:
        print(f"Calibration failed: {msg}")
        provider.stop()
        return 1

    print("\nLive picking — move your hand over the Blender viewport. Ctrl-C to stop.\n")
    last_name = object()
    hits = total = 0
    try:
        while True:
            provider.pump_ui()
            cursor = provider.get_cursor()
            if cursor is None:
                time.sleep(0.03)
                continue
            if not rect.contains_screen_point(cursor.x, cursor.y):
                if last_name is not None:
                    print(f"  cursor @ ({cursor.x},{cursor.y})pt -> outside viewport")
                    last_name = None
                time.sleep(0.03)
                continue

            rx, ry = rect.screen_to_region(cursor.x, cursor.y)
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
                    print(f"  accuracy on '{args.target}': {hits}/{total} ({100.0 * hits / total:.0f}%)")
            if name != last_name:
                print(f"  pick @ region ({rx:.0f},{ry:.0f}) -> {name or '(empty)'}")
                last_name = name
            time.sleep(0.05)
    except KeyboardInterrupt:
        if args.target and total:
            print(f"\nFinal accuracy on '{args.target}': {hits}/{total} ({100.0 * hits / total:.0f}%)")
    finally:
        provider.stop()
        conn.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
