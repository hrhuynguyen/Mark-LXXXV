"""Integration test: load addon/forge_addon.py in real Blender (headless) and run
scripts/_check_addon.py, which registers the addon and exercises every Step 2
handler incl. the viewport raycast. Skipped if Blender isn't installed.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BLENDER = os.getenv("BLENDER_APP_PATH", "/Applications/Blender.app/Contents/MacOS/Blender")
CHECK = ROOT / "scripts" / "_check_addon.py"


@pytest.mark.skipif(not pathlib.Path(BLENDER).exists(), reason="Blender not installed")
def test_addon_registers_and_handlers_work():
    proc = subprocess.run(
        [BLENDER, "--background", "--factory-startup", "--python", str(CHECK)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert "FORGE_ADDON_CHECK PASS" in out, out[-2000:]
