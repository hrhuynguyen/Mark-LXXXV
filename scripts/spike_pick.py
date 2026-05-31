"""Spike S3 (plan.md Step 3): hand-cursor -> live viewport picking.

The hand drives a visible yellow dot via an absolute linear inner-area map
(handTrack-style): no homography, no ring calibration. The palm (landmark 9) is
tracked for steadiness. Only ONE quick calibration remains:

  Viewport anchors: park the dot on the Blender viewport's top-left then
  bottom-right corners -> measures the viewport rectangle empirically
  (no macOS window-geometry guessing, so no pointing offset).

Then the dot acts as a mouse over the viewport and we raycast-pick under it,
with a small search radius so minor hand jitter still selects the object.

Prereq: Blender open with the Forge addon enabled and **Connect** clicked, the
3D viewport filling its area, on the monitor the webcam faces.

    python scripts/spike_pick.py --target Cube   # score hits on an object
    python scripts/spike_pick.py --debug Cube    # print region pos + delta to center

Point at an object and confirm it selects reliably (Step 3 gate: >=90%). Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time
from collections import deque

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from client.blender_bridge import BlenderConnection, BlenderError  # noqa: E402
from client.cursor.provider import HandCursorProvider  # noqa: E402


# Rosette raycast: try the pixel under the dot first, then expanding rings, and
# return the nearest object hit. This "fattens the finger" so small hand jitter
# still selects the object you're pointing at. Prototyped via execute_code (no
# addon reload); promote into the addon's pick_object_at once the radius is set.
_PICK_CODE = """
import bpy, math
from bpy_extras import view3d_utils
rx, ry, radius = {rx}, {ry}, {radius}
rg = rv = None
for a in bpy.context.window.screen.areas:
    if a.type == 'VIEW_3D':
        rg = next(r for r in a.regions if r.type == 'WINDOW')
        rv = a.spaces.active.region_3d
        break
offs = [(0.0, 0.0)]
for ring in (radius * 0.5, radius):
    for k in range(8):
        ang = k * math.pi / 4.0
        offs.append((ring * math.cos(ang), ring * math.sin(ang)))
dg = bpy.context.evaluated_depsgraph_get()
name = ''
for dx, dy in offs:
    co = (rx + dx, ry + dy)
    origin = view3d_utils.region_2d_to_origin_3d(rg, rv, co)
    direction = view3d_utils.region_2d_to_vector_3d(rg, rv, co)
    hit, loc, nrm, idx, obj, mat = bpy.context.scene.ray_cast(dg, origin, direction)
    if hit and obj is not None:
        name = obj.name
        break
print(name)
"""


def _pick_radius(conn: BlenderConnection, rx: float, ry: float, radius: float):
    code = _PICK_CODE.format(rx=round(rx, 1), ry=round(ry, 1), radius=round(radius, 1))
    return conn.send_command("execute_code", {"code": code})["result"].strip() or None


def _project_center(conn: BlenderConnection, name: str):
    code = (
        "import bpy\n"
        "from bpy_extras import view3d_utils\n"
        "for a in bpy.context.window.screen.areas:\n"
        "    if a.type=='VIEW_3D':\n"
        "        rg=next(r for r in a.regions if r.type=='WINDOW'); rv=a.spaces.active.region_3d; break\n"
        f"o=bpy.data.objects.get({name!r})\n"
        "co=view3d_utils.location_3d_to_region_2d(rg, rv, o.matrix_world.translation)\n"
        "print(round(co.x,1), round(co.y,1))\n"
    )
    out = conn.send_command("execute_code", {"code": code})["result"].split()
    return (float(out[0]), float(out[1]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", help="object name to score pick accuracy against")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--radius", type=float, default=80.0, help="pick search radius in region px")
    ap.add_argument("--smoothing", type=float, default=0.25, help="cursor smoothing alpha (lower=steadier)")
    ap.add_argument("--landmark", type=int, default=8, help="8=index fingertip (wide range), 9=palm (steady)")
    ap.add_argument("--debug", metavar="OBJECT", help="print region pos + delta to OBJECT center")
    args = ap.parse_args()

    conn = BlenderConnection()
    try:
        region = conn.send_command("get_view_geometry")["region"]
    except BlenderError as exc:
        print(f"Could not reach Blender: {exc}\nIs the addon enabled and 'Connect' clicked?")
        return 1
    width, height = region["width"], region["height"]
    print(f"Blender VIEW_3D region: {width}x{height}")

    provider = HandCursorProvider(
        camera_index=args.camera, smoothing=args.smoothing, landmark_id=args.landmark
    )
    if not provider.start():
        print(f"Hand cursor failed to start: {provider.status()['last_error']}")
        return 1

    try:
        # The dot tracks the hand immediately (absolute linear map — no ring
        # calibration). Just anchor the viewport rectangle empirically.
        print("\nThe yellow dot now follows your hand. We'll anchor the viewport corners.\n")
        ok, msg = provider.calibrate_viewport_anchors(width, height)
        if not ok:
            print(f"Viewport anchoring failed: {msg}")
            return 1

        sg = provider.mapper.screen_geometry
        s2r = provider.screen_to_region
        spread_x, spread_y = abs(s2r.bx - s2r.ax), abs(s2r.by - s2r.ay)
        print(f"[geom] screen={sg.width}x{sg.height}  region={width}x{height}  landmark={args.landmark}")
        print(
            f"[geom] anchors: TL screen=({s2r.ax:.0f},{s2r.ay:.0f})  "
            f"BR screen=({s2r.bx:.0f},{s2r.by:.0f})  spread=({spread_x:.0f},{spread_y:.0f})px"
        )
        if spread_x < 500 or spread_y < 300:
            print(
                "[WARN] Tiny calibration spread — your hand barely moved between the two\n"
                "       viewport corners, so picking will be hyper-sensitive and miss. RE-RUN\n"
                "       and move the yellow dot between the Blender viewport's TOP-LEFT and\n"
                "       BOTTOM-RIGHT corners. Do not point at the object until anchoring is done."
            )

        target_center = _project_center(conn, args.debug) if args.debug else None
        if target_center:
            print(f"[debug] {args.debug} center projects to region {target_center}")

        print(f"\nLive picking (radius={args.radius:.0f}px) — point at the viewport. Ctrl-C to stop.")
        print("Live line shows: palm(norm) -> dot(screen) -> region -> hit. Move your hand to confirm it tracks.\n")
        last_status = 0.0
        recent: deque = deque(maxlen=20)  # rolling window: True if hit target
        total = best = 0
        while True:
            provider.pump_ui()
            dbg = provider.region_point_debug()  # drives the dot + returns coords
            if dbg is None:
                time.sleep(0.03)
                continue
            rx, ry = dbg["region"]

            try:
                name = _pick_radius(conn, rx, ry, args.radius)
            except BlenderError as exc:
                print(f"pick error: {exc}")
                time.sleep(0.1)
                continue

            if args.target is not None:
                total += 1
                recent.append(name == args.target)

            # live readout (~3/sec): raw palm -> screen dot -> region -> hit
            if time.time() - last_status > 0.33:
                nx, ny = dbg["norm"] if dbg["norm"] else (float("nan"), float("nan"))
                sx, sy = dbg["screen"]
                tag = " STALE(no-hand)" if dbg["stale"] else ""
                extra = ""
                if target_center:
                    extra = f"  delta=({rx - target_center[0]:+.0f},{ry - target_center[1]:+.0f})"
                acc = ""
                if recent:
                    pct = 100.0 * sum(recent) / len(recent)
                    best = max(best, int(pct))
                    acc = f"  acc={sum(recent)}/{len(recent)}({pct:.0f}%)"
                print(
                    f"palm=({nx:.2f},{ny:.2f}) -> dot=({sx},{sy}) -> region=({rx:.0f},{ry:.0f})"
                    f" -> {name or '(empty)'}{extra}{acc}{tag}"
                )
                last_status = time.time()
            time.sleep(0.05)
    except KeyboardInterrupt:
        if args.target and recent:
            pct = 100.0 * sum(recent) / len(recent)
            print(f"\nLast-20 accuracy on '{args.target}': {sum(recent)}/{len(recent)} ({pct:.0f}%)")
            print(f"Best rolling window this session: {best}%")
    finally:
        provider.stop()
        conn.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
