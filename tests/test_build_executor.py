from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from server.agents import blender_agent as blender_module
from server.agents.blender_agent import (
    build_from_spec,
    build_part_code,
    expand_part_instances,
    normalize_build_spec,
)
from server.runtime.part_registry import clear_part_registry


FALCON9_SPEC = {
    "object": "falcon9",
    "summary": "Falcon 9: two stages, nine-engine base, grid fins, and landing legs.",
    "lod_budget": "medium",
    "parts": [
        {
            "name": "stage1_body",
            "generator": "cylinder",
            "params": {"height": 42.6, "radius": 1.83},
            "parent": "falcon9",
            "place": "origin",
            "material": "white_matte",
        },
        {
            "name": "octaweb",
            "generator": "engine_cluster",
            "params": {"count": 9},
            "parent": "stage1_body",
            "place": "base",
            "material": "dark_metal",
        },
        {
            "name": "grid_fin",
            "generator": "grid_fin",
            "params": {"count": 4, "height": 1.5, "width": 1.2},
            "parent": "stage1_body",
            "place": "upper, 90 apart",
            "material": "brushed_metal",
        },
        {
            "name": "landing_leg",
            "generator": "landing_leg",
            "params": {"count": 4, "length": 8.0, "radius": 0.25},
            "parent": "stage1_body",
            "place": "base, 90 apart",
            "material": "brushed_metal",
        },
        {
            "name": "interstage",
            "generator": "cylinder",
            "params": {"height": 3.7, "radius": 1.83},
            "parent": "stage1_body",
            "place": "top",
            "material": "black_rubber",
        },
        {
            "name": "stage2_body",
            "generator": "cylinder",
            "params": {"height": 14.0, "radius": 1.83},
            "parent": "interstage",
            "place": "top",
            "material": "white_matte",
        },
        {
            "name": "fairing",
            "generator": "cone",
            "params": {"height": 13.0, "radius": 1.83, "radius2": 0.0},
            "parent": "stage2_body",
            "place": "top",
            "material": "white_matte",
        },
    ],
    "materials": {
        "white_matte": {"base_color": [0.92, 0.92, 0.86, 1.0], "roughness": 0.85},
        "dark_metal": {"base_color": [0.03, 0.03, 0.035, 1.0], "roughness": 0.4},
    },
}


def test_normalize_build_spec_accepts_json_string() -> None:
    spec = normalize_build_spec(json.dumps(FALCON9_SPEC))

    assert spec["object"] == "falcon9"
    assert spec["parts"][0]["name"] == "stage1_body"
    assert spec["parts"][0]["generator"] == "cylinder"


def test_expand_part_instances_suffixes_counted_editable_parts() -> None:
    names = [instance.name for instance in expand_part_instances(FALCON9_SPEC)]

    assert names == [
        "stage1_body",
        "octaweb",
        "grid_fin_1",
        "grid_fin_2",
        "grid_fin_3",
        "grid_fin_4",
        "landing_leg_1",
        "landing_leg_2",
        "landing_leg_3",
        "landing_leg_4",
        "interstage",
        "stage2_body",
        "fairing",
    ]


def test_build_part_code_contains_safe_config_and_generator() -> None:
    instance = expand_part_instances(FALCON9_SPEC)[2]
    code = build_part_code(instance, materials=FALCON9_SPEC["materials"])

    assert '"name": "grid_fin_1"' in code
    assert '"generator": "grid_fin"' in code
    assert "bpy.ops.mesh.primitive_cube_add" in code
    assert "CONFIG =" in code


def test_normalize_rejects_unsupported_generator() -> None:
    bad = dict(FALCON9_SPEC)
    bad["parts"] = [dict(FALCON9_SPEC["parts"][0], generator="python_eval")]

    with pytest.raises(ValueError, match="Unsupported generator"):
        normalize_build_spec(bad)


def test_build_from_spec_executes_and_registers_all_parts(monkeypatch) -> None:
    executed_names: list[str] = []

    async def fake_execute_blender_code(_tool_context, code: str) -> dict:
        marker = "CONFIG = "
        config_line = next(line for line in code.splitlines() if line.startswith(marker))
        config = json.loads(config_line.removeprefix(marker))
        executed_names.append(config["name"])
        return {"executed": True, "result": ""}

    async def fake_get_object_info(_tool_context, name: str) -> dict:
        return {
            "name": name,
            "type": "MESH",
            "world_bounding_box": [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
        }

    monkeypatch.setattr(blender_module, "execute_blender_code", fake_execute_blender_code)
    monkeypatch.setattr(blender_module, "get_object_info", fake_get_object_info)

    clear_part_registry("user", "session")
    tool_context = SimpleNamespace(user_id="user", session=SimpleNamespace(id="session"))
    try:
        result = asyncio.run(build_from_spec(tool_context, FALCON9_SPEC))
    finally:
        clear_part_registry("user", "session")

    assert result["ok"] is True
    assert result["built_count"] == 13
    assert executed_names == result["built_names"]
    assert "stage1_body" in result["registry"]["parts"]
    assert "grid_fin_4" in result["registry"]["parts"]
    assert "landing_leg_4" in result["registry"]["parts"]
    assert result["registry"]["parts"]["landing_leg_4"]["params"]["index"] == 3
