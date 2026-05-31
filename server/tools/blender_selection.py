"""Helpers for inspecting the currently selected Blender object."""

from __future__ import annotations

import json
from typing import Any, Protocol


class BlenderCommandClient(Protocol):
    def send_command(self, command_type: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        ...


SELECTED_OBJECT_CODE = r"""
import bpy
import json
import mathutils

obj = bpy.context.view_layer.objects.active
if obj is None and bpy.context.selected_objects:
    obj = bpy.context.selected_objects[0]

if obj is None:
    print(json.dumps({"selected": False}))
else:
    def vec3(value):
        return [round(float(value[0]), 4), round(float(value[1]), 4), round(float(value[2]), 4)]

    def aabb(mesh_obj):
        if mesh_obj.type != "MESH":
            return None
        corners = [mesh_obj.matrix_world @ mathutils.Vector(corner) for corner in mesh_obj.bound_box]
        mins = [min(c[i] for c in corners) for i in range(3)]
        maxs = [max(c[i] for c in corners) for i in range(3)]
        return [vec3(mins), vec3(maxs)]

    data = {
        "selected": True,
        "name": obj.name,
        "type": obj.type,
        "location": vec3(obj.location),
        "rotation": vec3(obj.rotation_euler),
        "scale": vec3(obj.scale),
        "dimensions": vec3(obj.dimensions),
        "visible": bool(obj.visible_get()),
        "parent": obj.parent.name if obj.parent else None,
        "children": [child.name for child in obj.children[:12]],
        "collections": [collection.name for collection in obj.users_collection],
        "materials": [
            slot.material.name
            for slot in obj.material_slots
            if slot.material is not None
        ],
        "world_bounding_box": aabb(obj),
    }

    if obj.type == "MESH" and obj.data:
        data["mesh"] = {
            "vertices": len(obj.data.vertices),
            "edges": len(obj.data.edges),
            "polygons": len(obj.data.polygons),
        }

    print(json.dumps(data))
"""


def get_selected_object_snapshot(client: BlenderCommandClient) -> dict[str, Any] | None:
    """Return a compact snapshot for Blender's active selected object."""

    result = client.send_command("execute_code", {"code": SELECTED_OBJECT_CODE})
    payload = _parse_json_from_stdout(str(result.get("result", "")))
    if not payload.get("selected"):
        return None

    name = str(payload.get("name") or "")
    if name:
        try:
            payload["object_info"] = client.send_command("get_object_info", {"name": name})
        except Exception as exc:
            payload["object_info_error"] = str(exc)
    return payload


def _parse_json_from_stdout(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("Blender did not return selected-object JSON")
