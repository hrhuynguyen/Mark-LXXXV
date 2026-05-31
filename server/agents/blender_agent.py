"""Native-audio modeling agent that controls local Blender through remote tools."""

from __future__ import annotations

import json
import os
import re
import textwrap
from dataclasses import dataclass
from typing import Any, Mapping

from google.adk.agents import Agent
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.tool_context import ToolContext

from server.agents.spec_agent import spec_agent
from server.callbacks.echo_dedupe import echo_dedupe_before_tool_callback
from server.callbacks.handoff_guard import transfer_audio_gate_before_tool_callback
from server.runtime.part_registry import PartRegistry, get_part_registry, replace_part_registry
from server.tools.remote_blender import (
    execute_blender_code,
    frame_object,
    get_object_info,
    get_viewport_screenshot,
    pick_object_at,
)
from server.tools.remote_blender import _get_uid_sid as _get_tool_context_ids


MODEL = os.getenv("FORGE_BLENDER_MODEL", "gemini-2.5-flash-native-audio-latest")

SUPPORTED_GENERATORS = {
    "cylinder",
    "cone",
    "cube",
    "engine_cluster",
    "grid_fin",
    "landing_leg",
}

DEFAULT_MATERIALS: dict[str, dict[str, Any]] = {
    "white_matte": {"base_color": [0.92, 0.92, 0.86, 1.0], "roughness": 0.85},
    "dark_metal": {"base_color": [0.04, 0.045, 0.05, 1.0], "roughness": 0.45},
    "black_rubber": {"base_color": [0.01, 0.01, 0.012, 1.0], "roughness": 0.75},
    "brushed_metal": {"base_color": [0.55, 0.56, 0.58, 1.0], "roughness": 0.35},
}


@dataclass(frozen=True)
class BuildPartInstance:
    name: str
    generator: str
    params: dict[str, Any]
    parent: str
    place: str
    material: str | None
    build_object: str
    source_name: str
    instance_index: int | None = None
    instance_count: int | None = None


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL)
    return fenced.group(1).strip() if fenced else text


def normalize_build_spec(spec: Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(spec, str):
        spec = json.loads(_strip_json_fence(spec))
    if not isinstance(spec, Mapping):
        raise ValueError("Build Spec must be a JSON object")

    object_name = str(spec.get("object") or "").strip()
    if not object_name:
        raise ValueError("Build Spec requires object")

    parts = spec.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("Build Spec requires a non-empty parts list")

    normalized_parts: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for raw_part in parts:
        if not isinstance(raw_part, Mapping):
            raise ValueError("Each Build Spec part must be a JSON object")
        name = str(raw_part.get("name") or "").strip()
        generator = str(raw_part.get("generator") or "").strip()
        parent = str(raw_part.get("parent") or object_name).strip()
        if not name or not generator or not parent:
            raise ValueError("Each Build Spec part requires name, generator, and parent")
        if generator not in SUPPORTED_GENERATORS:
            raise ValueError(f"Unsupported generator: {generator}")
        if name in seen_names:
            raise ValueError(f"Duplicate Build Spec part name: {name}")
        seen_names.add(name)
        params = raw_part.get("params") or {}
        if not isinstance(params, Mapping):
            raise ValueError(f"Part params must be a JSON object: {name}")
        normalized_parts.append(
            {
                "name": name,
                "generator": generator,
                "params": json.loads(json.dumps(dict(params))),
                "parent": parent,
                "place": str(raw_part.get("place") or "origin"),
                "material": raw_part.get("material"),
            }
        )

    materials = spec.get("materials") or {}
    if not isinstance(materials, Mapping):
        raise ValueError("Build Spec materials must be a JSON object when present")

    return {
        "object": object_name,
        "summary": str(spec.get("summary") or f"Building {object_name} now."),
        "lod_budget": str(spec.get("lod_budget") or "medium"),
        "parts": normalized_parts,
        "materials": json.loads(json.dumps(dict(materials))),
    }


def expand_part_instances(spec: Mapping[str, Any]) -> list[BuildPartInstance]:
    normalized = normalize_build_spec(spec)
    build_object = normalized["object"]
    instances: list[BuildPartInstance] = []
    instance_names: set[str] = set()
    for part in normalized["parts"]:
        params = dict(part["params"])
        count = int(params.get("count", 1) or 1)
        if count < 1:
            raise ValueError(f"Part count must be >= 1: {part['name']}")
        expands = part["generator"] in {"grid_fin", "landing_leg"} and count > 1
        for idx in range(count if expands else 1):
            name = f"{part['name']}_{idx + 1}" if expands else part["name"]
            if name in instance_names:
                raise ValueError(f"Duplicate expanded part name: {name}")
            instance_names.add(name)
            instance_params = dict(params)
            if expands:
                instance_params["count"] = count
                instance_params["index"] = idx
            instances.append(
                BuildPartInstance(
                    name=name,
                    generator=part["generator"],
                    params=instance_params,
                    parent=part["parent"],
                    place=part["place"],
                    material=part.get("material"),
                    build_object=build_object,
                    source_name=part["name"],
                    instance_index=idx if expands else None,
                    instance_count=count if expands else None,
                )
            )
    return instances


def _resolve_material(
    material_name: str | None,
    materials: Mapping[str, Any],
    generator: str,
) -> tuple[str, dict[str, Any]]:
    fallback_name = {
        "engine_cluster": "dark_metal",
        "landing_leg": "brushed_metal",
        "grid_fin": "brushed_metal",
    }.get(generator, "white_matte")
    name = str(material_name or fallback_name)
    raw = materials.get(name) or DEFAULT_MATERIALS.get(name) or DEFAULT_MATERIALS[fallback_name]
    if isinstance(raw, str):
        raw = DEFAULT_MATERIALS.get(raw, DEFAULT_MATERIALS[fallback_name])
    if not isinstance(raw, Mapping):
        raw = DEFAULT_MATERIALS[fallback_name]
    color = raw.get("base_color", DEFAULT_MATERIALS[fallback_name]["base_color"])
    if not isinstance(color, list) or len(color) not in {3, 4}:
        color = DEFAULT_MATERIALS[fallback_name]["base_color"]
    if len(color) == 3:
        color = [*color, 1.0]
    return name, {"base_color": [float(v) for v in color[:4]], "roughness": raw.get("roughness", 0.7)}


def build_part_code(
    instance: BuildPartInstance,
    *,
    materials: Mapping[str, Any] | None = None,
) -> str:
    material_name, material = _resolve_material(
        instance.material,
        materials or {},
        instance.generator,
    )
    config = {
        "name": instance.name,
        "generator": instance.generator,
        "params": instance.params,
        "parent": instance.parent,
        "place": instance.place,
        "build_object": instance.build_object,
        "material_name": material_name,
        "material": material,
    }
    return textwrap.dedent(
        f"""
        import bpy
        import json
        import math
        from mathutils import Vector

        CONFIG = {json.dumps(config, sort_keys=True)}

        def remove_existing(name):
            obj = bpy.data.objects.get(name)
            if obj is not None:
                bpy.data.objects.remove(obj, do_unlink=True)

        def ensure_collection(name):
            coll = bpy.data.collections.get(name)
            if coll is None:
                coll = bpy.data.collections.new(name)
                bpy.context.scene.collection.children.link(coll)
            return coll

        def ensure_empty(name, coll):
            obj = bpy.data.objects.get(name)
            if obj is None:
                obj = bpy.data.objects.new(name, None)
                coll.objects.link(obj)
            return obj

        def ensure_material(name, payload):
            mat = bpy.data.materials.get(name)
            if mat is None:
                mat = bpy.data.materials.new(name)
            mat.diffuse_color = payload.get("base_color", [0.8, 0.8, 0.8, 1.0])
            return mat

        def aabb(obj):
            if obj is None or obj.type != "MESH":
                return None
            world_corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
            mins = [min(c[i] for c in world_corners) for i in range(3)]
            maxs = [max(c[i] for c in world_corners) for i in range(3)]
            return [mins, maxs]

        def parent_bounds(parent_name):
            parent = bpy.data.objects.get(parent_name)
            bounds = aabb(parent)
            return parent, bounds

        def parent_radius(bounds, default=1.0):
            if not bounds:
                return default
            return max(abs(bounds[0][0]), abs(bounds[1][0]), abs(bounds[0][1]), abs(bounds[1][1]))

        def z_for(bounds, height, place):
            if not bounds:
                return height / 2.0
            if "base" in place:
                return bounds[0][2] - height / 2.0
            if "top" in place or "upper" in place:
                return bounds[1][2] + height / 2.0
            return (bounds[0][2] + bounds[1][2]) / 2.0

        def link_to_collection(obj, coll):
            if obj.name not in coll.objects:
                coll.objects.link(obj)

        def finish(obj, parent, coll, mat):
            obj.name = CONFIG["name"]
            obj.data.name = CONFIG["name"] + "_mesh"
            obj.data.materials.clear()
            obj.data.materials.append(mat)
            if parent is not None:
                obj.parent = parent
            link_to_collection(obj, coll)
            bpy.context.view_layer.update()
            return obj

        def make_cube(name, dims, location, rotation_z, parent, coll, mat):
            remove_existing(name)
            bpy.ops.mesh.primitive_cube_add(size=1, location=location, rotation=(0, 0, rotation_z))
            obj = bpy.context.object
            obj.dimensions = dims
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            obj.rotation_euler[2] = rotation_z
            return finish(obj, parent, coll, mat)

        def make_cylinder(name, radius, height, location, vertices, parent, coll, mat):
            remove_existing(name)
            bpy.ops.mesh.primitive_cylinder_add(
                vertices=vertices,
                radius=radius,
                depth=height,
                location=location,
            )
            return finish(bpy.context.object, parent, coll, mat)

        def make_cone(name, radius1, radius2, height, location, vertices, parent, coll, mat):
            remove_existing(name)
            bpy.ops.mesh.primitive_cone_add(
                vertices=vertices,
                radius1=radius1,
                radius2=radius2,
                depth=height,
                location=location,
            )
            return finish(bpy.context.object, parent, coll, mat)

        params = CONFIG["params"]
        place = str(CONFIG.get("place") or "origin").lower()
        coll = ensure_collection(CONFIG["build_object"])
        root = ensure_empty(CONFIG["build_object"], coll)
        parent, bounds = parent_bounds(CONFIG["parent"])
        if parent is None:
            parent = root
        mat = ensure_material(CONFIG["material_name"], CONFIG["material"])

        generator = CONFIG["generator"]
        if generator == "cylinder":
            height = float(params.get("height", params.get("depth", 2.0)))
            radius = float(params.get("radius", 0.5))
            vertices = int(params.get("vertices", 64))
            obj = make_cylinder(
                CONFIG["name"],
                radius,
                height,
                (0, 0, z_for(bounds, height, place)),
                vertices,
                parent,
                coll,
                mat,
            )
        elif generator == "cone":
            height = float(params.get("height", 2.0))
            radius1 = float(params.get("radius", params.get("radius1", 0.8)))
            radius2 = float(params.get("radius2", 0.0))
            vertices = int(params.get("vertices", 64))
            obj = make_cone(
                CONFIG["name"],
                radius1,
                radius2,
                height,
                (0, 0, z_for(bounds, height, place)),
                vertices,
                parent,
                coll,
                mat,
            )
        elif generator == "engine_cluster":
            height = float(params.get("height", 0.8))
            radius = float(params.get("radius", parent_radius(bounds, 1.0) * 0.75))
            obj = make_cylinder(
                CONFIG["name"],
                radius,
                height,
                (0, 0, z_for(bounds, height, "base")),
                48,
                parent,
                coll,
                mat,
            )
        elif generator == "grid_fin":
            count = max(1, int(params.get("count", 1)))
            index = int(params.get("index", 0))
            angle = (2.0 * math.pi * index) / count
            width = float(params.get("width", 1.2))
            height = float(params.get("height", 2.0))
            thickness = float(params.get("thickness", 0.12))
            radius = parent_radius(bounds, 1.0) + float(params.get("offset", 0.2))
            z = bounds[0][2] + (bounds[1][2] - bounds[0][2]) * 0.82 if bounds else height
            obj = make_cube(
                CONFIG["name"],
                (thickness, width, height),
                (math.cos(angle) * radius, math.sin(angle) * radius, z),
                angle,
                parent,
                coll,
                mat,
            )
        elif generator == "landing_leg":
            count = max(1, int(params.get("count", 1)))
            index = int(params.get("index", 0))
            angle = (2.0 * math.pi * index) / count
            length = float(params.get("length", 6.0))
            thickness = float(params.get("thickness", params.get("radius", 0.25)))
            radius = parent_radius(bounds, 1.0) + thickness
            z_min = bounds[0][2] if bounds else 0.0
            obj = make_cube(
                CONFIG["name"],
                (thickness, thickness, length),
                (math.cos(angle) * radius, math.sin(angle) * radius, z_min + length / 2.0),
                angle,
                parent,
                coll,
                mat,
            )
        else:
            dims = params.get("dimensions", [1.0, 1.0, 1.0])
            if len(dims) != 3:
                dims = [1.0, 1.0, 1.0]
            height = float(dims[2])
            obj = make_cube(
                CONFIG["name"],
                tuple(float(v) for v in dims),
                (0, 0, z_for(bounds, height, place)),
                0.0,
                parent,
                coll,
                mat,
            )

        print(json.dumps({{"name": obj.name, "world_bounding_box": aabb(obj)}}))
        """
    ).strip()


async def build_named_part(
    tool_context: ToolContext,
    part: dict[str, Any],
    build_object: str = "forge_build",
    materials: dict[str, Any] | None = None,
) -> dict:
    """Build one Build-Spec part in Blender and register every produced object."""
    spec = normalize_build_spec(
        {
            "object": build_object,
            "parts": [part],
            "materials": materials or {},
        }
    )
    user_id, session_id = _get_tool_context_ids(tool_context)
    registry = get_part_registry(user_id=user_id, session_id=session_id)
    if registry is None:
        registry = replace_part_registry(user_id, session_id, PartRegistry(object_name=build_object))
    registry.object_name = build_object

    built: list[dict[str, Any]] = []
    for instance in expand_part_instances(spec):
        code = build_part_code(instance, materials=spec["materials"])
        await execute_blender_code(tool_context, code)
        info = await get_object_info(tool_context, instance.name)
        registry_part = {
            "name": instance.name,
            "generator": instance.generator,
            "params": instance.params,
            "parent": instance.parent,
        }
        entry = registry.register_tool_result(part=registry_part, result=info, kind="procedural")
        built.append({"name": instance.name, "registry": entry.to_json(), "info": info})
    return {"ok": True, "built": built, "registry": registry.to_json()}


async def build_from_spec(tool_context: ToolContext, spec: dict[str, Any]) -> dict:
    """Execute a full Forge Build Spec now, registering each produced Blender object."""
    normalized = normalize_build_spec(spec)
    user_id, session_id = _get_tool_context_ids(tool_context)
    registry = replace_part_registry(
        user_id,
        session_id,
        PartRegistry(object_name=normalized["object"]),
    )

    built: list[dict[str, Any]] = []
    for part in normalized["parts"]:
        part_result = await build_named_part(
            tool_context,
            part,
            build_object=normalized["object"],
            materials=normalized["materials"],
        )
        built.extend(part_result["built"])

    return {
        "ok": True,
        "object": normalized["object"],
        "summary": normalized["summary"],
        "built_count": len(built),
        "built_names": [item["name"] for item in built],
        "registry": registry.to_json(),
    }


blender_agent = Agent(
    name="blender_agent",
    model=MODEL,
    description=(
        "Controls the user's local Blender scene through a remote tool bridge. "
        "Supports pointing via the user's calibrated hand cursor."
    ),
    instruction=(
        "You control the user's local Blender scene. The user can point with a hand cursor.\n"
        "Forge builds digital 3D models inside Blender. If the user says build, make, create, "
        "add, spawn, design, decide, or model an object, interpret it as a Blender scene-building task, "
        "not a physical-world request.\n"
        "'Here', 'this', 'right there', and 'this part' refer to the current cursor position.\n"
        "Keep spoken responses to one short sentence. Act first, confirm briefly after.\n\n"
        "TOOLS:\n"
        "- execute_blender_code(code): run Python inside Blender for simple scene edits.\n"
        "- spec_agent(request): expand a terse build request into a JSON Build Spec.\n"
        "- build_from_spec(spec): execute a full Build Spec and register every object.\n"
        "- build_named_part(part, build_object, materials): build/register one spec part.\n"
        "- pick_object_at(): raycast at the user's current calibrated cursor.\n"
        "- get_object_info(name): inspect a named Blender object.\n"
        "- get_viewport_screenshot(max_size=800): capture the current Blender viewport.\n"
        "- frame_object(name): frame a named object in the viewport.\n\n"
        "STEP-7 BUILD BEHAVIOR:\n"
        "- For requests like 'make/build/spawn a rocket' or 'build a Falcon 9', call spec_agent first.\n"
        "- Speak the spec summary in one short sentence, then immediately call build_from_spec.\n"
        "- Do not ask clarifying questions before building; use medium LOD unless the user says otherwise.\n"
        "- Never apologize that you cannot physically build something; build the digital Blender model.\n"
        "- Prefer named procedural parts over fused generated meshes.\n"
        "- After a build, the Part Registry is available. Pick results include registry metadata when tracked.\n\n"
        "SIMPLE / POINTED BEHAVIOR:\n"
        "- For 'add a cube' or other simple primitive requests, use execute_blender_code immediately.\n"
        "- For 'what is this?' or pointed questions, call pick_object_at first, then get_object_info if it hits.\n"
        "- If a pick misses, say you do not have a part under the cursor and ask the user to point again.\n"
        "- Name created objects clearly. Use separate Blender objects when there are distinct parts.\n"
        "- Do not claim transform/regenerate editing is available yet; that arrives in Step 9.\n\n"
        "TRANSFER TO CONCIERGE:\n"
        "- Pure factual questions with no Blender task -> transfer_to_agent('concierge').\n"
        "- Pure conversation -> transfer_to_agent('concierge')."
    ),
    before_tool_callback=[
        echo_dedupe_before_tool_callback,
        transfer_audio_gate_before_tool_callback,
    ],
    tools=[
        AgentTool(agent=spec_agent, skip_summarization=True),
        build_from_spec,
        build_named_part,
        execute_blender_code,
        pick_object_at,
        get_object_info,
        get_viewport_screenshot,
        frame_object,
    ],
)
