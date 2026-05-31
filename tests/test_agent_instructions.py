"""Regression checks for high-risk voice routing instructions."""

from __future__ import annotations

from server.agents.blender_agent import blender_agent
from server.agents.concierge import root_agent


def _instruction(agent) -> str:
    return str(getattr(agent, "instruction", "")).lower()


def test_concierge_routes_build_requests_as_digital_blender_models() -> None:
    instruction = _instruction(root_agent)
    assert "digital blender modeling app" in instruction
    assert "build a rocket" in instruction
    assert "design/decide/model" in instruction
    assert "never answer that you cannot physically build" in instruction
    assert "you are not a web-browser assistant" in instruction
    assert "call spec_agent, then call build_from_spec" in instruction


def test_concierge_has_direct_blender_tools() -> None:
    names = {
        getattr(tool, "name", "") or getattr(tool, "__name__", "")
        for tool in getattr(root_agent, "tools", [])
    }
    assert "spec_agent" in names
    assert "build_from_spec" in names
    assert "execute_blender_code" in names
    assert "pick_object_at" in names


def test_blender_agent_never_refuses_build_as_physical_task() -> None:
    instruction = _instruction(blender_agent)
    assert "digital 3d models inside blender" in instruction
    assert "build a falcon 9" in instruction
    assert "never apologize that you cannot physically build" in instruction
    assert "build the digital blender model" in instruction
