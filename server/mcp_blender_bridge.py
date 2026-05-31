"""Standalone MCP server for the Forge Blender bridge.

This is the BlenderMCP layer adapted to Forge: MCP tools are thin stubs that
forward to the existing Forge/Blender JSON socket (`addon/forge_addon.py`) via
`client.blender_bridge.BlenderConnection`.

The Gemini Live app still uses `server.server` + the macOS companion client.
This module exists for MCP clients and for testing the Blender bridge directly
with the same pattern documented in `blender-mcp.md`.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from client.blender_bridge import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    BlenderConnection,
)


_blender_connection: BlenderConnection | None = None


def get_blender_connection() -> BlenderConnection:
    """Return a persistent socket connection to the Forge Blender addon."""
    global _blender_connection
    if _blender_connection is not None:
        try:
            _blender_connection.send_command("get_scene_info")
            return _blender_connection
        except Exception:
            _blender_connection.disconnect()
            _blender_connection = None

    host = os.getenv("BLENDER_HOST", DEFAULT_HOST)
    port = int(os.getenv("BLENDER_PORT", str(DEFAULT_PORT)))
    conn = BlenderConnection(host=host, port=port)
    if not conn.connect():
        raise ConnectionError(
            f"Could not connect to Blender at {host}:{port}. "
            "Start Blender with the Forge addon enabled and connected."
        )
    _blender_connection = conn
    return conn


def close_blender_connection() -> None:
    """Close the persistent socket connection, if one exists."""
    global _blender_connection
    if _blender_connection is not None:
        _blender_connection.disconnect()
        _blender_connection = None


def send_blender_command(command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Forward one command to Blender using the BlenderMCP JSON socket contract."""
    return get_blender_connection().send_command(command, params or {})


def json_result(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def build_mcp_server():
    """Create the FastMCP server lazily so normal Forge imports do not require MCP."""
    try:
        from mcp.server.fastmcp import FastMCP, Image
    except Exception as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "The MCP extra is required for this entrypoint. Install with: "
            'uv pip install -e ".[mcp]"'
        ) from exc

    mcp = FastMCP("ForgeBlenderMCP")

    @mcp.tool()
    def get_scene_info() -> str:
        """Get a compact summary of the current Blender scene."""
        return json_result(send_blender_command("get_scene_info"))

    @mcp.tool()
    def get_object_info(name: str) -> str:
        """Get transform and bounding-box information for a named Blender object."""
        return json_result(send_blender_command("get_object_info", {"name": name}))

    @mcp.tool()
    def execute_blender_code(code: str) -> str:
        """Execute Python code inside Blender through the Forge addon."""
        result = send_blender_command("execute_code", {"code": code})
        stdout = result.get("result", "")
        return f"Code executed successfully.\n{stdout}".rstrip()

    @mcp.tool()
    def pick_object_at(region_x: int, region_y: int, radius: float = 80.0) -> str:
        """Raycast-pick the object at a Blender VIEW_3D region pixel."""
        result = send_blender_command(
            "pick_object_at",
            {"region_x": int(region_x), "region_y": int(region_y), "radius": float(radius)},
        )
        return json_result(result)

    @mcp.tool()
    def get_view_geometry() -> str:
        """Return the active Blender VIEW_3D region geometry."""
        return json_result(send_blender_command("get_view_geometry"))

    @mcp.tool()
    def frame_object(name: str) -> str:
        """Frame a named object in the active Blender viewport."""
        return json_result(send_blender_command("frame_object", {"name": name}))

    @mcp.tool()
    def get_viewport_screenshot(max_size: int = 800):
        """Capture the active Blender viewport as an MCP image."""
        path = Path(tempfile.gettempdir()) / f"forge_mcp_viewport_{os.getpid()}.png"
        try:
            send_blender_command(
                "get_viewport_screenshot",
                {"max_size": int(max_size), "filepath": str(path), "format": "png"},
            )
            data = path.read_bytes()
            return Image(data=data, format="png")
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    @mcp.prompt()
    def asset_creation_strategy() -> str:
        """Adapted from BlenderMCP for Forge's procedural-first workflow."""
        return (
            "Use Blender tools directly. First inspect the scene with get_scene_info. "
            "For structural objects, prefer procedural bpy code through execute_blender_code "
            "so each meaningful part is a separate named object. Verify important objects "
            "with get_object_info and world_bounding_box. Use screenshots only when visual "
            "confirmation is needed. Keep generated parts named and editable for pointing "
            "and registry-driven edits."
        )

    return mcp


def main() -> None:
    mcp = build_mcp_server()
    try:
        mcp.run()
    finally:
        close_blender_connection()


if __name__ == "__main__":
    main()
