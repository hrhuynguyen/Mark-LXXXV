"""Tests for LocalToolExecutor — the client relay that forwards server tool_calls
to the Blender socket bridge (Step 4). Uses fakes for the bridge + WS sender.
"""

from __future__ import annotations

import asyncio

from client.blender_bridge import BlenderError
from client.local_executor import LocalToolExecutor


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_json(self, kind: str, payload: dict) -> int:
        self.sent.append((kind, payload))
        return len(self.sent)

    async def send_bytes(self, kind: str, data: bytes) -> int:  # pragma: no cover - unused
        return 0


class FakeBridge:
    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.result = {"ok": True} if result is None else result
        self.error = error

    def send_command(self, tool: str, args: dict):
        self.calls.append((tool, args))
        if self.error is not None:
            raise self.error
        return self.result


def _run(coro):
    return asyncio.run(coro)


def test_forwards_tool_call_to_bridge():
    bridge = FakeBridge(result={"hit": True, "name": "Cube"})
    sender = FakeSender()
    ex = LocalToolExecutor(bridge, sender)
    handled = _run(
        ex.handle_message(
            {"type": "tool_call", "call_id": "c1", "tool": "get_object_info", "args": {"name": "Cube"}}
        )
    )
    assert handled is True
    assert bridge.calls == [("get_object_info", {"name": "Cube"})]
    kind, payload = sender.sent[-1]
    assert kind == "tool_result"
    assert payload == {
        "type": "tool_result",
        "call_id": "c1",
        "ok": True,
        "result": {"hit": True, "name": "Cube"},
    }


def test_non_tool_call_is_ignored():
    ex = LocalToolExecutor(FakeBridge(), FakeSender())
    assert _run(ex.handle_message({"type": "cursor", "x": 1, "y": 2})) is False


def test_dedup_same_call_id_dispatches_once():
    bridge = FakeBridge()
    sender = FakeSender()
    ex = LocalToolExecutor(bridge, sender)
    msg = {"type": "tool_call", "call_id": "dup", "tool": "get_scene_info", "args": {}}
    _run(ex.handle_message(msg))
    _run(ex.handle_message(msg))
    assert len(bridge.calls) == 1


def test_bridge_error_returns_ok_false():
    bridge = FakeBridge(error=BlenderError("boom"))
    sender = FakeSender()
    ex = LocalToolExecutor(bridge, sender)
    _run(ex.handle_message({"type": "tool_call", "call_id": "e1", "tool": "x", "args": {}}))
    payload = sender.sent[-1][1]
    assert payload["ok"] is False
    assert "boom" in payload["error"]


def test_missing_tool_name_errors_without_dispatch():
    bridge = FakeBridge()
    sender = FakeSender()
    ex = LocalToolExecutor(bridge, sender)
    _run(ex.handle_message({"type": "tool_call", "call_id": "m1", "tool": "", "args": {}}))
    assert bridge.calls == []
    assert sender.sent[-1][1]["ok"] is False


def test_non_dict_result_is_wrapped():
    bridge = FakeBridge(result="plain string")
    sender = FakeSender()
    ex = LocalToolExecutor(bridge, sender)
    _run(ex.handle_message({"type": "tool_call", "call_id": "w1", "tool": "tts", "args": {}}))
    assert sender.sent[-1][1]["result"] == {"result": "plain string"}


def test_execute_blender_code_maps_to_addon_execute_code():
    bridge = FakeBridge(result={"executed": True})
    sender = FakeSender()
    ex = LocalToolExecutor(bridge, sender)
    _run(
        ex.handle_message(
            {
                "type": "tool_call",
                "call_id": "code1",
                "tool": "execute_blender_code",
                "args": {"code": "print('x')"},
            }
        )
    )
    assert bridge.calls == [("execute_code", {"code": "print('x')"})]


def test_pick_object_at_injects_current_region_point():
    bridge = FakeBridge(result={"hit": True, "name": "Cube"})
    sender = FakeSender()
    ex = LocalToolExecutor(bridge, sender, region_point_provider=lambda: (12.4, 56.6))
    _run(ex.handle_message({"type": "tool_call", "call_id": "p1", "tool": "pick_object_at", "args": {}}))
    assert bridge.calls == [("pick_object_at", {"region_x": 12, "region_y": 57})]


def test_pick_object_at_without_calibrated_cursor_errors():
    bridge = FakeBridge()
    sender = FakeSender()
    ex = LocalToolExecutor(bridge, sender, region_point_provider=lambda: None)
    _run(ex.handle_message({"type": "tool_call", "call_id": "p2", "tool": "pick_object_at", "args": {}}))
    assert bridge.calls == []
    assert sender.sent[-1][1]["ok"] is False
    assert "no calibrated cursor" in sender.sent[-1][1]["error"]
