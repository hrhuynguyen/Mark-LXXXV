"""Walk a Build Spec and drive Blender through the session bridge."""

from __future__ import annotations

import json
from typing import Any

from ..runtime.part_registry import PartEntry, PartRegistry
from ..runtime.session_bridge import SessionBridge
from .generators import generate_part_code, placement_code
from .text2blender_adapter import execute_text2blender_build, text2blender_enabled


async def execute_build_spec(
    bridge: SessionBridge,
    registry: PartRegistry,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Create a Blender object from a Build Spec.

    The default path now runs the user's local Text2Blender CLI from that
    project folder. Set ``FORGE_USE_TEXT2BLENDER=0`` or ``spec["generator"] =
    "procedural"`` to use the older Forge procedural snippets.
    """
    if text2blender_enabled(spec):
        return await _execute_text2blender_build(registry, spec)
    return await _execute_procedural_build_spec(bridge, registry, spec)


async def _execute_text2blender_build(
    registry: PartRegistry,
    spec: dict[str, Any],
) -> dict[str, Any]:
    result = await execute_text2blender_build(spec)
    object_slug = str(result.get("object") or "build")
    registry.object = object_slug
    registry.add(
        PartEntry(
            name=object_slug,
            kind="text2blender",
            params={
                "prompt": result["prompt"],
                "source_path": result["source_path"],
                "runner": result.get("runner"),
                "command": result.get("command"),
            },
            generator="text2blender",
            parent=None,
            bbox=None,
        )
    )
    result["built_parts"] = [object_slug]
    result["registry"] = registry.to_json()
    return result


async def _execute_procedural_build_spec(
    bridge: SessionBridge,
    registry: PartRegistry,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Create parts procedurally, place them, and register metadata."""
    object_slug = str(spec.get("object") or "build")
    registry.object = object_slug
    parts = spec.get("parts") or []
    if not isinstance(parts, list):
        raise ValueError("build spec parts must be a list")

    root_code = f"""
import bpy
coll_name = {_json(object_slug)}
if coll_name not in bpy.data.collections:
    coll = bpy.data.collections.new(coll_name)
    bpy.context.scene.collection.children.link(coll)
root = bpy.data.objects.new(coll_name + '_root', None)
root.empty_display_type = 'PLAIN_AXES'
bpy.context.scene.collection.objects.link(root)
"""
    await bridge.call_tool("execute_blender_code", {"code": root_code})

    built: list[str] = []
    parent_bboxes: dict[str, list | None] = {object_slug: None}

    for raw in parts:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "")
        if not name:
            continue
        generator = str(raw.get("generator") or "box")
        params = dict(raw.get("params") or {})
        parent = str(raw.get("parent") or object_slug)
        place = str(raw.get("place") or "origin")

        code = generate_part_code(name, generator, params)
        result = await bridge.call_tool("execute_blender_code", {"code": code})
        parent_bbox = parent_bboxes.get(parent)
        place_code = placement_code(name, place, parent_bbox)
        await bridge.call_tool("execute_blender_code", {"code": place_code})

        info = await bridge.call_tool("get_object_info", {"name": name})
        bbox = info.get("world_bounding_box")
        parent_bboxes[name] = bbox

        registry.add(
            PartEntry(
                name=name,
                kind="procedural",
                params=params,
                generator=generator,
                parent=parent,
                bbox=bbox,
            )
        )
        built.append(name)

    return {
        "object": object_slug,
        "summary": spec.get("summary"),
        "built_parts": built,
        "registry": registry.to_json(),
    }


def _json(value: Any) -> str:
    return json.dumps(value)
