from __future__ import annotations

from server.tools.blender_selection import get_selected_object_snapshot


class _FakeBlender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    def send_command(self, command_type: str, params: dict | None = None) -> dict:
        self.calls.append((command_type, params))
        if command_type == "execute_code":
            return {
                "executed": True,
                "result": 'noise\n{"selected": true, "name": "landing_leg_1", "type": "MESH"}\n',
            }
        if command_type == "get_object_info":
            return {
                "name": params["name"],
                "mesh": {"vertices": 8, "edges": 12, "polygons": 6},
            }
        raise AssertionError(command_type)


def test_get_selected_object_snapshot_fetches_active_object_info():
    fake = _FakeBlender()

    snapshot = get_selected_object_snapshot(fake)

    assert snapshot is not None
    assert snapshot["name"] == "landing_leg_1"
    assert snapshot["object_info"]["mesh"]["vertices"] == 8
    assert fake.calls[1] == ("get_object_info", {"name": "landing_leg_1"})


def test_get_selected_object_snapshot_returns_none_without_selection():
    class NoSelection:
        def send_command(self, command_type: str, params: dict | None = None) -> dict:
            return {"executed": True, "result": '{"selected": false}\n'}

    assert get_selected_object_snapshot(NoSelection()) is None
