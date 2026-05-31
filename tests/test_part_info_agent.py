from __future__ import annotations

from types import SimpleNamespace

from server.agents.part_info_agent import (
    SPEECH_PLAYBACK_LOCK,
    _extract_live_audio,
    _extract_live_output_transcription,
    _join_audio_chunks,
    _split_text_for_speech,
    build_part_info_prompt,
    wait_for_speech_to_finish,
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


def test_split_text_for_speech_preserves_complete_sentences():
    segments = _split_text_for_speech(
        "This is a landing leg. It supports the rocket during touchdown. "
        "It folds away during flight.",
        max_chars=48,
    )

    assert segments == [
        "This is a landing leg.",
        "It supports the rocket during touchdown.",
        "It folds away during flight.",
    ]


def test_join_audio_chunks_adds_short_pause_between_chunks():
    audio = _join_audio_chunks([b"aa", b"", b"bb"], pause_seconds=0.01)

    assert audio.startswith(b"aa")
    assert audio.endswith(b"bb")
    assert len(audio) > len(b"aabb")


def test_wait_for_speech_to_finish_reports_locked_playback():
    assert wait_for_speech_to_finish(timeout=0.0) is True
    SPEECH_PLAYBACK_LOCK.acquire()
    try:
        assert wait_for_speech_to_finish(timeout=0.0) is False
    finally:
        SPEECH_PLAYBACK_LOCK.release()
