"""Tests for the standalone MCP-to-Blender bridge helpers."""

from __future__ import annotations

import json

from server import mcp_blender_bridge


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.disconnected = False

    def send_command(self, command: str, params: dict | None = None):
        self.calls.append((command, params or {}))
        return {"ok": True, "command": command, "params": params or {}}

    def disconnect(self) -> None:
        self.disconnected = True


def test_json_result_is_stable_pretty_json() -> None:
    payload = {"b": 2, "a": {"z": 1}}
    rendered = mcp_blender_bridge.json_result(payload)
    assert json.loads(rendered) == payload
    assert rendered.splitlines()[1].strip().startswith('"a"')


def test_send_blender_command_uses_persistent_connection(monkeypatch) -> None:
    fake = FakeConnection()
    monkeypatch.setattr(mcp_blender_bridge, "_blender_connection", fake)

    result = mcp_blender_bridge.send_blender_command("get_object_info", {"name": "Cube"})

    assert result == {
        "ok": True,
        "command": "get_object_info",
        "params": {"name": "Cube"},
    }
    assert fake.calls == [
        ("get_scene_info", {}),
        ("get_object_info", {"name": "Cube"}),
    ]


def test_close_blender_connection_disconnects(monkeypatch) -> None:
    fake = FakeConnection()
    monkeypatch.setattr(mcp_blender_bridge, "_blender_connection", fake)

    mcp_blender_bridge.close_blender_connection()

    assert fake.disconnected is True
    assert mcp_blender_bridge._blender_connection is None


def test_build_mcp_server_when_dependency_installed() -> None:
    try:
        import mcp  # noqa: F401
    except Exception:
        return

    server = mcp_blender_bridge.build_mcp_server()
    assert type(server).__name__ == "FastMCP"
