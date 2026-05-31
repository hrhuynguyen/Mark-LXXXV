from __future__ import annotations

import asyncio
import json

import pytest

from server.runtime.part_registry import (
    PartEntry,
    PartRegistry,
    PartRegistryError,
    clear_part_registry,
    get_part_registry,
    replace_part_registry,
)
from server.tools import remote_blender


def test_part_registry_round_trips_as_json() -> None:
    registry = PartRegistry(build_id="build-1", object_name="falcon9")
    registry.add(
        PartEntry(
            name="stage1_body",
            kind="procedural",
            params={"height": 42.6, "radius": 1.83},
            generator="cylinder",
            parent="falcon9",
            bbox=[[-1.83, -1.83, 0.0], [1.83, 1.83, 42.6]],
        )
    )
    registry.add_from_build_spec_part(
        {
            "name": "landing_leg_2",
            "generator": "landing_leg",
            "params": {"length": 8.0, "radius": 0.3},
            "parent": "stage1_body",
        },
        bbox=[[1.4, 0.0, -0.2], [3.0, 0.3, 6.0]],
    )

    dumped = json.loads(json.dumps(registry.to_json()))
    restored = PartRegistry.from_json(dumped)

    assert restored.to_json() == dumped
    assert restored.children("stage1_body")[0].name == "landing_leg_2"
    assert restored.scene_name_report(["stage1_body", "landing_leg_2"]) == {
        "registered_missing_in_scene": [],
        "scene_unregistered": [],
    }


def test_part_entry_rejects_non_json_params() -> None:
    with pytest.raises(PartRegistryError, match="params must be JSON-serializable"):
        PartEntry(
            name="bad_part",
            kind="procedural",
            params={"callable": lambda: None},
            generator="bad_generator",
        )


def test_session_registry_helpers_are_isolated() -> None:
    clear_part_registry("user-a", "session-a")
    clear_part_registry("user-a", "session-b")

    registry_a = get_part_registry("user-a", "session-a")
    registry_b = get_part_registry("user-a", "session-b")

    assert registry_a is not None
    assert registry_b is not None
    assert registry_a is not registry_b

    registry_a.add(PartEntry(name="fin", kind="procedural", params={}, generator="grid_fin"))
    assert registry_b.get("fin") is None

    assert clear_part_registry("user-a", "session-a") is registry_a
    assert clear_part_registry("user-a", "session-b") is registry_b


def test_pick_object_at_enriches_result_with_registry(monkeypatch) -> None:
    async def fake_call(tool_context, tool_name, args):
        assert tool_name == "pick_object_at"
        assert args == {}
        return {
            "hit": True,
            "name": "landing_leg_2",
            "world_bounding_box": [[0, 0, 0], [1, 1, 1]],
        }

    monkeypatch.setattr(remote_blender, "_call", fake_call)
    monkeypatch.setattr(remote_blender, "_get_uid_sid", lambda _tc: ("user", "session"))

    registry = PartRegistry(build_id="build-1", object_name="falcon9")
    registry.add(
        PartEntry(
            name="landing_leg_2",
            kind="procedural",
            params={"length": 8.0},
            generator="landing_leg",
            parent="stage1_body",
        )
    )
    replace_part_registry("user", "session", registry)
    try:
        result = asyncio.run(remote_blender.pick_object_at(object()))
    finally:
        clear_part_registry("user", "session")

    assert result["registry"]["name"] == "landing_leg_2"
    assert result["registry"]["params"] == {"length": 8.0}
    assert result["registry"]["bbox"] == [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]
