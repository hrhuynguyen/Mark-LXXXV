"""Interactive Text2Blender prompt test with hand gesture mouse control.

This bypasses the unfinished full Forge server/client stack. It is meant for
live testing the two working pieces together:

* type a prompt, then Text2Blender creates the object in Blender;
* keep the webcam hand mouse running so gestures can move/navigate Blender.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXIT_REQUESTED = -1

from client.blender_bridge import BlenderConnection
from client.cursor.webcam_tracker import WebcamFingerTracker
from client.gestures.hand_mouse import (
    HandMouseController,
    HandMouseCursorProvider,
    PynputMouseBackend,
)
from client.reference_picker import ReferencePickerUnavailable, choose_reference_image
from client.voice_input import (
    VoiceInputUnavailable,
    improve_build_prompt,
    record_microphone_until_silence,
    record_microphone_wav,
    transcribe_voice_command,
)
from server.agents.part_info_agent import GeminiLivePartInfoAgent, wait_for_speech_to_finish
from server.build.text2blender_adapter import (
    Text2BlenderUnavailable,
    _run_text2blender_cli,
    resolve_text2blender_command,
    resolve_text2blender_path,
)
from server.tools.blender_selection import get_selected_object_snapshot
from server.tools.reference_images import (
    ReferenceImage,
    ReferenceImageSearchUnavailable,
    build_reference_query,
    describe_reference_image,
    search_reference_images,
)
from server.tools.reference_refine import (
    build_part_addition_prompt,
    build_part_replacement_prompt,
    build_reference_refine_prompt,
    create_restore_point,
    prepare_part_replacement,
    restore_from_restore_point,
)


@dataclass
class RuntimeSettings:
    speak_info: bool = True
    auto_info: bool = True
    auto_voice_input: bool = True
    selection_hold_seconds: float = 3.0
    adk_prompts: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> tuple[bool, bool, float, bool]:
        with self._lock:
            return (
                self.speak_info,
                self.auto_info,
                max(0.0, self.selection_hold_seconds),
                self.adk_prompts,
            )

    def set_speak(self, enabled: bool) -> None:
        with self._lock:
            self.speak_info = bool(enabled)

    def set_auto_info(self, enabled: bool) -> None:
        with self._lock:
            self.auto_info = bool(enabled)

    def set_auto_voice_input(self, enabled: bool) -> None:
        with self._lock:
            self.auto_voice_input = bool(enabled)

    def auto_voice_enabled(self) -> bool:
        with self._lock:
            return bool(self.auto_voice_input)

    def set_hold_seconds(self, seconds: float) -> None:
        with self._lock:
            self.selection_hold_seconds = max(0.0, float(seconds))

    def set_adk_prompts(self, enabled: bool) -> None:
        with self._lock:
            self.adk_prompts = bool(enabled)


@dataclass
class ReferenceWorkflowState:
    references: list[ReferenceImage] = field(default_factory=list)
    scope: str = "part"
    query: str = ""
    snapshot: dict[str, Any] | None = None
    restore_point: dict[str, Any] | None = None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Type Text2Blender prompts while hand gestures control the mouse."
    )
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--no-gestures", action="store_true")
    parser.add_argument("--gesture-debug", action="store_true")
    parser.add_argument("--gesture-touch-threshold", type=float, default=0.19)
    parser.add_argument("--gesture-hold-frames", type=int, default=2)
    parser.add_argument("--gesture-smoothing", type=float, default=0.35)
    parser.add_argument("--gesture-inner-area", type=float, default=0.70)
    parser.add_argument(
        "--prompt",
        help="Run one prompt and exit. Without this flag, prompts are read interactively.",
    )
    parser.add_argument(
        "--no-auto-info",
        action="store_true",
        help="Do not ask Gemini Live when Blender's active selected object changes.",
    )
    parser.add_argument(
        "--part-info-model",
        default=None,
        help="Gemini Live model for selected-part explanations.",
    )
    parser.add_argument("--info-poll-seconds", type=float, default=1.0)
    parser.add_argument(
        "--selection-hold-seconds",
        type=float,
        default=3.0,
        help="Explain a selected object only after it remains selected this long.",
    )
    voice_group = parser.add_mutually_exclusive_group()
    voice_group.add_argument(
        "--speak-info",
        dest="speak_info",
        action="store_true",
        default=True,
        help="Speak selected-part explanations with Gemini Live audio output (default).",
    )
    voice_group.add_argument(
        "--no-speak-info",
        dest="speak_info",
        action="store_false",
        help="Start with Gemini Live voice output off; /speak on can re-enable it.",
    )
    parser.add_argument(
        "--no-reference-popup",
        action="store_true",
        help="Keep Google image references in the terminal instead of opening a chooser.",
    )
    parser.add_argument(
        "--voice-record-seconds",
        type=float,
        default=5.0,
        help="How long /voice records the microphone before transcribing.",
    )
    parser.add_argument(
        "--voice-input-model",
        default=None,
        help="Gemini model for push-to-talk voice command transcription.",
    )
    parser.add_argument(
        "--voice-device",
        default=None,
        help="Optional sounddevice input device id/name for /voice.",
    )
    parser.add_argument(
        "--voice-direct",
        action="store_true",
        help="Compatibility flag; spoken build prompts already use direct Text2Blender by default.",
    )
    parser.add_argument(
        "--voice-adk",
        action="store_true",
        help="Route spoken build prompts through the optional ADK concierge.",
    )
    parser.add_argument(
        "--no-auto-voice-input",
        action="store_true",
        help="Do not start the always-listening voice command loop.",
    )
    parser.add_argument(
        "--wake-phrase",
        default="Hey Travis",
        help="Wake phrase required for automatic voice commands.",
    )
    parser.add_argument(
        "--no-wake-phrase",
        action="store_true",
        help="Let automatic voice input run without requiring the wake phrase.",
    )
    parser.add_argument(
        "--auto-voice-silence-seconds",
        type=float,
        default=0.9,
        help="How much silence ends an automatic voice command.",
    )
    parser.add_argument(
        "--auto-voice-min-speech-seconds",
        type=float,
        default=0.25,
        help="Ignore shorter automatic voice bursts as noise.",
    )
    parser.add_argument(
        "--auto-voice-max-seconds",
        type=float,
        default=12.0,
        help="Maximum length of one automatic voice command.",
    )
    parser.add_argument(
        "--auto-voice-energy-threshold",
        type=float,
        default=500.0,
        help="RMS threshold for detecting speech in automatic voice input.",
    )
    parser.add_argument(
        "--typed-first",
        action="store_true",
        help="Keep empty Enter as exit instead of starting voice input.",
    )
    return parser


def _start_gesture_provider(args: argparse.Namespace, stop_event: threading.Event):
    tracker = WebcamFingerTracker(camera_index=args.camera, num_hands=1)
    controller = HandMouseController(
        backend=PynputMouseBackend(),
        touch_threshold=args.gesture_touch_threshold,
        mode_hold_frames=args.gesture_hold_frames,
        smoothing=args.gesture_smoothing,
        inner_area_percent=args.gesture_inner_area,
    )
    provider = HandMouseCursorProvider(
        tracker=tracker,
        controller=controller,
        debug=args.gesture_debug,
    )

    if not provider.start():
        status = provider.status()
        raise RuntimeError(f"gesture provider failed: {status.get('last_error')}")

    def pump() -> None:
        try:
            while not stop_event.is_set():
                provider.get_cursor()
                provider.pump_ui()
                time.sleep(1 / 60)
        finally:
            provider.stop()

    thread = threading.Thread(target=pump, name="gesture-mouse", daemon=True)
    thread.start()
    return provider, thread


def _resolve_wake_phrase(args: argparse.Namespace) -> str | None:
    if getattr(args, "no_wake_phrase", False):
        return None
    wake_phrase = str(getattr(args, "wake_phrase", "") or "").strip()
    return wake_phrase or None


def _voice_uses_adk(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "voice_adk", False)) and not bool(
        getattr(args, "voice_direct", False)
    )


def _start_auto_voice_listener(
    args: argparse.Namespace,
    *,
    stop_event: threading.Event,
    busy_event: threading.Event,
    settings: RuntimeSettings,
    info_agent: GeminiLivePartInfoAgent | None,
    reference_state: ReferenceWorkflowState,
    command_lock: threading.Lock,
) -> threading.Thread:
    wake_phrase = _resolve_wake_phrase(args)

    def listen_loop() -> None:
        if settings.auto_voice_enabled():
            if not wake_phrase:
                print("Auto voice input is listening. Speak a command, then pause.")
            else:
                print(f'Auto voice input is listening. Say "{wake_phrase}" first.')
        else:
            print("Auto voice input is off. Type /mic on to enable it.")
        last_error: str | None = None
        while not stop_event.is_set():
            if not settings.auto_voice_enabled():
                time.sleep(0.25)
                continue
            if busy_event.is_set():
                time.sleep(0.15)
                continue
            if not wait_for_speech_to_finish(timeout=0.0):
                wait_for_speech_to_finish()
                continue

            speech_started = False

            def on_speech_start() -> None:
                nonlocal speech_started
                speech_started = True
                busy_event.set()
                print("\nListening...")

            try:
                audio = record_microphone_until_silence(
                    device=args.voice_device,
                    energy_threshold=args.auto_voice_energy_threshold,
                    silence_seconds=args.auto_voice_silence_seconds,
                    min_speech_seconds=args.auto_voice_min_speech_seconds,
                    max_record_seconds=args.auto_voice_max_seconds,
                    on_speech_start=on_speech_start,
                    should_stop=lambda: (
                        stop_event.is_set()
                        or not settings.auto_voice_enabled()
                        or not wait_for_speech_to_finish(timeout=0.0)
                    ),
                )
            except VoiceInputUnavailable as exc:
                message = str(exc)
                if message != last_error:
                    print(f"Auto voice input disabled: {message}")
                settings.set_auto_voice_input(False)
                busy_event.clear()
                return

            if not audio:
                if speech_started:
                    busy_event.clear()
                continue

            try:
                command = transcribe_voice_command(
                    audio,
                    model=args.voice_input_model,
                    wake_phrase=wake_phrase,
                )
            except Exception as exc:
                print(f"Voice command failed: {exc}")
                busy_event.clear()
                time.sleep(0.5)
                continue

            if not command:
                if not wake_phrase:
                    print("I could not hear a clear command.")
                else:
                    print(f'Wake phrase not heard. Say "{wake_phrase}" before the command.')
                busy_event.clear()
                continue
            if command.lower().startswith(("/voice", "/listen")):
                print("Voice command heard itself; ignoring.")
                busy_event.clear()
                continue

            print(f"Voice command: {command}")
            force_adk = _voice_uses_adk(args)
            if force_adk and not command.startswith("/"):
                print("Voice route: Gemini-improved ADK")
            elif not command.startswith("/"):
                print("Voice route: direct Text2Blender")

            with command_lock:
                code = _execute_user_input(
                    command,
                    args=args,
                    busy_event=busy_event,
                    settings=settings,
                    info_agent=info_agent,
                    reference_state=reference_state,
                    force_adk=force_adk,
                    improve_prompt=False,
                )
            busy_event.clear()
            if code == EXIT_REQUESTED:
                stop_event.set()
                return
            if code:
                stop_event.set()
                return

    thread = threading.Thread(target=listen_loop, name="auto-voice-input", daemon=True)
    thread.start()
    return thread


def _improve_prompt_for_generation(prompt: str) -> str:
    try:
        improved = improve_build_prompt(prompt)
    except VoiceInputUnavailable as exc:
        print(f"Gemini prompt improvement unavailable: {exc}")
        return prompt
    except Exception as exc:
        print(f"Gemini prompt improvement failed; using original prompt: {exc}")
        return prompt

    if improved.strip() and improved.strip() != prompt.strip():
        print("\nGemini improved prompt:")
        print(improved)
    return improved or prompt


def _run_prompt(
    prompt: str,
    busy_event: threading.Event | None = None,
    *,
    improve_prompt: bool = True,
) -> int:
    if busy_event:
        busy_event.set()
    try:
        generation_prompt = _improve_prompt_for_generation(prompt) if improve_prompt else prompt
        path = resolve_text2blender_path()
        command = resolve_text2blender_command(path)
        reply = _run_text2blender_cli(path, command, generation_prompt)
    except Text2BlenderUnavailable as exc:
        print(f"Text2Blender failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if busy_event:
            busy_event.clear()

    print("\nText2Blender reply:")
    print(reply)
    print()
    return 0


def _run_reference_search(
    raw: str,
    *,
    args: argparse.Namespace,
    state: ReferenceWorkflowState,
    busy_event: threading.Event,
) -> int:
    scope, query = _parse_reference_args(raw)
    conn = BlenderConnection()
    try:
        snapshot = get_selected_object_snapshot(conn)
    finally:
        conn.disconnect()
    if not snapshot and not query:
        print("Select a Blender part/object first, or provide a search query.")
        return 0

    search_query = build_reference_query(snapshot, scope=scope, query=query)
    print(f"Searching Google image references for: {search_query}")
    busy_event.set()
    try:
        references = search_reference_images(search_query, count=5)
    except ReferenceImageSearchUnavailable as exc:
        print(f"Reference image search is not configured: {exc}")
        print("Add GOOGLE_CSE_ID to .env to enable Google image references.")
        return 0
    except Exception as exc:
        print(f"Reference image search failed: {exc}")
        return 1
    finally:
        busy_event.clear()

    state.references = references
    state.scope = scope
    state.query = search_query
    state.snapshot = snapshot
    if not references:
        print("No reference images found.")
        return 0

    print("\nReference images:")
    for idx, ref in enumerate(references, start=1):
        print(f"{idx}. {ref.title}")
        if ref.context_url:
            print(f"   page: {ref.context_url}")
        print(f"   image: {ref.image_url}")
    print("\nUse /use-ref <number> to refine the selected target, or /undo-ref to restore.")
    if args.no_reference_popup:
        return 0

    busy_event.set()
    try:
        choice = choose_reference_image(
            references,
            title=f"Forge References: {search_query[:80]}",
        )
    except ReferencePickerUnavailable as exc:
        print(f"Reference image window unavailable: {exc}")
        print("Use /use-ref <number> in the terminal instead.")
        return 0
    finally:
        busy_event.clear()

    if choice is None:
        print("Reference selection cancelled.")
        return 0
    return _run_reference_refine(str(choice + 1), state=state, busy_event=busy_event)


def _run_reference_refine(
    raw: str,
    *,
    state: ReferenceWorkflowState,
    busy_event: threading.Event,
) -> int:
    if not state.references:
        print("No reference list yet. Run /refs first.")
        return 0
    try:
        index = int(raw.strip()) - 1
    except ValueError:
        print("Usage: /use-ref <number>")
        return 0
    if index < 0 or index >= len(state.references):
        print(f"Choose a number from 1 to {len(state.references)}.")
        return 0

    conn = BlenderConnection()
    try:
        snapshot = get_selected_object_snapshot(conn) or state.snapshot
    finally:
        conn.disconnect()
    if not snapshot:
        print("No Blender target is selected. Select the part/object to refine.")
        return 0

    reference = state.references[index]
    scope = state.scope
    target_name = str(snapshot.get("name") or "")
    if not target_name:
        print("Selected target has no Blender object name.")
        return 0

    print(f"Creating restore point for {scope}: {target_name}")
    busy_event.set()
    try:
        conn = BlenderConnection()
        try:
            restore_point = create_restore_point(conn, target_name=target_name, scope=scope)
        finally:
            conn.disconnect()
        if not restore_point.get("ok"):
            print(f"Could not create restore point: {restore_point}")
            return 1
        state.restore_point = restore_point

        print("Analyzing chosen reference image...")
        reference_description = describe_reference_image(reference, target_snapshot=snapshot)
        prompt = build_reference_refine_prompt(
            snapshot=snapshot,
            reference=reference,
            reference_description=reference_description,
            scope=scope,
        )
    except Exception as exc:
        print(f"Reference preparation failed: {exc}")
        return 1
    finally:
        busy_event.clear()

    print("Refining with Text2Blender. Use /undo-ref if the result is worse.")
    return _run_prompt(prompt, busy_event, improve_prompt=False)


def _run_part_replacement(
    raw: str,
    *,
    state: ReferenceWorkflowState,
    busy_event: threading.Event,
) -> int:
    replacement_request = raw.strip()
    if not replacement_request:
        print("Usage: /replace <new part description>")
        return 0

    conn = BlenderConnection()
    try:
        snapshot = get_selected_object_snapshot(conn)
    finally:
        conn.disconnect()
    if not snapshot:
        print("No Blender part is selected. Select the part to replace first.")
        return 0

    target_name = str(snapshot.get("name") or "")
    if not target_name:
        print("Selected target has no Blender object name.")
        return 0

    print(f"Backing up and hiding selected part: {target_name}")
    busy_event.set()
    try:
        conn = BlenderConnection()
        try:
            restore_point = prepare_part_replacement(conn, target_name=target_name)
        finally:
            conn.disconnect()
        if not restore_point.get("ok"):
            print(f"Could not prepare replacement: {restore_point}")
            return 1
        state.restore_point = restore_point
        state.snapshot = snapshot
        state.scope = "part"
        state.query = replacement_request
        prompt = build_part_replacement_prompt(
            snapshot=snapshot,
            replacement_request=replacement_request,
        )
    except Exception as exc:
        print(f"Replacement preparation failed: {exc}")
        return 1
    finally:
        busy_event.clear()

    print("Generating replacement part with Text2Blender. Use /undo-replace if needed.")
    code = _run_prompt(prompt, busy_event, improve_prompt=False)
    if code:
        print("Replacement generation failed; restoring the original selected part.")
        _run_reference_undo(state=state, busy_event=busy_event)
    return code


def _run_part_addition(
    raw: str,
    *,
    state: ReferenceWorkflowState,
    busy_event: threading.Event,
) -> int:
    addition_request = raw.strip()
    if not addition_request:
        print("Usage: /add-part <new part description>")
        return 0

    conn = BlenderConnection()
    try:
        snapshot = get_selected_object_snapshot(conn)
    finally:
        conn.disconnect()
    if not snapshot:
        print("No Blender part is selected. Select the anchor part first.")
        return 0

    target_name = str(snapshot.get("name") or "")
    if not target_name:
        print("Selected target has no Blender object name.")
        return 0

    print(f"Creating restore point before adding to: {target_name}")
    busy_event.set()
    try:
        conn = BlenderConnection()
        try:
            restore_point = create_restore_point(conn, target_name=target_name, scope="object")
        finally:
            conn.disconnect()
        if not restore_point.get("ok"):
            print(f"Could not create restore point: {restore_point}")
            return 1
        state.restore_point = restore_point
        state.snapshot = snapshot
        state.scope = "object"
        state.query = addition_request
        prompt = build_part_addition_prompt(
            snapshot=snapshot,
            addition_request=addition_request,
        )
    except Exception as exc:
        print(f"Add-part preparation failed: {exc}")
        return 1
    finally:
        busy_event.clear()

    print("Generating new add-on part with Text2Blender. Use /undo-add if needed.")
    code = _run_prompt(prompt, busy_event, improve_prompt=False)
    if code:
        print("Add-on generation failed; restoring the original object state.")
        _run_reference_undo(state=state, busy_event=busy_event)
    return code


def _run_reference_undo(
    *,
    state: ReferenceWorkflowState,
    busy_event: threading.Event,
) -> int:
    if not state.restore_point:
        print("No reference refinement restore point is available.")
        return 0
    busy_event.set()
    try:
        conn = BlenderConnection()
        try:
            result = restore_from_restore_point(conn, state.restore_point)
        finally:
            conn.disconnect()
    except Exception as exc:
        print(f"Restore failed: {exc}")
        return 1
    finally:
        busy_event.clear()

    print(f"Restore result: {result}")
    return 0


def _run_adk_prompt(prompt: str, busy_event: threading.Event | None = None) -> int:
    if busy_event:
        busy_event.set()
    try:
        from server.adk_app import run_forge_adk_text

        reply = run_forge_adk_text(prompt)
    except Exception as exc:
        print(f"ADK failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if busy_event:
            busy_event.clear()

    print("\nADK reply:")
    print(reply or "(no text returned)")
    print()
    return 0


def _explain_selected_part(
    *,
    agent: GeminiLivePartInfoAgent,
    speak: bool,
    label: str = "Gemini part info",
) -> str | None:
    conn = BlenderConnection()
    try:
        snapshot = get_selected_object_snapshot(conn)
    finally:
        conn.disconnect()

    if not snapshot:
        print("No Blender object is selected.")
        return None

    name = snapshot.get("name", "selected object")
    print(f"\nSelected: {name}")
    if speak:
        print("Asking Gemini Live to speak part info...")
        try:
            reply = agent.speak_part_live(snapshot)
        except Exception as exc:
            print(f"Gemini Live voice failed: {exc}")
            print("Using Gemini text fallback in the terminal.")
            reply = agent.explain_part_generate_from_snapshot(snapshot)
        else:
            print(f"Played Gemini Live voice audio ({agent.last_audio_bytes} bytes).")
    else:
        print("Asking Gemini for part info...")
        reply = agent.explain_part_generate_from_snapshot(snapshot)
    print(f"\n{label}:")
    print(reply)
    print()
    return str(name)


def _start_part_info_monitor(
    args: argparse.Namespace,
    *,
    stop_event: threading.Event,
    busy_event: threading.Event,
    settings: RuntimeSettings,
) -> threading.Thread:
    agent = GeminiLivePartInfoAgent(model=args.part_info_model)
    poll_s = max(0.25, float(args.info_poll_seconds))

    def monitor() -> None:
        candidate_name: str | None = None
        candidate_since = 0.0
        explained_name: str | None = None
        last_error: str | None = None
        while not stop_event.is_set():
            speak_info, auto_info, hold_s, _adk_prompts = settings.snapshot()
            if not auto_info:
                time.sleep(poll_s)
                continue
            if busy_event.is_set():
                time.sleep(poll_s)
                continue

            conn = BlenderConnection()
            try:
                snapshot = get_selected_object_snapshot(conn)
            except Exception as exc:
                message = str(exc)
                if message != last_error:
                    print(f"Part info monitor paused: {message}")
                    last_error = message
                time.sleep(poll_s)
                continue
            finally:
                conn.disconnect()

            last_error = None
            name = str(snapshot.get("name")) if snapshot else None
            if not name:
                candidate_name = None
                explained_name = None
                time.sleep(poll_s)
                continue

            now = time.monotonic()
            if name != candidate_name:
                candidate_name = name
                candidate_since = now
                print(f"\nSelected: {name} (hold for {hold_s:.1f}s for info)")
                time.sleep(poll_s)
                continue

            if name == explained_name or now - candidate_since < hold_s:
                time.sleep(poll_s)
                continue

            explained_name = name
            if speak_info:
                print(f"\n{name} held for {hold_s:.1f}s. Asking Gemini Live to speak part info...")
            else:
                print(f"\n{name} held for {hold_s:.1f}s. Asking Gemini for part info...")
            busy_event.set()
            try:
                if speak_info:
                    reply = agent.speak_part_live(snapshot)
                else:
                    reply = agent.explain_part_generate_from_snapshot(snapshot)
            except Exception as exc:
                print(f"Gemini part info failed: {exc}")
                if speak_info:
                    print("Using Gemini text fallback in the terminal.")
                    try:
                        reply = agent.explain_part_generate_from_snapshot(snapshot)
                    except Exception as fallback_exc:
                        print(f"Gemini text fallback failed: {fallback_exc}")
                        reply = ""
                    else:
                        print("\nGemini part info:")
                        print(reply)
                        print()
            else:
                if speak_info:
                    print(f"Played Gemini Live voice audio ({agent.last_audio_bytes} bytes).")
                print("\nGemini part info:")
                print(reply)
                print()
            finally:
                busy_event.clear()
            time.sleep(poll_s)

    thread = threading.Thread(target=monitor, name="part-info-monitor", daemon=True)
    thread.start()
    return thread


def _print_help(settings: RuntimeSettings, args: argparse.Namespace) -> None:
    speak_info, auto_info, hold_s, adk_prompts = settings.snapshot()
    wake_phrase = _resolve_wake_phrase(args)
    print("Type an object prompt to generate in Blender.")
    print("Raw text and /build <prompt> both generate objects after Gemini improves the prompt.")
    print("Use /adk <message> to route one command through Google ADK.")
    print("Select an object in Blender and hold it selected for part info.")
    if wake_phrase:
        print(f'Voice input listens automatically after "{wake_phrase}"; type /mic off to pause it.')
    else:
        print("Voice input listens automatically; type /mic off to pause it.")
    print("Commands:")
    print("  /info                 explain current selection now")
    print("  /adk <message>         run one command through Google ADK")
    print("  /adk on|off            make raw prompts use ADK or direct Text2Blender")
    print("  /voice [seconds]       record mic once; build directly with Text2Blender")
    print("  /mic on|off            turn automatic voice input on/off")
    print("  /refs [part|object] [query]  open Google image reference chooser")
    print("  /use-ref <number>      refine selected target with a reference")
    print("  /add-part <prompt>    keep selection and generate a new attached part")
    print("  /replace <prompt>      hide selected part and generate a replacement")
    print("  /undo-ref              restore before the last reference/add/replace")
    print("  /speak on|off          turn Gemini Live voice output on/off")
    print("  /auto on|off           turn automatic selection explanations on/off")
    print("  /hold <seconds>        set selection hold delay")
    print("  /mode quiet            typed + gesture only; no voice output")
    print("  /mode voice            typed + gesture + Gemini Live voice output")
    print("  /status               show current demo settings")
    print("  /quit                 exit")
    print(
        "Current: "
        f"speak={'on' if speak_info else 'off'}, "
        f"auto={'on' if auto_info else 'off'}, "
        f"hold={hold_s:.1f}s, "
        f"raw_prompts={'adk' if adk_prompts else 'direct'}"
    )


def _print_status(settings: RuntimeSettings, args: argparse.Namespace) -> None:
    speak_info, auto_info, hold_s, adk_prompts = settings.snapshot()
    wake_phrase = _resolve_wake_phrase(args)
    print(f"Speak info: {'on' if speak_info else 'off'}")
    print(f"Automatic selection info: {'on' if auto_info else 'off'}")
    print(f"Automatic voice input: {'on' if settings.auto_voice_enabled() else 'off'}")
    print(f"Automatic voice wake phrase: {wake_phrase or 'off'}")
    print(f"Spoken build route: {'ADK' if _voice_uses_adk(args) else 'direct Text2Blender'}")
    print(f"Selection hold: {hold_s:.1f}s")
    print(f"Raw prompt route: {'ADK' if adk_prompts else 'direct Text2Blender'}")


def _handle_command(
    command: str,
    *,
    args: argparse.Namespace,
    busy_event: threading.Event,
    settings: RuntimeSettings,
    info_agent: GeminiLivePartInfoAgent | None,
    reference_state: ReferenceWorkflowState,
) -> int | None:
    lower = command.lower()
    if lower in {"/help", "/h", "help"}:
        _print_help(settings, args)
        return 0
    if lower in {"/q", "/quit", "/exit"}:
        return EXIT_REQUESTED
    if lower == "/status":
        _print_status(settings, args)
        return 0
    if lower == "/info":
        if info_agent is None:
            print("Gemini part info is disabled because the API key is missing.")
            return 0
        busy_event.set()
        try:
            speak_info, _auto_info, _hold_s, _adk_prompts = settings.snapshot()
            _explain_selected_part(agent=info_agent, speak=speak_info)
        finally:
            busy_event.clear()
        return 0
    if lower.startswith("/build "):
        prompt = command.split(" ", 1)[1].strip()
        if not prompt:
            print("Usage: /build <object prompt>")
            return 0
        return _run_prompt(prompt, busy_event)
    if lower in {"/speak on", "/voice on"}:
        settings.set_speak(True)
        print("Gemini Live voice output: on")
        return 0
    if lower in {"/speak off", "/voice off"}:
        settings.set_speak(False)
        print("Gemini Live voice output: off")
        return 0
    if lower in {"/mic on", "/auto-voice on", "/listen on"}:
        settings.set_auto_voice_input(True)
        wake_phrase = _resolve_wake_phrase(args)
        if wake_phrase:
            print(f'Automatic voice input: on. Say "{wake_phrase}" before a command.')
        else:
            print("Automatic voice input: on")
        return 0
    if lower in {"/mic off", "/auto-voice off", "/listen off"}:
        settings.set_auto_voice_input(False)
        print("Automatic voice input: off")
        return 0
    if lower == "/voice" or lower.startswith("/voice "):
        return _run_voice_command(
            command[6:].strip(),
            args=args,
            busy_event=busy_event,
            settings=settings,
            info_agent=info_agent,
            reference_state=reference_state,
        )
    if lower == "/listen" or lower.startswith("/listen "):
        return _run_voice_command(
            command[7:].strip(),
            args=args,
            busy_event=busy_event,
            settings=settings,
            info_agent=info_agent,
            reference_state=reference_state,
        )
    if lower == "/refs" or lower.startswith("/refs "):
        return _run_reference_search(
            command[5:].strip(),
            args=args,
            state=reference_state,
            busy_event=busy_event,
        )
    if lower.startswith("/images"):
        return _run_reference_search(
            command[7:].strip(),
            args=args,
            state=reference_state,
            busy_event=busy_event,
        )
    if lower.startswith("/use-ref "):
        return _run_reference_refine(
            command.split(" ", 1)[1],
            state=reference_state,
            busy_event=busy_event,
        )
    if lower.startswith("/add-part "):
        return _run_part_addition(
            command.split(" ", 1)[1],
            state=reference_state,
            busy_event=busy_event,
        )
    if lower.startswith("/add-on "):
        return _run_part_addition(
            command.split(" ", 1)[1],
            state=reference_state,
            busy_event=busy_event,
        )
    if lower.startswith("/attach "):
        return _run_part_addition(
            command.split(" ", 1)[1],
            state=reference_state,
            busy_event=busy_event,
        )
    if lower.startswith("/replace "):
        return _run_part_replacement(
            command.split(" ", 1)[1],
            state=reference_state,
            busy_event=busy_event,
        )
    if lower.startswith("/replace-part "):
        return _run_part_replacement(
            command.split(" ", 1)[1],
            state=reference_state,
            busy_event=busy_event,
        )
    if lower.startswith("/swap "):
        return _run_part_replacement(
            command.split(" ", 1)[1],
            state=reference_state,
            busy_event=busy_event,
        )
    if lower.startswith("/refine-ref "):
        return _run_reference_refine(
            command.split(" ", 1)[1],
            state=reference_state,
            busy_event=busy_event,
        )
    if lower in {"/undo-ref", "/undo-replace", "/undo-add"}:
        return _run_reference_undo(state=reference_state, busy_event=busy_event)
    if lower == "/adk on":
        settings.set_adk_prompts(True)
        print("Raw prompt route: ADK")
        return 0
    if lower == "/adk off":
        settings.set_adk_prompts(False)
        print("Raw prompt route: direct Text2Blender")
        return 0
    if lower.startswith("/adk "):
        prompt = command.split(" ", 1)[1].strip()
        if not prompt:
            print("Usage: /adk <message>")
            return 0
        return _run_adk_prompt(prompt, busy_event)
    if lower in {"/auto on", "/auto-info on"}:
        settings.set_auto_info(True)
        print("Automatic selection explanations: on")
        return 0
    if lower in {"/auto off", "/auto-info off"}:
        settings.set_auto_info(False)
        print("Automatic selection explanations: off")
        return 0
    if lower.startswith("/hold "):
        value = command.split(" ", 1)[1].strip()
        try:
            seconds = float(value)
        except ValueError:
            print("Usage: /hold <seconds>")
            return 0
        settings.set_hold_seconds(seconds)
        print(f"Selection hold: {max(0.0, seconds):.1f}s")
        return 0
    if lower == "/mode quiet":
        settings.set_speak(False)
        settings.set_auto_info(True)
        print("Mode: quiet typed demo. Gesture + typed prompts stay active; voice output is off.")
        return 0
    if lower == "/mode voice":
        settings.set_speak(True)
        settings.set_auto_info(True)
        print("Mode: voice output demo. Gesture + typed prompts stay active; Gemini Live speaks info.")
        return 0
    if lower == "/mode adk":
        settings.set_adk_prompts(True)
        settings.set_auto_info(True)
        print("Mode: ADK typed demo. Raw typed prompts route through Google ADK.")
        return 0
    if lower == "/mode silent":
        settings.set_speak(False)
        settings.set_auto_info(False)
        print("Mode: silent. Gesture + typed prompts stay active; automatic explanations are off.")
        return 0
    if lower.startswith("/"):
        print(f"Unknown command: {command}")
        print("Type /help for commands.")
        return 0
    return None


def _parse_reference_args(raw: str) -> tuple[str, str]:
    parts = raw.strip().split()
    if not parts:
        return "part", ""
    first = parts[0].lower()
    if first in {"part", "object"}:
        return first, " ".join(parts[1:]).strip()
    return "part", raw.strip()


def _run_voice_command(
    raw: str,
    *,
    args: argparse.Namespace,
    busy_event: threading.Event,
    settings: RuntimeSettings,
    info_agent: GeminiLivePartInfoAgent | None,
    reference_state: ReferenceWorkflowState,
) -> int:
    try:
        duration = float(raw) if raw.strip() else float(args.voice_record_seconds)
    except ValueError:
        print("Usage: /voice [seconds]")
        return 0
    duration = max(1.0, min(15.0, duration))

    if not wait_for_speech_to_finish(timeout=0.0):
        print("Waiting for the current spoken explanation to finish...")
        wait_for_speech_to_finish()

    print(f"Recording voice command for {duration:.1f}s...")
    busy_event.set()
    try:
        audio = record_microphone_wav(
            duration_seconds=duration,
            device=args.voice_device,
        )
        command = transcribe_voice_command(audio, model=args.voice_input_model)
    except VoiceInputUnavailable as exc:
        print(f"Voice input unavailable: {exc}")
        return 0
    except Exception as exc:
        print(f"Voice command failed: {exc}")
        return 1
    finally:
        busy_event.clear()

    if not command:
        print("I could not hear a clear command.")
        return 0
    if command.lower().startswith(("/voice", "/listen")):
        print("Voice command heard itself; ignoring.")
        return 0

    print(f"Voice command: {command}")
    force_adk = _voice_uses_adk(args)
    if force_adk and not command.startswith("/"):
        print("Voice route: Gemini-improved ADK")
    elif not command.startswith("/"):
        print("Voice route: direct Text2Blender")
    return _execute_user_input(
        command,
        args=args,
        busy_event=busy_event,
        settings=settings,
        info_agent=info_agent,
        reference_state=reference_state,
        force_adk=force_adk,
        improve_prompt=False,
    )


def _execute_user_input(
    prompt: str,
    *,
    args: argparse.Namespace,
    busy_event: threading.Event,
    settings: RuntimeSettings,
    info_agent: GeminiLivePartInfoAgent | None,
    reference_state: ReferenceWorkflowState,
    force_adk: bool = False,
    improve_prompt: bool = True,
) -> int:
    if prompt.startswith("/") or prompt.lower() == "help":
        result = _handle_command(
            prompt,
            args=args,
            busy_event=busy_event,
            settings=settings,
            info_agent=info_agent,
            reference_state=reference_state,
        )
        if result is None:
            return _run_prompt(prompt, busy_event, improve_prompt=improve_prompt)
        return result

    _speak_info, _auto_info, _hold_s, adk_prompts = settings.snapshot()
    if force_adk or adk_prompts:
        return _run_adk_prompt(prompt, busy_event)
    return _run_prompt(prompt, busy_event, improve_prompt=improve_prompt)


def _normalize_interactive_input(raw: str, *, typed_first: bool) -> str | None:
    prompt = raw.strip()
    if prompt in {"/q", "/quit", "/exit"}:
        return None
    if prompt:
        return prompt
    if typed_first:
        return None
    return "/voice"


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    args = build_arg_parser().parse_args(argv)
    stop_event = threading.Event()
    busy_event = threading.Event()
    settings = RuntimeSettings(
        speak_info=bool(args.speak_info),
        auto_info=not bool(args.no_auto_info),
        auto_voice_input=not bool(args.no_auto_voice_input),
        selection_hold_seconds=max(0.0, float(args.selection_hold_seconds)),
    )
    reference_state = ReferenceWorkflowState()
    gesture_thread: threading.Thread | None = None
    info_thread: threading.Thread | None = None
    voice_thread: threading.Thread | None = None
    command_lock = threading.Lock()

    def stop(_signum=None, _frame=None) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        if not args.no_gestures:
            _provider, gesture_thread = _start_gesture_provider(args, stop_event)
            print("Gesture mouse is running.")
            print("Point/move: hand movement")
            print("Click/drag: thumb + index")
            print("Zoom: thumb + middle, move vertically")
            print("Orbit: thumb + ring, drag")
            print("Pan: peace sign, drag")
            print("Release: thumb + pinky or fist")
            print()

        info_agent: GeminiLivePartInfoAgent | None = None
        try:
            info_agent = GeminiLivePartInfoAgent(model=args.part_info_model)
            info_thread = _start_part_info_monitor(
                args,
                stop_event=stop_event,
                busy_event=busy_event,
                settings=settings,
            )
        except RuntimeError as exc:
            print(f"Part info monitor disabled: {exc}")
            print("Set GOOGLE_API_KEY or GEMINI_API_KEY to enable Gemini part info.")
            print()
        else:
            _speak_info, auto_info, hold_s, _adk_prompts = settings.snapshot()
            print("Part info monitor is running.")
            if auto_info:
                print(
                    "Select an object in Blender and keep it selected for "
                    f"{hold_s:.1f}s to ask what it is."
                )
            else:
                print("Automatic selection explanations start off; type /auto on to enable.")
            print("Use /info to explain the current selection manually.")
            print()

        if args.prompt:
            return _run_prompt(args.prompt, busy_event)

        voice_thread = _start_auto_voice_listener(
            args,
            stop_event=stop_event,
            busy_event=busy_event,
            settings=settings,
            info_agent=info_agent,
            reference_state=reference_state,
            command_lock=command_lock,
        )

        if args.typed_first:
            print("Type a Blender object prompt. Empty line or /quit exits.")
        else:
            print("Speak a command anytime. Type a command to use text. Type /quit to exit.")
        print(
            "Commands: /mic off, /voice, /info, /refs, /add-part, /replace, /undo-add, "
            "/adk <message>, /speak on|off, /help, /quit"
        )
        while not stop_event.is_set():
            try:
                raw_prompt = input("prompt> ")
            except EOFError:
                break
            prompt = _normalize_interactive_input(raw_prompt, typed_first=bool(args.typed_first))
            if prompt is None:
                break
            with command_lock:
                code = _execute_user_input(
                    prompt,
                    args=args,
                    busy_event=busy_event,
                    settings=settings,
                    info_agent=info_agent,
                    reference_state=reference_state,
                )
            if code == EXIT_REQUESTED:
                break
            if code:
                return code
        return 0
    finally:
        stop_event.set()
        if gesture_thread and gesture_thread.is_alive():
            gesture_thread.join(timeout=1.5)
        if info_thread and info_thread.is_alive():
            info_thread.join(timeout=1.5)
        if voice_thread and voice_thread.is_alive():
            voice_thread.join(timeout=1.5)


if __name__ == "__main__":
    raise SystemExit(main())
