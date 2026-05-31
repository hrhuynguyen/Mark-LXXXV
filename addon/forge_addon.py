"""
Forge Blender addon — socket server + command handlers.

Starts from BlenderMCP's `addon.py` (socket server on localhost:9876, main-thread
dispatch via `bpy.app.timers.register`, and the base handlers: execute_code,
get_scene_info, get_object_info, _get_aabb, get_viewport_screenshot, plus the
Hyper3D / Hunyuan3D / Sketchfab / Poly Haven integrations).

Forge adds these handlers (see plan.md):
  - get_view_geometry()              VIEW_3D region rect + size            (Step 2)
  - pick_object_at(region_x, y)      camera raycast -> object name + AABB  (Step 2)
  - set_part_transform(name, delta)  registry-driven transform edit        (Step 9)
  - orbit_view / zoom_view / pan_view  RegionView3D camera nav             (Step 11)
  - save_build / load_build          .blend + registry JSON persistence    (Step 12)

Install: Blender > Edit > Preferences > Add-ons > Install... > select this file.
This is a single-file Blender addon (NOT part of the Python package layout).
"""

bl_info = {
    "name": "Forge MCP",
    "author": "Forge",
    "version": (0, 1, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Forge",
    "description": "Voice + gesture controlled procedural modeling bridge",
    "category": "Interface",
}

# TODO Step 1: copy BlenderMCP addon.py here, rename panel/ids to Forge.
# TODO Step 2: add _get_view3d(), get_view_geometry(), pick_object_at().
# TODO Steps 9/11/12: add set_part_transform, orbit/zoom/pan_view, save/load_build.


def register():
    pass


def unregister():
    pass


if __name__ == "__main__":
    register()
