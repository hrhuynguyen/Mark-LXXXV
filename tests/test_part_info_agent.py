from __future__ import annotations

from server.agents.part_info_agent import build_part_info_prompt


def test_part_info_prompt_contains_selected_part_metadata():
    prompt = build_part_info_prompt(
        {
            "name": "landing_leg_1",
            "type": "MESH",
            "dimensions": [0.2, 0.4, 2.8],
            "materials": ["black_metal"],
            "mesh": {"vertices": 24, "edges": 36, "polygons": 12},
            "ignored_large_field": "not included",
        }
    )

    assert "landing_leg_1" in prompt
    assert "black_metal" in prompt
    assert "ignored_large_field" not in prompt
