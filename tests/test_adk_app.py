from __future__ import annotations

from types import SimpleNamespace

from server import adk_app
from server import adk_tools


def test_create_forge_agent_keeps_current_tools_available():
    agent = adk_app.create_forge_agent(model="gemini-2.5-flash")

    assert agent.name == "forge_concierge"
    assert agent.model == "gemini-2.5-flash"
    assert [tool.__name__ for tool in agent.tools] == [
        "generate_object",
        "get_selected_object",
        "explain_selected_object",
    ]
    assert "typed commands are first-class" in agent.instruction


def test_event_text_collects_text_parts():
    event = SimpleNamespace(
        content=SimpleNamespace(
            parts=[
                SimpleNamespace(text="hello "),
                SimpleNamespace(text="world"),
                SimpleNamespace(),
            ]
        )
    )

    assert adk_app._event_text(event) == "hello world"


def test_generate_object_wraps_text2blender_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(adk_tools, "resolve_text2blender_path", lambda: tmp_path)
    monkeypatch.setattr(adk_tools, "resolve_text2blender_command", lambda path: ["/tmp/t2b"])
    monkeypatch.setattr(
        adk_tools,
        "_run_text2blender_cli",
        lambda path, command, prompt: f"created from {prompt}",
    )

    result = adk_tools.generate_object("make a cube")

    assert result["ok"] is True
    assert result["source"] == "text2blender_cli"
    assert result["reply"] == "created from make a cube"
