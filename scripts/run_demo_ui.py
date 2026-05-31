"""Serve the Forge demo UI without exposing the whole repository."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import socket
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.blender_bridge import BlenderConnection
from server.tools.blender_selection import _parse_json_from_stdout
from scripts import run_prompt_gesture_test as harness

UI_ROOT = ROOT / "client" / "demo_ui"
MODEL_ROOT = ROOT / "client" / "models"
DEFAULT_SCREENSHOT_MAX_SIZE = 1280
DEFAULT_WAKE_PHRASE = "Hey Travis"
COMMAND_RUNTIME: "DemoCommandRuntime | None" = None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the Forge demo UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


class DemoUIHandler(BaseHTTPRequestHandler):
    server_version = "ForgeDemoUI/1.0"

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/api/blender/screenshot":
            self._send_json(capture_blender_viewport())
            return

        if path in {"", "/"}:
            path = "/index.html"

        if path.startswith("/models/"):
            file_path = _safe_join(MODEL_ROOT, path.removeprefix("/models/"))
        else:
            file_path = _safe_join(UI_ROOT, path.removeprefix("/"))

        if file_path is None or not file_path.is_file():
            self.send_error(404)
            return

        content = file_path.read_bytes()
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path != "/api/command":
            self.send_error(404)
            return

        try:
            payload = self._read_json_payload(max_bytes=64_000)
            command = str(payload.get("command") or "")
            source = _normalize_command_source(payload.get("source"))
        except Exception as exc:
            self._send_json({"ok": False, "error": f"Invalid command request: {exc}"}, status=400)
            return

        runtime = get_command_runtime()
        result = runtime.run(command, source=source)
        self._send_json(result)

    def _read_json_payload(self, *, max_bytes: int) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > max_bytes:
            raise ValueError("request too large")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected JSON object")
        return value

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        content = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


class DemoCommandRuntime:
    """Small adapter that lets the browser terminal use the working CLI command path."""

    def __init__(self) -> None:
        load_dotenv(ROOT / ".env")
        self.args = harness.build_arg_parser().parse_args(
            [
                "--no-gestures",
                "--no-auto-voice-input",
                "--no-reference-popup",
                "--typed-first",
            ]
        )
        self.args.no_auto_info = True
        self.busy_event = threading.Event()
        self.settings = harness.RuntimeSettings(
            speak_info=bool(self.args.speak_info),
            auto_info=False,
            auto_voice_input=False,
            selection_hold_seconds=max(0.0, float(self.args.selection_hold_seconds)),
        )
        self.reference_state = harness.ReferenceWorkflowState()
        self.info_agent = self._create_info_agent()
        self.lock = threading.Lock()

    def run(self, raw_command: str, *, source: str = "typed") -> dict[str, Any]:
        source = _normalize_command_source(source)
        original = raw_command.strip()
        mapped = coerce_ui_command(original, wake_phrase=str(self.args.wake_phrase))
        if not mapped:
            return {
                "ok": True,
                "command": original,
                "mapped_command": "",
                "source": source,
                "exit_code": 0,
                "output": "No command entered.",
            }

        output = StringIO()
        with self.lock, redirect_stdout(output), redirect_stderr(output):
            try:
                if mapped in {"/quit", "/exit", "/q"}:
                    code = harness.EXIT_REQUESTED
                    print("Use the browser tab controls to close the UI.")
                else:
                    force_adk = harness._voice_uses_adk(self.args)
                    code = harness._execute_user_input(
                        mapped,
                        args=self.args,
                        busy_event=self.busy_event,
                        settings=self.settings,
                        info_agent=self.info_agent,
                        reference_state=self.reference_state,
                        force_adk=force_adk,
                        improve_prompt=source != "voice",
                    )
            except Exception as exc:
                code = 1
                print(f"Command failed: {exc}")

        return {
            "ok": code in {0, harness.EXIT_REQUESTED},
            "command": original,
            "mapped_command": mapped,
            "source": source,
            "exit_code": int(code),
            "output": output.getvalue().strip(),
        }

    def _create_info_agent(self):
        try:
            return harness.GeminiLivePartInfoAgent(model=self.args.part_info_model)
        except Exception:
            return None


def get_command_runtime() -> DemoCommandRuntime:
    global COMMAND_RUNTIME
    if COMMAND_RUNTIME is None:
        COMMAND_RUNTIME = DemoCommandRuntime()
    return COMMAND_RUNTIME


def _normalize_command_source(value: Any) -> str:
    return "voice" if str(value or "").strip().lower() == "voice" else "typed"


def coerce_ui_command(raw_command: str, *, wake_phrase: str = DEFAULT_WAKE_PHRASE) -> str:
    """Map browser terminal text or speech into the same commands as the CLI harness."""

    command = " ".join(str(raw_command or "").strip().split())
    if not command:
        return ""

    command = _strip_wake_phrase(command, wake_phrase=wake_phrase)
    lower = command.lower()
    if lower.startswith("/"):
        return command

    if lower in {"quit", "exit", "stop program", "stop the program"}:
        return "/quit"
    if any(phrase in lower for phrase in ("what is this", "what's this", "what is selected")):
        return "/info"
    if "reference" in lower or "image" in lower:
        if "object" in lower or "whole" in lower:
            return "/refs object"
        return "/refs part"
    if lower.startswith("undo") or "go back" in lower:
        if "add" in lower:
            return "/undo-add"
        if "replace" in lower:
            return "/undo-replace"
        return "/undo-ref"
    if "stop speaking" in lower or "quiet mode" in lower:
        return "/speak off"
    if "speak" in lower and ("again" in lower or "out loud" in lower):
        return "/speak on"
    if _looks_like_part_addition(lower):
        return f"/add-part {_clean_part_request(command)}"
    if _looks_like_part_replacement(lower):
        return f"/replace {_clean_part_request(command)}"
    return command


def _strip_wake_phrase(command: str, *, wake_phrase: str) -> str:
    prefix = str(wake_phrase or "").strip().lower()
    if not prefix:
        return command
    lower = command.lower()
    if lower.startswith(prefix):
        return command[len(prefix) :].lstrip(" ,.:;-")
    return command


def _looks_like_part_addition(lower: str) -> bool:
    return (
        lower.startswith(("add ", "attach ", "add on "))
        or " add " in f" {lower} "
        or "attach" in lower
        or "add-on" in lower
        or "add on" in lower
    ) and "replace" not in lower


def _looks_like_part_replacement(lower: str) -> bool:
    return any(word in lower for word in ("replace", "swap", "regenerate", "remake"))


def _clean_part_request(command: str) -> str:
    cleaned = command.strip()
    replacements = [
        "replace this part with",
        "replace selected part with",
        "replace it with",
        "replace with",
        "swap this part with",
        "swap it with",
        "add a",
        "add an",
        "add the",
        "add",
        "attach a",
        "attach an",
        "attach the",
        "attach",
        "add on",
    ]
    lowered = cleaned.lower()
    for phrase in replacements:
        if lowered.startswith(phrase):
            cleaned = cleaned[len(phrase) :].strip(" ,.:;-")
            break
    return cleaned or command.strip()


def capture_blender_viewport(max_size: int = DEFAULT_SCREENSHOT_MAX_SIZE) -> dict[str, Any]:
    """Return the current Blender VIEW_3D screenshot as a browser data URL."""

    conn = BlenderConnection(timeout=12.0)
    try:
        screenshot_error = ""
        try:
            result = conn.send_command("get_viewport_screenshot", {"max_size": max_size})
            normalized = _normalize_screenshot_payload(result, source="get_viewport_screenshot")
            if normalized.get("ok"):
                return normalized
            screenshot_error = str(normalized.get("error") or "No image data.")
        except Exception as exc:
            screenshot_error = str(exc)

        try:
            result = conn.send_command(
                "execute_code",
                {"code": _build_viewport_screenshot_code(max_size=max_size)},
            )
            payload = _parse_json_from_stdout(str(result.get("result", "")))
            return _normalize_screenshot_payload(payload, source="execute_code")
        except Exception as fallback_exc:
            return {
                "ok": False,
                "error": (
                    "Could not capture Blender viewport. Make sure Blender is open, "
                    "the Text2Blender/BlenderMCP socket is running, and a 3D View is visible."
                ),
                "detail": f"{screenshot_error}; fallback: {fallback_exc}",
            }
    finally:
        conn.disconnect()


def _normalize_screenshot_payload(payload: Any, *, source: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"ok": False, "error": f"Unexpected screenshot response from {source}."}

    if payload.get("ok") is False:
        return {"ok": False, "error": str(payload.get("error") or "Screenshot failed.")}

    if data_url := _find_data_url(payload):
        return {
            "ok": True,
            "source": source,
            "data_url": data_url,
            "width": payload.get("width"),
            "height": payload.get("height"),
        }

    if encoded := _find_base64_image(payload):
        mime_type = str(
            payload.get("mime_type")
            or payload.get("mimeType")
            or payload.get("format")
            or "image/png"
        )
        if "/" not in mime_type:
            mime_type = f"image/{mime_type.lower()}"
        return {
            "ok": True,
            "source": source,
            "data_url": f"data:{mime_type};base64,{encoded}",
            "width": payload.get("width"),
            "height": payload.get("height"),
        }

    if image_path := _find_safe_image_path(payload):
        data = image_path.read_bytes()
        mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
        return {
            "ok": True,
            "source": source,
            "data_url": f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}",
            "width": payload.get("width"),
            "height": payload.get("height"),
        }

    return {"ok": False, "error": f"No image data found in screenshot response from {source}."}


def _find_data_url(payload: dict[str, Any]) -> str | None:
    for value in _walk_values(payload):
        if isinstance(value, str) and value.startswith("data:image/"):
            return value
    return None


def _find_base64_image(payload: dict[str, Any]) -> str | None:
    for key in ("data", "image", "screenshot", "base64", "bytes"):
        value = payload.get(key)
        if isinstance(value, str) and _looks_like_base64_image(value):
            return value
    for value in _walk_values(payload):
        if isinstance(value, str) and _looks_like_base64_image(value):
            return value
    return None


def _looks_like_base64_image(value: str) -> bool:
    compact = value.strip()
    if len(compact) < 64 or any(char.isspace() for char in compact):
        return False
    try:
        raw = base64.b64decode(compact, validate=True)
    except Exception:
        return False
    return raw.startswith(b"\x89PNG") or raw.startswith(b"\xff\xd8\xff")


def _find_safe_image_path(payload: dict[str, Any]) -> Path | None:
    for key in ("path", "filepath", "file", "screenshot_path", "image_path"):
        value = payload.get(key)
        if isinstance(value, str):
            path = Path(value).expanduser().resolve()
            if _is_safe_temp_image(path):
                return path
    return None


def _is_safe_temp_image(path: Path) -> bool:
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg"} or not path.is_file():
        return False
    allowed_roots = [
        Path("/tmp").resolve(),
        Path("/private/tmp").resolve(),
        Path("/var/folders").resolve(),
        Path("/private/var/folders").resolve(),
    ]
    return any(path == root or root in path.parents for root in allowed_roots)


def _walk_values(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values(child)
    else:
        yield value


def _build_viewport_screenshot_code(*, max_size: int) -> str:
    return _VIEWPORT_SCREENSHOT_CODE.format(max_size=int(max(320, max_size)))


def _safe_join(root: Path, relative: str) -> Path | None:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _find_available_port(host: str, start: int) -> int:
    if int(start) <= 0:
        return 0
    for port in range(int(start), int(start) + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    port = _find_available_port(args.host, args.port)
    server = ThreadingHTTPServer((args.host, port), DemoUIHandler)
    url = f"http://{args.host}:{server.server_port}/"
    print(f"Forge demo UI: {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Forge demo UI.")
    finally:
        server.server_close()
    return 0


_VIEWPORT_SCREENSHOT_CODE = r"""
import base64
import bpy
import json
import os
import tempfile

max_size = {max_size}
filepath = os.path.join(tempfile.gettempdir(), f"forge_viewport_{{os.getpid()}}.png")
area = next((item for item in bpy.context.screen.areas if item.type == "VIEW_3D"), None)
if area is None:
    print(json.dumps({{"ok": False, "error": "No visible VIEW_3D area found."}}))
else:
    try:
        with bpy.context.temp_override(area=area):
            bpy.ops.screen.screenshot_area(filepath=filepath)
        image = bpy.data.images.load(filepath)
        try:
            width, height = int(image.size[0]), int(image.size[1])
            largest = max(width, height)
            if largest > max_size:
                scale = max_size / largest
                width = max(1, int(width * scale))
                height = max(1, int(height * scale))
                image.scale(width, height)
                image.file_format = "PNG"
                image.save()
        finally:
            bpy.data.images.remove(image)

        with open(filepath, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
        print(json.dumps({{
            "ok": True,
            "mime_type": "image/png",
            "data": encoded,
            "width": width,
            "height": height,
        }}))
    except Exception as exc:
        print(json.dumps({{"ok": False, "error": str(exc)}}))
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass
"""


if __name__ == "__main__":
    raise SystemExit(main())
