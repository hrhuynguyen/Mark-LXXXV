"""ADK tool wrappers around Forge's current working local actions."""

from __future__ import annotations

from typing import Any

from client.blender_bridge import BlenderConnection
from server.agents.part_info_agent import GeminiLivePartInfoAgent
from server.build.text2blender_adapter import (
    _run_text2blender_cli,
    resolve_text2blender_command,
    resolve_text2blender_path,
)
from server.tools.blender_selection import get_selected_object_snapshot


def generate_object(prompt: str) -> dict[str, Any]:
    """Generate a Blender object from a typed prompt using Text2Blender."""

    path = resolve_text2blender_path()
    command = resolve_text2blender_command(path)
    reply = _run_text2blender_cli(path, command, prompt)
    return {
        "ok": True,
        "tool": "generate_object",
        "source": "text2blender_cli",
        "reply": reply,
    }


def get_selected_object() -> dict[str, Any]:
    """Return the currently selected Blender object metadata."""

    conn = BlenderConnection()
    try:
        snapshot = get_selected_object_snapshot(conn)
    finally:
        conn.disconnect()
    if not snapshot:
        return {"ok": False, "selected": False, "message": "No Blender object is selected."}
    return {"ok": True, "selected": True, "object": snapshot}


def explain_selected_object(speak: bool = False) -> dict[str, Any]:
    """Explain what the selected Blender object is and what it is used for."""

    selected = get_selected_object()
    if not selected.get("ok"):
        return selected

    agent = GeminiLivePartInfoAgent()
    snapshot = selected["object"]
    if speak:
        explanation = agent.speak_part_live(snapshot)
        voice = "gemini_live_audio"
        audio_bytes = agent.last_audio_bytes
    else:
        explanation = agent.explain_part_generate_from_snapshot(snapshot)
        voice = "off"
        audio_bytes = 0

    return {
        "ok": True,
        "selected": True,
        "tool": "explain_selected_object",
        "voice": voice,
        "audio_bytes": audio_bytes,
        "explanation": explanation,
    }
