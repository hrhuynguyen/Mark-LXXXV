from __future__ import annotations

import asyncio

from server.build import executor
from server.build import text2blender_adapter as adapter
from server.build.text2blender_adapter import (
    DEFAULT_TEXT2BLENDER_PATH,
    build_text2blender_prompt,
)
from server.runtime.part_registry import PartRegistry


class _Bridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        if name == "get_object_info":
            return {"world_bounding_box": [[0, 0, 0], [1, 1, 1]]}
        return {"ok": True}


def test_text2blender_prompt_preserves_build_spec_parts():
    prompt = build_text2blender_prompt(
        {
            "object": "falcon9",
            "summary": "two-stage rocket",
            "parts": [
                {
                    "name": "stage1_body",
                    "generator": "cylinder",
                    "params": {"radius": 1.8, "height": 42},
                    "place": "origin",
                }
            ],
        }
    )

    assert "falcon9" in prompt
    assert "two-stage rocket" in prompt
    assert "stage1_body" in prompt
    assert "cylinder" in prompt
    assert str(DEFAULT_TEXT2BLENDER_PATH).endswith("Text2Blender")


def test_resolve_text2blender_command_prefers_project_cli(tmp_path, monkeypatch):
    monkeypatch.delenv("TEXT2BLENDER_COMMAND", raising=False)
    cli = tmp_path / ".venv" / "bin" / "text2blender"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/bin/sh\n", encoding="utf-8")

    assert adapter.resolve_text2blender_command(tmp_path) == [str(cli)]


def test_run_text2blender_cli_uses_project_cwd_and_prompt(tmp_path, monkeypatch):
    monkeypatch.delenv("TEXT2BLENDER_MODEL", raising=False)
    call: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = "created cube\n"
        stderr = ""

    def fake_run(args, **kwargs):
        call["args"] = args
        call["cwd"] = kwargs["cwd"]
        call["timeout"] = kwargs["timeout"]
        return Completed()

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)

    reply = adapter._run_text2blender_cli(
        tmp_path,
        ["/tmp/text2blender"],
        "Create a cube.",
    )

    assert reply == "created cube"
    assert call["args"] == ["/tmp/text2blender", "--prompt", "Create a cube."]
    assert call["cwd"] == str(tmp_path)
    assert call["timeout"] == 300


def test_execute_build_spec_delegates_to_text2blender_by_default(monkeypatch):
    async def fake_text2blender_build(spec):
        return {
            "object": spec["object"],
            "summary": spec.get("summary"),
            "generator": "text2blender",
            "source_path": "/tmp/Text2Blender",
            "prompt": "create cube",
            "reply": "created cube",
        }

    monkeypatch.setattr(executor, "execute_text2blender_build", fake_text2blender_build)

    registry = PartRegistry()
    result = asyncio.run(
        executor.execute_build_spec(
            _Bridge(),
            registry,
            {"object": "cube", "summary": "simple test object"},
        )
    )

    assert result["generator"] == "text2blender"
    assert result["reply"] == "created cube"
    assert result["built_parts"] == ["cube"]
    assert registry.get("cube").kind == "text2blender"


def test_execute_build_spec_can_fall_back_to_procedural(monkeypatch):
    monkeypatch.setenv("FORGE_USE_TEXT2BLENDER", "0")
    bridge = _Bridge()
    registry = PartRegistry()

    result = asyncio.run(
        executor.execute_build_spec(
            bridge,
            registry,
            {
                "object": "demo",
                "parts": [
                    {
                        "name": "demo_body",
                        "generator": "box",
                        "params": {"size": 1.0},
                    }
                ],
            },
        )
    )

    assert result["built_parts"] == ["demo_body"]
    assert registry.get("demo_body").kind == "procedural"
    assert any(name == "execute_blender_code" for name, _args in bridge.calls)
