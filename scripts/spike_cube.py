"""
Spike S1 (plan.md Step 1): client -> Blender socket round-trip.

Prereq: Blender is open with the Forge addon installed, and you've clicked
"Connect" in the Forge sidebar panel (starts the socket server on :9876).

Run:  python scripts/spike_cube.py
Pass: a cube appears in the live Blender window and this prints {"executed": true, ...}.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from client.blender_bridge import BlenderConnection


def main() -> None:
    conn = BlenderConnection(host="localhost", port=9876)
    if not conn.connect():
        raise SystemExit("Could not connect to Blender. Is the addon server running?")
    result = conn.send_command(
        "execute_code",
        {"code": "import bpy; bpy.ops.mesh.primitive_cube_add()"},
    )
    print(result)
    conn.disconnect()


if __name__ == "__main__":
    main()
