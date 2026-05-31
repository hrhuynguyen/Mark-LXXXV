"""Tests for blender_launcher.ensure_blender_running — attach-vs-spawn logic.
Uses a fake Forge socket server; never launches real Blender.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

from client.blender_launcher import ensure_blender_running


def _free_port() -> int:
    s = socket.socket()
    s.bind(("localhost", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeBlenderSocket:
    """Minimal stand-in for the addon server: replies to any command with a
    success envelope, so blender_launcher's get_scene_info probe succeeds."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("localhost", port))
        self._sock.listen(1)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.3)
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            with conn:
                try:
                    conn.recv(8192)
                    conn.sendall(json.dumps({"status": "success", "result": {"objects": []}}).encode())
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop = True
        self._thread.join(timeout=1.0)
        self._sock.close()


def test_attaches_when_socket_already_up():
    port = _free_port()
    fake = FakeBlenderSocket(port)
    fake.start()
    try:
        handle = ensure_blender_running(port=port, spawn=False)
        assert handle.attached is True
        assert handle.process is None
        assert handle.port == port
    finally:
        fake.stop()


def test_raises_when_no_socket_and_spawn_disabled():
    port = _free_port()
    with pytest.raises(RuntimeError):
        ensure_blender_running(port=port, spawn=False)


def test_raises_filenotfound_when_binary_missing():
    port = _free_port()  # nothing listening -> proceeds to spawn path
    with pytest.raises(FileNotFoundError):
        ensure_blender_running(port=port, spawn=True, blender_app="/no/such/Blender.app", timeout_s=1.0)
