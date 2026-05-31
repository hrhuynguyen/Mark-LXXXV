"""Socket bridge to the local Blender/Text2Blender addon."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from typing import Any

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876
DEFAULT_TIMEOUT = 180.0


def _env_port() -> int:
    value = os.getenv("BLENDER_PORT")
    if not value:
        return DEFAULT_PORT
    return int(value)


@dataclass
class BlenderConnection:
    """Small JSON-over-TCP client for BlenderMCP/Text2Blender's addon."""

    host: str = os.getenv("BLENDER_HOST", DEFAULT_HOST)
    port: int = _env_port()
    timeout: float = DEFAULT_TIMEOUT
    sock: socket.socket | None = None

    def connect(self) -> bool:
        if self.sock is not None:
            return True
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect((self.host, self.port))
            return True
        except Exception:
            self.sock = None
            return False

    def disconnect(self) -> None:
        if self.sock is None:
            return
        try:
            self.sock.close()
        finally:
            self.sock = None

    def send_command(self, command_type: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.sock is None and not self.connect():
            raise ConnectionError(
                f"Could not connect to Blender at {self.host}:{self.port}. "
                "Make sure the Blender addon socket is running."
            )
        assert self.sock is not None

        command = {"type": command_type, "params": params or {}}
        try:
            self.sock.sendall(json.dumps(command).encode("utf-8"))
            response = json.loads(self._receive_full_response().decode("utf-8"))
        except (ConnectionError, BrokenPipeError, ConnectionResetError, socket.timeout):
            self.disconnect()
            raise
        except json.JSONDecodeError as exc:
            self.disconnect()
            raise RuntimeError(f"Invalid JSON response from Blender: {exc}") from exc

        if response.get("status") == "error":
            raise RuntimeError(response.get("message", "Unknown Blender error"))
        return response.get("result", {})

    def _receive_full_response(self, buffer_size: int = 8192) -> bytes:
        assert self.sock is not None
        chunks: list[bytes] = []
        self.sock.settimeout(self.timeout)
        while True:
            chunk = self.sock.recv(buffer_size)
            if not chunk:
                if not chunks:
                    raise ConnectionError("Connection closed before any data received")
                break
            chunks.append(chunk)
            data = b"".join(chunks)
            try:
                json.loads(data.decode("utf-8"))
                return data
            except json.JSONDecodeError:
                continue

        data = b"".join(chunks)
        json.loads(data.decode("utf-8"))
        return data
