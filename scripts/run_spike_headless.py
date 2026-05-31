"""Automated Step 1 (S1) round-trip against REAL Blender, no GUI.

Launches Blender headless running scripts/_spike_blender_server.py, waits for it
to listen, then drives it through client.blender_bridge — exactly the path the
client uses — and asserts a cube was created in a genuine bpy scene.

    python scripts/run_spike_headless.py

Pass: prints the bridge's result with "executed": true and a scene object list
that includes the new cube.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import threading

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

sys.path.insert(0, str(ROOT))
from client.blender_bridge import BlenderConnection  # noqa: E402

BLENDER = os.getenv("BLENDER_APP_PATH", "/Applications/Blender.app/Contents/MacOS/Blender")
SERVER_SCRIPT = ROOT / "scripts" / "_spike_blender_server.py"
SENTINEL = "FORGE_SPIKE_LISTENING"
STARTUP_TIMEOUT = 60.0


def main() -> int:
    if not pathlib.Path(BLENDER).exists():
        print(f"Blender not found at {BLENDER}; set BLENDER_APP_PATH.")
        return 1

    proc = subprocess.Popen(
        [BLENDER, "--background", "--factory-startup", "--python", str(SERVER_SCRIPT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    listening = threading.Event()

    def pump():
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                print(f"  [blender] {line}")
            if SENTINEL in line:
                listening.set()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    if not listening.wait(STARTUP_TIMEOUT):
        print("Blender never reported LISTENING; aborting.")
        proc.kill()
        return 1

    code = (
        "import bpy; "
        "before = len(bpy.data.objects); "
        "bpy.ops.mesh.primitive_cube_add(); "
        "print('objects:', sorted(o.name for o in bpy.data.objects)); "
        "print('added:', len(bpy.data.objects) - before)"
    )
    try:
        with BlenderConnection(host="127.0.0.1", port=9876, timeout=30) as conn:
            result = conn.send_command("execute_code", {"code": code})
        print("\nbridge result:", result)
        ok = result.get("executed") is True and "added: 1" in result.get("result", "")
        print("\nS1 PASS ✅" if ok else "\nS1 FAIL ❌")
        return 0 if ok else 1
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
