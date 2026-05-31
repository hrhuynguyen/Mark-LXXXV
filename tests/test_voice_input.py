from __future__ import annotations

import wave
from io import BytesIO
from types import SimpleNamespace

from client import voice_input
from client.voice_input import (
    build_build_prompt_improver_prompt,
    build_voice_command_prompt,
    clean_voice_command,
    improve_build_prompt,
    pcm16_rms,
    pcm16_to_wav_bytes,
)


def test_clean_voice_command_returns_first_executable_line():
    assert clean_voice_command("Command: /info\nextra") == "/info"
    assert clean_voice_command('"build a rocket"') == "build a rocket"


def test_build_voice_command_prompt_maps_common_spoken_intents():
    prompt = build_voice_command_prompt()

    assert "Wake phrase:" not in prompt
    assert "/add-part <short description of the new part>" in prompt
    assert "/info" in prompt
    assert "/refs part" in prompt
    assert "/refs object" in prompt
    assert "/replace <short description of the new part>" in prompt
    assert "/undo-ref" in prompt
    assert "improved build prompt" in prompt
    assert "detailed Falcon 9 rocket" in prompt
    assert "named visible parts" in prompt


def test_build_voice_command_prompt_can_require_wake_phrase():
    prompt = build_voice_command_prompt(wake_phrase="Hey Travis")

    assert "Wake phrase:" in prompt
    assert '"Hey Travis"' in prompt
    assert "Only return a command" in prompt
    assert "Remove the wake phrase" in prompt
    assert "return an empty string" in prompt


def test_build_prompt_improver_prompt_requests_modeling_detail():
    prompt = build_build_prompt_improver_prompt("make a rocket")

    assert "Return exactly one detailed prompt line" in prompt
    assert "named visible parts" in prompt
    assert "colors/materials" in prompt
    assert "User command: make a rocket" in prompt


def test_improve_build_prompt_uses_gemini_and_cleans_response(monkeypatch):
    seen: dict = {}

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            seen["api_key"] = api_key
            self.models = self

        def generate_content(self, *, model, contents, config):
            seen["model"] = model
            seen["contents"] = contents
            seen["config"] = config
            return SimpleNamespace(
                text='Command: "build a detailed rocket with fins and landing legs"'
            )

    monkeypatch.setenv("GOOGLE_API_KEY", "gemini-key")
    monkeypatch.setattr(voice_input.genai, "Client", FakeClient)

    result = improve_build_prompt("make a rocket", model="gemini-test")

    assert result == "build a detailed rocket with fins and landing legs"
    assert seen["api_key"] == "gemini-key"
    assert seen["model"] == "gemini-test"
    assert "User command: make a rocket" in seen["contents"]


def test_pcm16_to_wav_bytes_writes_valid_mono_wav():
    wav_bytes = pcm16_to_wav_bytes(b"\x00\x00\x01\x00", sample_rate=16000, channels=1)

    with wave.open(BytesIO(wav_bytes), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.readframes(2) == b"\x00\x00\x01\x00"


def test_pcm16_rms_detects_silence_and_signal():
    assert pcm16_rms(b"\x00\x00\x00\x00") == 0.0
    assert pcm16_rms(b"\x10\x00\xf0\xff") > 0.0
