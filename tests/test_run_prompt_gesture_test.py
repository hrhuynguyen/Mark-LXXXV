from __future__ import annotations

from scripts import run_prompt_gesture_test as harness


def test_spoken_builds_use_direct_text2blender_by_default():
    args = harness.build_arg_parser().parse_args([])

    assert harness._voice_uses_adk(args) is False


def test_spoken_builds_can_opt_into_adk():
    args = harness.build_arg_parser().parse_args(["--voice-adk"])

    assert harness._voice_uses_adk(args) is True


def test_voice_direct_keeps_adk_off_for_compatibility():
    args = harness.build_arg_parser().parse_args(["--voice-adk", "--voice-direct"])

    assert harness._voice_uses_adk(args) is False


def test_run_prompt_sends_gemini_improved_prompt_to_text2blender(monkeypatch, tmp_path):
    seen: dict = {}

    monkeypatch.setattr(harness, "_improve_prompt_for_generation", lambda prompt: f"detailed {prompt}")
    monkeypatch.setattr(harness, "resolve_text2blender_path", lambda: tmp_path)
    monkeypatch.setattr(harness, "resolve_text2blender_command", lambda path: ["/tmp/t2b"])

    def fake_run(path, command, prompt):
        seen["path"] = path
        seen["command"] = command
        seen["prompt"] = prompt
        return "created"

    monkeypatch.setattr(harness, "_run_text2blender_cli", fake_run)

    assert harness._run_prompt("rocket", improve_prompt=True) == 0
    assert seen["prompt"] == "detailed rocket"


def test_run_prompt_can_skip_prompt_improvement(monkeypatch, tmp_path):
    seen: dict = {}

    monkeypatch.setattr(
        harness,
        "_improve_prompt_for_generation",
        lambda prompt: (_ for _ in ()).throw(AssertionError("should not improve")),
    )
    monkeypatch.setattr(harness, "resolve_text2blender_path", lambda: tmp_path)
    monkeypatch.setattr(harness, "resolve_text2blender_command", lambda path: ["/tmp/t2b"])
    monkeypatch.setattr(
        harness,
        "_run_text2blender_cli",
        lambda path, command, prompt: seen.setdefault("prompt", prompt) or "created",
    )

    assert harness._run_prompt("reference refine prompt", improve_prompt=False) == 0
    assert seen["prompt"] == "reference refine prompt"
