from __future__ import annotations

from types import SimpleNamespace

from server.agents.part_info_agent import (
    _extract_live_audio,
    _extract_live_output_transcription,
    build_part_info_prompt,
)


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
    assert "Do not say the internal object name" in prompt
    assert "what it is and its application" in prompt


def test_extract_live_audio_and_transcription_from_server_message():
    message = SimpleNamespace(
        server_content=SimpleNamespace(
            output_transcription=SimpleNamespace(text="This is a leg."),
            model_turn=SimpleNamespace(
                parts=[
                    SimpleNamespace(inline_data=SimpleNamespace(data=b"abc")),
                    SimpleNamespace(inline_data=SimpleNamespace(data=b"def")),
                    SimpleNamespace(inline_data=SimpleNamespace(data="Z2hp")),
                ]
            ),
        )
    )

    assert _extract_live_output_transcription(message) == "This is a leg."
    assert _extract_live_audio(message) == b"abcdefghi"
