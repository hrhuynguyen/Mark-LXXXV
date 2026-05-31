"""Single-shot Blender-side socket server for the Step 1 round-trip (S1).

Runs INSIDE Blender's bundled Python via ``blender --background --python``. It is
a throwaway, minimal stand-in for the real addon server (Step 2): it listens once
on :9876, executes one command on Blender's main thread (so ``bpy`` is safe),
replies with the Seam-2 envelope, and exits. This lets us verify
``client.blender_bridge`` against genuine ``bpy`` without the GUI/timer machinery
the persistent addon needs.

Not for production — the real server lives in addon/forge_addon.py (Step 2).
"""

import io
import json
import socket
from contextlib import redirect_stdout

import bpy  # provided by Blender

PORT = 9876


def _recv_command(conn):
    chunks = []
    while True:
        chunk = conn.recv(8192)
        if not chunk:
            return None
        chunks.append(chunk)
        try:
            return json.loads(b"".join(chunks).decode())
        except json.JSONDecodeError:
            continue


def _handle(command):
    cmd = command.get("type")
    params = command.get("params", {})
    try:
        if cmd == "execute_code":
            ns = {"bpy": bpy}
            buf = io.StringIO()
            with redirect_stdout(buf):
                exec(params["code"], ns)  # noqa: S102 — intentional escape hatch
            return {"status": "success", "result": {"executed": True, "result": buf.getvalue()}}
        return {"status": "error", "message": f"spike server does not handle '{cmd}'"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"Code execution error: {exc}"}


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(1)
    # Sentinel the orchestrator waits for before connecting.
    print(f"FORGE_SPIKE_LISTENING {PORT}", flush=True)

    conn, _ = srv.accept()
    with conn:
        command = _recv_command(conn)
        if command is not None:
            conn.sendall(json.dumps(_handle(command)).encode())
    srv.close()


if __name__ == "__main__":
    main()
