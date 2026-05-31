"""client.blender_bridge

The client→Blender socket bridge (Forge Seam 2, see CONTRACTS.md §3).

Lifted from BlenderMCP's ``BlenderConnection`` (``send_command`` /
``receive_full_response``) and adapted to Forge conventions:

* protocol is JSON over a raw TCP socket on ``localhost:9876`` with **no length
  framing** — the receiver reads chunks until the accumulated bytes parse as one
  complete JSON object (the addon sends exactly one response per command);
* request envelope  ``{"type": <command>, "params": {...}}``;
* response envelope ``{"status": "success", "result": {...}}`` on success or
  ``{"status": "error", "message": "..."}`` on failure;
* ``send_command`` returns the ``result`` dict and raises :class:`BlenderError`
  on an error envelope or a transport failure;
* the 180 s socket timeout matches the addon (long enough for generation/import).

This module is stdlib-only on purpose: it runs in the client venv, *not* inside
Blender's bundled Python. Step 1 (plan.md). The Forge-specific commands
(``pick_object_at``, ``set_part_transform``, …) ride this same transport and are
added addon-side in Step 2.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any

logger = logging.getLogger("forge.blender_bridge")

DEFAULT_HOST = os.getenv("BLENDER_HOST", "localhost")
DEFAULT_PORT = int(os.getenv("BLENDER_PORT", "9876"))
DEFAULT_TIMEOUT = 180.0  # CONTRACTS.md §7 — matches the addon's socket timeout.
RECV_BUFFER = 8192


class BlenderError(RuntimeError):
    """Raised when Blender returns an error envelope or the transport fails."""


class BlenderConnection:
    """A single-socket client to the Forge Blender addon (localhost:9876).

    One command in flight at a time — mirrors the addon's single-client server.
    Usable directly or as a context manager::

        with BlenderConnection() as conn:
            conn.send_command("execute_code", {"code": "import bpy; ..."})
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None

    # ── lifecycle ───────────────────────────────────────────────────────
    def connect(self) -> bool:
        """Open the socket. Returns True if connected (idempotent)."""
        if self.sock:
            return True
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.info("Connected to Blender at %s:%s", self.host, self.port)
            return True
        except OSError as exc:
            logger.error("Failed to connect to Blender: %s", exc)
            self.sock = None
            return False

    def disconnect(self) -> None:
        """Close the socket if open."""
        if self.sock:
            try:
                self.sock.close()
            except OSError as exc:
                logger.error("Error disconnecting from Blender: %s", exc)
            finally:
                self.sock = None

    def __enter__(self) -> BlenderConnection:
        if not self.connect():
            raise BlenderError(
                f"Could not connect to Blender at {self.host}:{self.port} "
                "(is the addon's socket server running?)"
            )
        return self

    def __exit__(self, *_exc: object) -> None:
        self.disconnect()

    # ── transport ───────────────────────────────────────────────────────
    def _receive_full_response(self, sock: socket.socket) -> bytes:
        """Read chunks until the accumulated bytes parse as one JSON object.

        There is no length prefix, so completeness is detected by a successful
        ``json.loads``. An empty chunk means the peer closed the connection.
        """
        sock.settimeout(self.timeout)
        chunks: list[bytes] = []
        try:
            while True:
                chunk = sock.recv(RECV_BUFFER)
                if not chunk:
                    if not chunks:
                        raise BlenderError("Connection closed before any data was received")
                    break
                chunks.append(chunk)
                try:
                    data = b"".join(chunks)
                    json.loads(data.decode("utf-8"))
                    logger.debug("Received complete response (%d bytes)", len(data))
                    return data
                except json.JSONDecodeError:
                    continue  # incomplete JSON — keep reading
        except socket.timeout as exc:
            raise BlenderError("Timeout while receiving response from Blender") from exc
        except (ConnectionError, OSError) as exc:
            raise BlenderError(f"Socket error while receiving from Blender: {exc}") from exc

        # Peer closed mid-stream: use what we have if it happens to be valid.
        data = b"".join(chunks)
        try:
            json.loads(data.decode("utf-8"))
            return data
        except json.JSONDecodeError as exc:
            raise BlenderError("Incomplete JSON response received from Blender") from exc

    def send_command(self, command_type: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one command and return its ``result`` dict.

        Raises :class:`BlenderError` on an error envelope or any transport
        failure. The socket is invalidated on failure so the next call reconnects.
        """
        if not self.sock and not self.connect():
            raise BlenderError(f"Not connected to Blender at {self.host}:{self.port}")
        assert self.sock is not None  # for type-checkers

        command = {"type": command_type, "params": params or {}}
        try:
            logger.info("→ %s %s", command_type, params or {})
            self.sock.settimeout(self.timeout)
            self.sock.sendall(json.dumps(command).encode("utf-8"))
            response_data = self._receive_full_response(self.sock)
        except BlenderError:
            self.sock = None
            raise
        except (socket.timeout, ConnectionError, OSError) as exc:
            self.sock = None
            raise BlenderError(f"Communication error with Blender: {exc}") from exc

        try:
            response = json.loads(response_data.decode("utf-8"))
        except json.JSONDecodeError as exc:
            self.sock = None
            raise BlenderError(f"Invalid JSON response from Blender: {exc}") from exc

        if response.get("status") == "error":
            message = response.get("message", "Unknown error from Blender")
            logger.error("Blender error: %s", message)
            raise BlenderError(message)

        return response.get("result", {})
