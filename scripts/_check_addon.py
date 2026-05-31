"""Headless sanity check for addon/forge_addon.py — runs INSIDE Blender:

    blender --background --factory-startup --python scripts/_check_addon.py

Verifies registration on this Blender version and the handlers that don't need a
3D viewport. pick_object_at / get_view_geometry can't run headless (no VIEW_3D),
so we assert they return a graceful error envelope rather than crash; their live
behaviour is verified in the GUI during the Step 3 calibration spike.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_DIR = os.path.join(os.path.dirname(HERE), "addon")
sys.path.insert(0, ADDON_DIR)

import bpy  # noqa: E402

import forge_addon  # noqa: E402

failures = []


def check(label, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


# 1) register on this Blender version
forge_addon.register()
check("register() succeeds", True)
check("FORGE_PT_Panel registered", hasattr(bpy.types, "FORGE_PT_Panel"))
check("scene.forge_port property", hasattr(bpy.context.scene, "forge_port"))
check("scene.forge_server_running property", hasattr(bpy.context.scene, "forge_server_running"))

srv = forge_addon.ForgeServer()

# 2) handlers that work headless
r = srv.execute_command({"type": "get_scene_info"})
check("get_scene_info -> success", r.get("status") == "success")
check("get_scene_info has objects", "objects" in r.get("result", {}))

r = srv.execute_command({"type": "get_object_info", "params": {"name": "Cube"}})
check("get_object_info(Cube) -> success", r.get("status") == "success")
check("get_object_info has world_bounding_box", "world_bounding_box" in r.get("result", {}))

r = srv.execute_command(
    {"type": "execute_code", "params": {"code": "import bpy; print(len(bpy.data.objects))"}}
)
check("execute_code -> executed", r.get("result", {}).get("executed") is True)

# 3) viewport raycast — the factory scene has a VIEW_3D region even headless, and
#    the default view looks down at the Cube sitting on the origin.
gv = srv.execute_command({"type": "get_view_geometry"})
check("get_view_geometry -> success", gv.get("status") == "success")
region = gv.get("result", {}).get("region", {})
cx, cy = region.get("width", 0) // 2, region.get("height", 0) // 2

hit = srv.execute_command({"type": "pick_object_at", "params": {"region_x": cx, "region_y": cy}})
hit_res = hit.get("result", {})
check("pick at center hits the Cube", hit_res.get("name") == "Cube")
check("pick result carries world_bounding_box", "world_bounding_box" in hit_res)

miss = srv.execute_command({"type": "pick_object_at", "params": {"region_x": 2, "region_y": 2}})
check("pick at empty corner -> hit:false", miss.get("result", {}).get("hit") is False)

r = srv.execute_command({"type": "no_such_command"})
check("unknown command -> error envelope", r.get("status") == "error")

forge_addon.unregister()
check("unregister() succeeds", True)

print("\nFORGE_ADDON_CHECK", "PASS" if not failures else f"FAIL ({failures})")
