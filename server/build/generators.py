"""Bpy generator snippets keyed by Build Spec ``generator`` names."""

from __future__ import annotations

import json
from typing import Any


def _py(obj: Any) -> str:
    return json.dumps(obj)


def generate_part_code(name: str, generator: str, params: dict[str, Any]) -> str:
    """Return bpy code that creates a named mesh object for one spec part."""
    p = params or {}
    gen = (generator or "box").lower().replace("build_", "")

    if gen in {"cylinder", "body", "stage"}:
        r = float(p.get("radius", 1.0))
        h = float(p.get("height", 2.0))
        return f"""
import bpy
bpy.ops.mesh.primitive_cylinder_add(radius={r}, depth={h}, location=(0,0,{h/2}))
obj = bpy.context.active_object
obj.name = {_py(name)}
"""

    if gen in {"box", "cube", "fairing", "interstage"}:
        sx = float(p.get("width", p.get("size", 1.0)))
        sy = float(p.get("depth", p.get("size", 1.0)))
        sz = float(p.get("height", p.get("size", 1.0)))
        return f"""
import bpy
bpy.ops.mesh.primitive_cube_add(size=1, location=(0,0,{sz/2}))
obj = bpy.context.active_object
obj.scale = ({sx}, {sy}, {sz})
obj.name = {_py(name)}
"""

    if gen in {"sphere", "dome"}:
        r = float(p.get("radius", 1.0))
        return f"""
import bpy
bpy.ops.mesh.primitive_uv_sphere_add(radius={r}, location=(0,0,{r}))
obj = bpy.context.active_object
obj.name = {_py(name)}
"""

    if gen in {"engine_cluster", "octaweb", "engines"}:
        count = int(p.get("count", 9))
        r = float(p.get("radius", 0.35))
        ring_r = float(p.get("ring_radius", 1.2))
        codes = [
            f"""
import bpy, math
for i in range({count}):
    ang = (2*math.pi*i)/{count}
    x = math.cos(ang)*{ring_r}
    y = math.sin(ang)*{ring_r}
    bpy.ops.mesh.primitive_cylinder_add(radius={r}, depth={r*2}, location=(x,y,{r}))
    o = bpy.context.active_object
    o.name = {_py(name)} + ('_' + str(i) if {count} > 1 else '')
"""
        ]
        return codes[0]

    if gen in {"grid_fin", "fin"}:
        count = int(p.get("count", 4))
        h = float(p.get("height", 2.0))
        w = float(p.get("width", 0.4))
        return f"""
import bpy, math
for i in range({count}):
    ang = (2*math.pi*i)/{count}
    bpy.ops.mesh.primitive_cube_add(size=1, location=(math.cos(ang)*2, math.sin(ang)*2, {h/2}))
    o = bpy.context.active_object
    o.scale = ({w}, 0.1, {h})
    o.name = {_py(name)} + '_' + str(i)
"""

    if gen in {"landing_leg", "leg"}:
        count = int(p.get("count", 4))
        length = float(p.get("length", 2.0))
        return f"""
import bpy, math
for i in range({count}):
    ang = (2*math.pi*i)/{count} + math.pi/4
    x = math.cos(ang)*1.8
    y = math.sin(ang)*1.8
    bpy.ops.mesh.primitive_cylinder_add(radius=0.15, depth={length}, location=(x,y,{length/2}))
    o = bpy.context.active_object
    o.name = {_py(name)} + '_' + str(i)
"""

    # fallback
    return f"""
import bpy
bpy.ops.mesh.primitive_cube_add(size=1, location=(0,0,0.5))
obj = bpy.context.active_object
obj.name = {_py(name)}
"""


def placement_code(name: str, place: str, parent_bbox: list | None) -> str:
    """Rough AABB stacking from placement hints."""
    hint = (place or "origin").lower()
    z_offset = 0.0
    if parent_bbox and len(parent_bbox) == 2:
        z_offset = float(parent_bbox[1][2])
    if "upper" in hint or "top" in hint:
        z_offset += 1.0
    if "base" in hint or "bottom" in hint:
        z_offset = max(0.0, z_offset - 0.5)
    return f"""
import bpy
obj = bpy.data.objects.get({_py(name)})
if obj:
    obj.location.z += {z_offset}
"""
