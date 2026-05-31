"""client.blender_launcher

Launch (or attach to) Blender with the Forge addon and its socket server already
running — so the companion app comes up fully connected with no manual "Connect"
click. Step 4.

Behaviour:
  * If the Forge socket already answers on the port, **attach** (the user already
    has Blender open with the server running) and leave it alone.
  * Otherwise **spawn** Blender from the .app bundle with ``FORGE_AUTOSTART=1`` +
    ``FORGE_PORT`` in the environment; the addon's ``register()`` reads those and
    starts the socket server on a deferred timer. Poll until the socket answers.

Requires the Forge addon to be installed and **enabled** in this Blender
(register runs on launch only for enabled add-ons). On quit we deliberately do
NOT kill Blender — the user keeps their session.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from client.blender_bridge import BlenderConnection

DEFAULT_BLENDER_APP = os.getenv("FORGE_BLENDER_APP", "/Applications/Blender.app")


@dataclass
class BlenderHandle:
    """Result of ensure_blender_running. ``attached`` is True when we connected
    to an already-running Blender (``process`` is then None)."""

    process: Optional[subprocess.Popen]
    attached: bool
    port: int


def _socket_responds(host: str, port: int, timeout: float = 1.0) -> bool:
    conn = BlenderConnection(host=host, port=port, timeout=timeout)
    try:
        conn.send_command("get_scene_info")  # cheap handler that always exists
        return True
    except Exception:
        return False
    finally:
        conn.disconnect()


def blender_binary(blender_app: str) -> Path:
    return Path(blender_app) / "Contents" / "MacOS" / "Blender"


def ensure_blender_running(
    *,
    port: int = 9876,
    host: str = "localhost",
    blender_app: str = DEFAULT_BLENDER_APP,
    timeout_s: float = 30.0,
    poll_dt_s: float = 0.5,
    spawn: bool = True,
) -> BlenderHandle:
    """Attach to a running Forge socket, or spawn Blender and wait for it.

    Raises FileNotFoundError if the Blender binary is missing, TimeoutError if
    the socket never comes up, RuntimeError if Blender exits early.
    """
    if _socket_responds(host, port):
        return BlenderHandle(process=None, attached=True, port=port)

    if not spawn:
        raise RuntimeError(f"No Forge socket on {host}:{port} and spawn=False")

    binary = blender_binary(blender_app)
    if not binary.exists():
        raise FileNotFoundError(
            f"Blender executable not found at {binary}. "
            "Install Blender or set FORGE_BLENDER_APP / pass blender_app=..."
        )

    env = dict(os.environ)
    env["FORGE_AUTOSTART"] = "1"
    env["FORGE_PORT"] = str(port)
    process = subprocess.Popen([str(binary)], env=env)

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _socket_responds(host, port):
            return BlenderHandle(process=process, attached=False, port=port)
        if process.poll() is not None:
            raise RuntimeError(
                f"Blender exited (code {process.returncode}) before the Forge socket came up. "
                "Is the Forge addon installed and enabled?"
            )
        time.sleep(poll_dt_s)

    raise TimeoutError(
        f"Forge socket did not come up on {host}:{port} within {timeout_s:.0f}s "
        "(is the Forge addon enabled, with auto-start honoured?)"
    )
