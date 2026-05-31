from __future__ import annotations

import base64
import threading
from pathlib import Path

from scripts import run_demo_ui


ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "client" / "demo_ui"


def test_demo_ui_contains_camera_hand_and_terminal_surfaces():
    html = (UI_ROOT / "index.html").read_text()
    js = (UI_ROOT / "app.js").read_text()
    css = (UI_ROOT / "styles.css").read_text()

    assert 'id="cameraVideo"' in html
    assert 'id="handCanvas"' in html
    assert 'id="terminalOutput"' in html
    assert 'id="blenderFrame"' in html
    assert 'id="voiceButton"' in html
    assert "getUserMedia" in js
    assert "HandLandmarker" in js
    assert "/api/blender/screenshot" in js
    assert "/api/command" in js
    assert "SpeechRecognition" in js
    assert 'runTerminalCommand(transcript, "voice")' in js
    assert ".camera-panel" in css
    assert ".terminal-panel" in css
    assert ".blender-frame" in css


def test_demo_ui_server_only_serves_allowed_roots():
    ui_file = run_demo_ui._safe_join(run_demo_ui.UI_ROOT, "index.html")
    model_file = run_demo_ui._safe_join(run_demo_ui.MODEL_ROOT, "hand_landmarker.task")
    escaped = run_demo_ui._safe_join(run_demo_ui.UI_ROOT, "../../.env")

    assert ui_file == run_demo_ui.UI_ROOT / "index.html"
    assert model_file == run_demo_ui.MODEL_ROOT / "hand_landmarker.task"
    assert escaped is None


def test_ui_command_mapping_supports_terminal_and_voice_phrases():
    assert run_demo_ui.coerce_ui_command("Hey Travis, build a rocket") == "build a rocket"
    assert run_demo_ui.coerce_ui_command("/info") == "/info"
    assert run_demo_ui.coerce_ui_command("Hey Travis replace this part with carbon leg") == (
        "/replace carbon leg"
    )
    assert run_demo_ui.coerce_ui_command("Hey Travis add a small antenna to this part") == (
        "/add-part small antenna to this part"
    )
    assert run_demo_ui.coerce_ui_command("what is this") == "/info"


def test_demo_command_runtime_skips_prompt_improvement_for_voice(monkeypatch):
    runtime = object.__new__(run_demo_ui.DemoCommandRuntime)
    runtime.args = run_demo_ui.harness.build_arg_parser().parse_args(
        ["--no-gestures", "--no-auto-voice-input", "--no-reference-popup", "--typed-first"]
    )
    runtime.args.no_auto_info = True
    runtime.busy_event = threading.Event()
    runtime.settings = run_demo_ui.harness.RuntimeSettings(
        speak_info=True,
        auto_info=False,
        auto_voice_input=False,
        selection_hold_seconds=3.0,
    )
    runtime.reference_state = run_demo_ui.harness.ReferenceWorkflowState()
    runtime.info_agent = None
    runtime.lock = threading.Lock()
    seen: list[dict] = []

    def fake_execute(prompt, **kwargs):
        seen.append({"prompt": prompt, "improve_prompt": kwargs["improve_prompt"]})
        return 0

    monkeypatch.setattr(run_demo_ui.harness, "_execute_user_input", fake_execute)

    voice_result = runtime.run("Hey Travis, build a rocket", source="voice")
    typed_result = runtime.run("Hey Travis, build a rocket", source="typed")

    assert voice_result["source"] == "voice"
    assert typed_result["source"] == "typed"
    assert seen == [
        {"prompt": "build a rocket", "improve_prompt": False},
        {"prompt": "build a rocket", "improve_prompt": True},
    ]


def test_capture_blender_viewport_normalizes_socket_screenshot(monkeypatch):
    sample_png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"fake" * 24).decode("ascii")

    class FakeConnection:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def send_command(self, command_type: str, params: dict | None = None) -> dict:
            assert command_type == "get_viewport_screenshot"
            assert params == {"max_size": run_demo_ui.DEFAULT_SCREENSHOT_MAX_SIZE}
            return {
                "data": sample_png,
                "mime_type": "image/png",
                "width": 640,
                "height": 360,
            }

        def disconnect(self) -> None:
            return None

    monkeypatch.setattr(run_demo_ui, "BlenderConnection", FakeConnection)

    payload = run_demo_ui.capture_blender_viewport()

    assert payload["ok"] is True
    assert payload["source"] == "get_viewport_screenshot"
    assert payload["data_url"] == f"data:image/png;base64,{sample_png}"
    assert payload["width"] == 640
    assert payload["height"] == 360


def test_capture_blender_viewport_falls_back_to_execute_code(monkeypatch):
    sample_png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"fallback" * 16).decode("ascii")
    calls: list[str] = []

    class FakeConnection:
        def __init__(self, timeout: float) -> None:
            return None

        def send_command(self, command_type: str, params: dict | None = None) -> dict:
            calls.append(command_type)
            if command_type == "get_viewport_screenshot":
                raise RuntimeError("unsupported")
            assert command_type == "execute_code"
            assert "screenshot_area" in params["code"]
            return {"result": f'noise\n{{"ok": true, "data": "{sample_png}"}}\n'}

        def disconnect(self) -> None:
            return None

    monkeypatch.setattr(run_demo_ui, "BlenderConnection", FakeConnection)

    payload = run_demo_ui.capture_blender_viewport(max_size=800)

    assert calls == ["get_viewport_screenshot", "execute_code"]
    assert payload["ok"] is True
    assert payload["source"] == "execute_code"
    assert payload["data_url"] == f"data:image/png;base64,{sample_png}"
