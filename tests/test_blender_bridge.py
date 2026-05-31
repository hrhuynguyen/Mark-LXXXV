"""Unit tests for client.blender_bridge against a mock socket server.

No Blender required — a tiny in-process TCP server speaks the Seam 2 protocol
(CONTRACTS.md §3), including a deliberately *fragmented* reply to exercise the
"read chunks until the JSON parses" path that has no length framing.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from client.blender_bridge import BlenderConnection, BlenderError


class MockBlenderServer:
    """Single-client TCP server that returns a canned envelope per command."""

    def __init__(self, responder, fragment: bool = False) -> None:
        self._responder = responder  # (cmd_type, params) -> envelope dict
        self._fragment = fragment
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _recv_command(self, conn: socket.socket) -> dict:
        chunks = []
        while True:
            chunk = conn.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
            try:
                return json.loads(b"".join(chunks).decode())
            except json.JSONDecodeError:
                continue
        return {}

    def _serve(self) -> None:
        conn, _ = self._srv.accept()
        with conn:
            command = self._recv_command(conn)
            envelope = self._responder(command.get("type"), command.get("params", {}))
            payload = json.dumps(envelope).encode()
            if self._fragment and len(payload) > 4:
                mid = len(payload) // 2
                conn.sendall(payload[:mid])
                time.sleep(0.05)  # force the client to loop in _receive_full_response
                conn.sendall(payload[mid:])
            else:
                conn.sendall(payload)
        self._srv.close()


def test_execute_code_round_trip():
    server = MockBlenderServer(
        lambda t, p: {"status": "success", "result": {"executed": True, "result": ""}}
    )
    server.start()
    with BlenderConnection(host="127.0.0.1", port=server.port, timeout=5) as conn:
        result = conn.send_command("execute_code", {"code": "import bpy"})
    assert result == {"executed": True, "result": ""}


def test_fragmented_response_is_reassembled():
    """A reply split across two sends must still parse (no length framing)."""
    big = {"status": "success", "result": {"name": "Cube", "world_bounding_box": [[0, 0, 0]] * 8}}
    server = MockBlenderServer(lambda t, p: big, fragment=True)
    server.start()
    with BlenderConnection(host="127.0.0.1", port=server.port, timeout=5) as conn:
        result = conn.send_command("pick_object_at", {})
    assert result["name"] == "Cube"


def test_error_envelope_raises():
    server = MockBlenderServer(lambda t, p: {"status": "error", "message": "boom in bpy"})
    server.start()
    with BlenderConnection(host="127.0.0.1", port=server.port, timeout=5) as conn:
        with pytest.raises(BlenderError, match="boom in bpy"):
            conn.send_command("execute_code", {"code": "1/0"})


def test_connect_failure_returns_false():
    # Nothing listening on this port.
    conn = BlenderConnection(host="127.0.0.1", port=1, timeout=1)
    assert conn.connect() is False
    with pytest.raises(BlenderError):
        conn.send_command("get_scene_info")
