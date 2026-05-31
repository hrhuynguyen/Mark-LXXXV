"""Forge Blender addon — socket server + command handlers (Seam 2, CONTRACTS.md §3).

Lifts BlenderMCP's socket server (localhost:9876, main-thread dispatch via
``bpy.app.timers.register``) and base handlers, strips all the generation /
telemetry integrations, and adds the Forge-specific handlers.

Handlers implemented here (Step 2):
  - execute_code(code)               arbitrary bpy escape hatch
  - get_scene_info()                 lightweight scene summary
  - get_object_info(name)            object detail + world AABB
  - get_view_geometry()              VIEW_3D region rect + view state
  - pick_object_at(region_x, y)      camera raycast -> object name + world AABB

Later steps extend the ``handlers`` dict:
  - set_part_transform (Step 9), orbit/zoom/pan_view (Step 11),
    save_build/load_build (Step 12).

Install: Blender > Edit > Preferences > Add-ons > Install... > select this file,
enable it, open the "Forge" tab in the 3D View N-sidebar, click "Connect".
Single-file addon — NOT part of the Python package layout.

Tested on Blender 5.1.2 (bundled Python 3.13). ``bl_info["blender"]`` is the
*minimum* supported version; the APIs used (``scene.ray_cast(depsgraph, ...)``,
``view3d_utils.region_2d_to_*``) are stable from 3.0 through 5.x.
"""

import io
import json
import socket
import threading
import time
import traceback
from contextlib import redirect_stdout

import bpy
import mathutils
from bpy.props import BoolProperty, IntProperty
from bpy_extras import view3d_utils

bl_info = {
    "name": "Forge MCP",
    "author": "Forge",
    "version": (0, 1, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Forge",
    "description": "Voice + gesture controlled procedural modeling bridge",
    "category": "Interface",
}

DEFAULT_PORT = 9876


# ─────────────────────────────────────────────────────────────────────────────
# Socket server
# ─────────────────────────────────────────────────────────────────────────────
class ForgeServer:
    """JSON-over-TCP server. Accepts one client; runs handlers on Blender's main
    thread via ``bpy.app.timers`` (bpy is not thread-safe)."""

    def __init__(self, host="localhost", port=DEFAULT_PORT):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None

    def start(self):
        if self.running:
            print("[Forge] server already running")
            return
        self.running = True
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)
            self.server_thread = threading.Thread(target=self._server_loop, daemon=True)
            self.server_thread.start()
            print(f"[Forge] server started on {self.host}:{self.port}")
        except Exception as exc:  # noqa: BLE001
            print(f"[Forge] failed to start server: {exc}")
            self.stop()

    def stop(self):
        self.running = False
        if self.socket:
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None
        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except RuntimeError:
                pass
            self.server_thread = None
        print("[Forge] server stopped")

    def _server_loop(self):
        self.socket.settimeout(1.0)  # so we can notice self.running going False
        while self.running:
            try:
                try:
                    client, address = self.socket.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                print(f"[Forge] client connected: {address}")
                threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()
            except Exception as exc:  # noqa: BLE001
                print(f"[Forge] server loop error: {exc}")
                if not self.running:
                    break
                time.sleep(0.5)

    def _handle_client(self, client):
        """Read commands (no length framing: parse when the buffer is valid JSON)
        and marshal each onto the main thread for execution."""
        client.settimeout(None)
        buffer = b""
        try:
            while self.running:
                data = client.recv(8192)
                if not data:
                    break
                buffer += data
                try:
                    command = json.loads(buffer.decode("utf-8"))
                except json.JSONDecodeError:
                    continue  # incomplete — keep reading
                buffer = b""

                def execute_wrapper(cmd=command):
                    try:
                        response = self.execute_command(cmd)
                        client.sendall(json.dumps(response).encode("utf-8"))
                    except Exception as exc:  # noqa: BLE001
                        traceback.print_exc()
                        try:
                            client.sendall(
                                json.dumps({"status": "error", "message": str(exc)}).encode("utf-8")
                            )
                        except OSError:
                            pass
                    return None  # unregister this one-shot timer

                bpy.app.timers.register(execute_wrapper, first_interval=0.0)
        except Exception as exc:  # noqa: BLE001
            print(f"[Forge] client handler error: {exc}")
        finally:
            try:
                client.close()
            except OSError:
                pass

    # ── dispatch ─────────────────────────────────────────────────────────
    def execute_command(self, command):
        try:
            return self._execute_command_internal(command)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return {"status": "error", "message": str(exc)}

    def _execute_command_internal(self, command):
        cmd_type = command.get("type")
        params = command.get("params", {})
        handlers = {
            "execute_code": self.execute_code,
            "get_scene_info": self.get_scene_info,
            "get_object_info": self.get_object_info,
            "get_view_geometry": self.get_view_geometry,
            "pick_object_at": self.pick_object_at,
        }
        handler = handlers.get(cmd_type)
        if not handler:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}
        try:
            result = handler(**params)
            return {"status": "success", "result": result}
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return {"status": "error", "message": str(exc)}

    # ── handlers ─────────────────────────────────────────────────────────
    def execute_code(self, code):
        """Run arbitrary Blender Python; return captured stdout."""
        namespace = {"bpy": bpy}
        buf = io.StringIO()
        with redirect_stdout(buf):
            exec(code, namespace)  # noqa: S102 — intentional escape hatch
        return {"executed": True, "result": buf.getvalue()}

    def get_scene_info(self):
        scene = bpy.context.scene
        info = {
            "name": scene.name,
            "object_count": len(scene.objects),
            "objects": [
                {
                    "name": obj.name,
                    "type": obj.type,
                    "location": [round(float(c), 4) for c in obj.location],
                }
                for obj in list(scene.objects)[:50]
            ],
        }
        return info

    @staticmethod
    def _get_aabb(obj):
        """World-space axis-aligned bounding box as [min_xyz, max_xyz]."""
        if obj.type != "MESH":
            raise TypeError("Object must be a mesh")
        world_corners = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
        min_corner = mathutils.Vector(map(min, zip(*world_corners)))
        max_corner = mathutils.Vector(map(max, zip(*world_corners)))
        return [[*min_corner], [*max_corner]]

    def get_object_info(self, name):
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Object not found: {name}")
        info = {
            "name": obj.name,
            "type": obj.type,
            "location": [*obj.location],
            "rotation": [*obj.rotation_euler],
            "scale": [*obj.scale],
            "visible": obj.visible_get(),
        }
        if obj.type == "MESH":
            info["world_bounding_box"] = self._get_aabb(obj)
        return info

    # ── Forge: viewport <-> world ────────────────────────────────────────
    @staticmethod
    def _get_view3d():
        """Return (area, window-region, RegionView3D) for the first VIEW_3D area.

        Raises if there is no 3D viewport — e.g. in ``--background`` mode, where
        pick/orbit are meaningless anyway.
        """
        screen = getattr(bpy.context, "screen", None)
        if screen is None:
            raise RuntimeError("No active screen (headless?) — no VIEW_3D available")
        for area in screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                if region is None:
                    continue
                return area, region, area.spaces.active.region_3d
        raise RuntimeError("No VIEW_3D area found")

    def get_view_geometry(self):
        """Region rectangle + view state — the client uses width/height to drive
        fingertip→region calibration (Step 3). Region pixels use a bottom-left
        origin (CONTRACTS.md §6)."""
        _area, region, rv3d = self._get_view3d()
        return {
            "region": {
                "x": region.x,
                "y": region.y,
                "width": region.width,
                "height": region.height,
            },
            "is_perspective": rv3d.is_perspective,
            "view_distance": float(rv3d.view_distance),
        }

    def pick_object_at(self, region_x, region_y):
        """Raycast from a region pixel into the scene; return the hit object.

        ``region_x/region_y`` are pixels within the VIEW_3D WINDOW region with a
        bottom-left origin. The client injects the calibrated cursor here
        (CONTRACTS.md §4.1) — the server never receives screen coordinates.
        """
        _area, region, rv3d = self._get_view3d()
        coord = (float(region_x), float(region_y))
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        hit, location, normal, _index, obj, _matrix = bpy.context.scene.ray_cast(
            depsgraph, origin, direction
        )
        if not hit or obj is None:
            return {"hit": False}
        result = {
            "hit": True,
            "name": obj.name,
            "type": obj.type,
            "hit_location": [*location],
            "hit_normal": [*normal],
        }
        if obj.type == "MESH":
            result["world_bounding_box"] = self._get_aabb(obj)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# UI: panel + operators
# ─────────────────────────────────────────────────────────────────────────────
class FORGE_PT_Panel(bpy.types.Panel):
    bl_label = "Forge"
    bl_idname = "FORGE_PT_Panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Forge"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.prop(scene, "forge_port")
        if not scene.forge_server_running:
            layout.operator("forge.start_server", text="Connect", icon="PLAY")
        else:
            layout.operator("forge.stop_server", text="Disconnect", icon="PAUSE")
            layout.label(text=f"Listening on :{scene.forge_port}", icon="LINKED")


class FORGE_OT_StartServer(bpy.types.Operator):
    bl_idname = "forge.start_server"
    bl_label = "Connect"
    bl_description = "Start the Forge socket server"

    def execute(self, context):
        scene = context.scene
        if not getattr(bpy.types, "forge_server", None):
            bpy.types.forge_server = ForgeServer(port=scene.forge_port)
        bpy.types.forge_server.start()
        scene.forge_server_running = True
        return {"FINISHED"}


class FORGE_OT_StopServer(bpy.types.Operator):
    bl_idname = "forge.stop_server"
    bl_label = "Disconnect"
    bl_description = "Stop the Forge socket server"

    def execute(self, context):
        if getattr(bpy.types, "forge_server", None):
            bpy.types.forge_server.stop()
            del bpy.types.forge_server
        context.scene.forge_server_running = False
        return {"FINISHED"}


_CLASSES = (FORGE_PT_Panel, FORGE_OT_StartServer, FORGE_OT_StopServer)


def register():
    bpy.types.Scene.forge_port = IntProperty(
        name="Port", default=DEFAULT_PORT, min=1024, max=65535
    )
    bpy.types.Scene.forge_server_running = BoolProperty(name="Running", default=False)
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    if getattr(bpy.types, "forge_server", None):
        bpy.types.forge_server.stop()
        del bpy.types.forge_server
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.forge_port
    del bpy.types.Scene.forge_server_running


if __name__ == "__main__":
    register()
