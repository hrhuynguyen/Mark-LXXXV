"""Adapter that delegates object generation to a local Text2Blender checkout."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_TEXT2BLENDER_PATH = Path("/Users/dothanhtam91/Desktop/PROJECT /Text2Blender")


class Text2BlenderUnavailable(RuntimeError):
    """Raised when the local Text2Blender checkout cannot be used."""


def text2blender_enabled(spec: dict[str, Any] | None = None) -> bool:
    if (os.getenv("FORGE_USE_TEXT2BLENDER") or "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False
    if spec and str(spec.get("generator") or "").lower() in {"procedural", "forge"}:
        return False
    return True


def resolve_text2blender_path() -> Path:
    return Path(os.getenv("TEXT2BLENDER_PATH", str(DEFAULT_TEXT2BLENDER_PATH))).expanduser()


def resolve_text2blender_command(path: Path) -> list[str]:
    custom_command = os.getenv("TEXT2BLENDER_COMMAND")
    if custom_command:
        command = shlex.split(custom_command)
        if not command:
            raise Text2BlenderUnavailable("TEXT2BLENDER_COMMAND is empty")
        return command

    cli = path / ".venv" / "bin" / "text2blender"
    if cli.exists():
        return [str(cli)]

    python = path / ".venv" / "bin" / "python"
    if python.exists():
        return [str(python), "-m", "text2blender.cli"]

    raise Text2BlenderUnavailable(
        "Text2Blender CLI not found. Expected "
        f"{cli} or {python}. Set TEXT2BLENDER_COMMAND to override."
    )


def build_text2blender_prompt(spec: dict[str, Any]) -> str:
    object_name = str(spec.get("object") or spec.get("name") or "object")
    summary = str(spec.get("summary") or "").strip()
    parts = spec.get("parts") if isinstance(spec.get("parts"), list) else []

    lines = [
        f"Create this object in the current Blender scene: {object_name}.",
    ]
    if summary:
        lines.append(f"Design summary: {summary}")

    if parts:
        lines.append("")
        lines.append("Important components to include:")
        for part in parts:
            if not isinstance(part, dict):
                continue
            name = str(part.get("name") or "part")
            generator = str(part.get("generator") or part.get("kind") or "component")
            params = part.get("params") or {}
            place = part.get("place") or part.get("placement") or ""
            details = [f"- {name}: {generator}"]
            if params:
                details.append(f"params={params}")
            if place:
                details.append(f"placement={place}")
            lines.append("; ".join(details))

    lines.extend(
        [
            "",
            "Use Text2Blender's Blender tools to create, import, or script the asset.",
            "Prefer a visually coherent result over many tiny procedural subparts.",
            "Name the main object or collection using the requested object name when practical.",
            "After creating it, verify the scene and give a concise summary.",
        ]
    )
    return "\n".join(lines)


async def execute_text2blender_build(spec: dict[str, Any]) -> dict[str, Any]:
    path = resolve_text2blender_path()
    prompt = build_text2blender_prompt(spec)
    command = resolve_text2blender_command(path)
    reply = await asyncio.to_thread(_run_text2blender_cli, path, command, prompt)
    return {
        "object": str(spec.get("object") or spec.get("name") or "object"),
        "summary": spec.get("summary"),
        "generator": "text2blender",
        "runner": "cli",
        "source_path": str(path),
        "command": command,
        "prompt": prompt,
        "reply": reply,
    }


def _run_text2blender_cli(path: Path, command: list[str], prompt: str) -> str:
    if not path.exists():
        raise Text2BlenderUnavailable(f"Text2Blender folder not found: {path}")

    args = [*command, "--prompt", prompt]
    if model := os.getenv("TEXT2BLENDER_MODEL"):
        args.extend(["--model", model])

    timeout = float(os.getenv("TEXT2BLENDER_TIMEOUT", "300"))
    try:
        completed = subprocess.run(
            args,
            cwd=str(path),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise Text2BlenderUnavailable(f"Text2Blender command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise Text2BlenderUnavailable(
            f"Text2Blender timed out after {timeout:g} seconds"
        ) from exc

    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    if completed.returncode != 0:
        detail = stderr or stdout or f"exit code {completed.returncode}"
        raise Text2BlenderUnavailable(f"Text2Blender CLI failed: {detail}")

    return stdout
