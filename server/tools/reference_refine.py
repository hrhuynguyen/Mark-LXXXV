"""Reference-image refinement helpers for selected Blender targets."""

from __future__ import annotations

import json
from typing import Any

from server.tools.blender_selection import BlenderCommandClient
from server.tools.reference_images import ReferenceImage


def create_restore_point(
    client: BlenderCommandClient,
    *,
    target_name: str,
    scope: str = "part",
) -> dict[str, Any]:
    """Create hidden backups for the selected part or its containing object group."""

    code = _CREATE_RESTORE_POINT_CODE.format(
        target_name=json.dumps(target_name),
        scope=json.dumps(scope),
    )
    result = client.send_command("execute_code", {"code": code})
    return _parse_json_from_stdout(str(result.get("result", "")))


def restore_from_restore_point(client: BlenderCommandClient, restore_point: dict[str, Any]) -> dict[str, Any]:
    """Restore hidden backups created before reference refinement."""

    code = _RESTORE_POINT_CODE.format(restore_point=json.dumps(restore_point))
    result = client.send_command("execute_code", {"code": code})
    return _parse_json_from_stdout(str(result.get("result", "")))


def prepare_part_replacement(
    client: BlenderCommandClient,
    *,
    target_name: str,
) -> dict[str, Any]:
    """Back up and hide a selected part before generating a replacement."""

    restore_point = create_restore_point(client, target_name=target_name, scope="part")
    if not restore_point.get("ok"):
        return restore_point

    code = _HIDE_TARGET_FOR_REPLACEMENT_CODE.format(target_name=json.dumps(target_name))
    result = client.send_command("execute_code", {"code": code})
    hidden = _parse_json_from_stdout(str(result.get("result", "")))
    restore_point["replacement"] = hidden
    return restore_point


def build_reference_refine_prompt(
    *,
    snapshot: dict[str, Any],
    reference: ReferenceImage,
    reference_description: str,
    scope: str,
) -> str:
    target_name = str(snapshot.get("name") or "selected object")
    target_label = "the whole related object/group" if scope == "object" else "only the selected part"
    return f"""Improve {target_label} in the current Blender scene using the chosen visual reference.

Target Blender object name: {target_name}
Scope: {scope}
Reference image title: {reference.title}
Reference image URL: {reference.image_url}
Reference source page: {reference.context_url}
Reference visual analysis: {reference_description}

Instructions:
- First inspect the scene and locate the target by the exact Blender object name above.
- Refine the target's shape, proportions, material, color, and distinctive details to better match the reference analysis.
- Preserve the target's approximate location and scale, unless the reference clearly implies a better proportion.
- Do not modify unrelated objects.
- Keep existing surrounding scene context intact.
- Reply with a concise summary of what changed.
"""


def build_part_replacement_prompt(
    *,
    snapshot: dict[str, Any],
    replacement_request: str,
) -> str:
    target_name = str(snapshot.get("name") or "selected part")
    parent = str(snapshot.get("parent") or "")
    dimensions = snapshot.get("dimensions") or []
    location = snapshot.get("location") or []
    rotation = snapshot.get("rotation") or []
    scale = snapshot.get("scale") or []
    materials = snapshot.get("materials") or []
    parent_line = f"Parent object/group: {parent}" if parent else "Parent object/group: none"

    return f"""Replace one selected part in the current Blender scene by creating a new part.

Original selected part name: {target_name}
{parent_line}
Original dimensions: {dimensions}
Original location: {location}
Original rotation: {rotation}
Original scale: {scale}
Original materials: {materials}
Replacement request: {replacement_request}

Instructions:
- The original selected part has already been backed up and hidden. Do not delete it.
- Create only the new replacement part, not a whole new object.
- Place the new part where the original part was, preserving its approximate attachment point, orientation, and scale.
- Match the surrounding object so the replacement looks connected and intentional.
- Add the requested shape/material/detail.
- Do not modify unrelated objects.
- Name the new replacement clearly using the original part name and the word Replacement.
- Reply with a concise summary of the replacement.
"""


def build_part_addition_prompt(
    *,
    snapshot: dict[str, Any],
    addition_request: str,
) -> str:
    target_name = str(snapshot.get("name") or "selected part")
    parent = str(snapshot.get("parent") or "")
    dimensions = snapshot.get("dimensions") or []
    location = snapshot.get("location") or []
    rotation = snapshot.get("rotation") or []
    scale = snapshot.get("scale") or []
    materials = snapshot.get("materials") or []
    parent_line = f"Parent object/group: {parent}" if parent else "Parent object/group: none"

    return f"""Add a new part to the existing object in the current Blender scene.

Selected anchor part name: {target_name}
{parent_line}
Anchor dimensions: {dimensions}
Anchor location: {location}
Anchor rotation: {rotation}
Anchor scale: {scale}
Anchor materials: {materials}
New part request: {addition_request}

Instructions:
- Keep the original selected part and original object visible and unchanged.
- Create only the new additional part, not a whole new object.
- Attach or place the new part near the selected anchor part, preserving the surrounding object's scale and style.
- Match the surrounding object so the new part looks connected and intentional.
- Add the requested shape/material/detail.
- Do not modify unrelated objects.
- Name the new part clearly using the selected anchor part name and the word AddOn.
- Reply with a concise summary of the added part.
"""


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
    raise ValueError("Blender did not return restore-point JSON")


_CREATE_RESTORE_POINT_CODE = r"""
import bpy
import json
import time

target_name = {target_name}
scope = {scope}
target = bpy.data.objects.get(target_name)
if target is None:
    print(json.dumps({{"ok": False, "error": f"Target not found: {{target_name}}"}}))
else:
    scene_before = [obj.name for obj in bpy.data.objects]
    backup_collection_name = "Forge_Reference_Backups"
    backup_collection = bpy.data.collections.get(backup_collection_name)
    if backup_collection is None:
        backup_collection = bpy.data.collections.new(backup_collection_name)
        bpy.context.scene.collection.children.link(backup_collection)

    if scope == "object":
        if target.parent is not None:
            root = target.parent
            while root.parent is not None:
                root = root.parent
            targets = [root] + list(root.children_recursive)
        elif target.users_collection:
            coll = target.users_collection[0]
            targets = [obj for obj in coll.objects if obj.type in {{"MESH", "EMPTY", "CURVE", "FONT"}}]
        else:
            targets = [target]
    else:
        targets = [target]

    backup_prefix = f"ForgeBackup_{{int(time.time())}}_"
    target_names = {{obj.name for obj in targets}}
    backup_by_original = {{}}
    backups = []
    for obj in targets:
        world_matrix = obj.matrix_world.copy()
        dup = obj.copy()
        if obj.data is not None:
            dup.data = obj.data.copy()
        dup.name = backup_prefix + obj.name
        dup.hide_viewport = True
        dup.hide_render = True
        backup_collection.objects.link(dup)
        dup.matrix_world = world_matrix
        backup_by_original[obj.name] = dup.name
        backups.append({{
            "original": obj.name,
            "backup": dup.name,
            "parent": obj.parent.name if obj.parent is not None and obj.parent.name in target_names else None,
            "collections": [coll.name for coll in obj.users_collection],
        }})

    for item in backups:
        parent_name = item.get("parent")
        if not parent_name:
            continue
        backup = bpy.data.objects.get(item.get("backup"))
        parent_backup = bpy.data.objects.get(backup_by_original.get(parent_name))
        if backup is not None and parent_backup is not None:
            world_matrix = backup.matrix_world.copy()
            backup.parent = parent_backup
            backup.matrix_world = world_matrix

    print(json.dumps({{
        "ok": True,
        "scope": scope,
        "target": target.name,
        "scene_before": scene_before,
        "backups": backups,
    }}))
"""


_HIDE_TARGET_FOR_REPLACEMENT_CODE = r"""
import bpy
import json

target_name = {target_name}
target = bpy.data.objects.get(target_name)
if target is None:
    print(json.dumps({{"ok": False, "error": f"Target not found: {{target_name}}"}}))
else:
    target.hide_viewport = True
    target.hide_render = True
    target.select_set(False)
    print(json.dumps({{
        "ok": True,
        "hidden_original": target.name,
        "message": "Original selected part hidden; backup remains available for restore.",
    }}))
"""


_RESTORE_POINT_CODE = r"""
import bpy
import json

restore_point = {restore_point}
if not restore_point.get("ok"):
    print(json.dumps({{"ok": False, "error": "Invalid restore point."}}))
else:
    scene_before = set(restore_point.get("scene_before") or [])
    backups = restore_point.get("backups") or []
    backup_names = {{item.get("backup") for item in backups}}

    # Remove newly created objects from the refinement pass.
    for obj in list(bpy.data.objects):
        if obj.name not in scene_before and obj.name not in backup_names:
            bpy.data.objects.remove(obj, do_unlink=True)

    restored_objects = {{}}
    restored = []
    for item in backups:
        original_name = item.get("original")
        backup_name = item.get("backup")
        backup = bpy.data.objects.get(backup_name)
        if backup is None or not original_name:
            continue

        current = bpy.data.objects.get(original_name)
        if current is not None and current.name != backup.name:
            bpy.data.objects.remove(current, do_unlink=True)

        backup.name = original_name
        if backup.data is not None:
            backup.data.name = original_name + "_mesh"
        backup.hide_viewport = False
        backup.hide_render = False
        restored_objects[original_name] = backup
        restored.append(original_name)

        for collection_name in item.get("collections") or []:
            collection = bpy.data.collections.get(collection_name)
            if collection is not None and backup.name not in collection.objects:
                collection.objects.link(backup)

    for item in backups:
        original_name = item.get("original")
        parent_name = item.get("parent")
        obj = restored_objects.get(original_name)
        if obj is not None:
            parent = restored_objects.get(parent_name) if parent_name else None
            world_matrix = obj.matrix_world.copy()
            obj.parent = parent
            obj.matrix_world = world_matrix

    backup_collection = bpy.data.collections.get("Forge_Reference_Backups")
    if backup_collection is not None:
        for obj in restored_objects.values():
            if obj.name in backup_collection.objects and len(obj.users_collection) > 1:
                backup_collection.objects.unlink(obj)

    print(json.dumps({{"ok": True, "restored": restored}}))
"""
