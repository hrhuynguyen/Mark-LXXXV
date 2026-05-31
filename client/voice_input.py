"""Push-to-talk voice command capture and transcription."""

from __future__ import annotations

import io
import math
import os
import struct
import time
import wave
from collections import deque
from collections.abc import Callable

from google import genai
from google.genai import types

DEFAULT_VOICE_MODEL = "gemini-2.5-flash"
DEFAULT_SAMPLE_RATE = 16000


class VoiceInputUnavailable(RuntimeError):
    """Raised when microphone capture or voice transcription is unavailable."""


def record_microphone_wav(
    *,
    duration_seconds: float = 5.0,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    device: int | str | None = None,
) -> bytes:
    """Record mono microphone audio and return WAV bytes."""

    if duration_seconds <= 0:
        raise VoiceInputUnavailable("Recording duration must be greater than zero.")
    try:
        import sounddevice as sd
    except Exception as exc:  # pragma: no cover - depends on local audio install
        raise VoiceInputUnavailable(f"sounddevice is unavailable: {exc}") from exc

    frame_count = max(1, int(float(duration_seconds) * int(sample_rate)))
    try:
        frames = sd.rec(
            frame_count,
            samplerate=int(sample_rate),
            channels=1,
            dtype="int16",
            device=device,
        )
        sd.wait()
    except Exception as exc:  # pragma: no cover - depends on local audio hardware/permissions
        raise VoiceInputUnavailable(f"Microphone recording failed: {exc}") from exc

    return pcm16_to_wav_bytes(frames.tobytes(), sample_rate=int(sample_rate), channels=1)


def record_microphone_until_silence(
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    device: int | str | None = None,
    energy_threshold: float = 500.0,
    silence_seconds: float = 0.9,
    min_speech_seconds: float = 0.25,
    max_record_seconds: float = 12.0,
    idle_timeout_seconds: float = 60.0,
    chunk_seconds: float = 0.1,
    pre_roll_seconds: float = 0.25,
    on_speech_start: Callable[[], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bytes:
    """Record until speech starts and then stops for a short silence window."""

    try:
        import sounddevice as sd
    except Exception as exc:  # pragma: no cover - depends on local audio install
        raise VoiceInputUnavailable(f"sounddevice is unavailable: {exc}") from exc

    chunk_frames = max(1, int(int(sample_rate) * max(0.02, float(chunk_seconds))))
    silence_chunks = max(1, math.ceil(max(0.1, float(silence_seconds)) / chunk_seconds))
    min_voice_chunks = max(1, math.ceil(max(0.0, float(min_speech_seconds)) / chunk_seconds))
    pre_roll_chunks = max(0, math.ceil(max(0.0, float(pre_roll_seconds)) / chunk_seconds))
    idle_timeout_seconds = max(1.0, float(idle_timeout_seconds))
    max_record_seconds = max(1.0, float(max_record_seconds))

    pre_roll: deque[bytes] = deque(maxlen=pre_roll_chunks)
    frames: list[bytes] = []
    speech_started = False
    speech_started_at = 0.0
    voice_chunks = 0
    silent_chunks = 0
    started_at = time.monotonic()

    try:
        with sd.InputStream(
            samplerate=int(sample_rate),
            channels=1,
            dtype="int16",
            blocksize=chunk_frames,
            device=device,
        ) as stream:
            while True:
                if should_stop and should_stop():
                    return b""
                chunk, _overflowed = stream.read(chunk_frames)
                pcm = bytes(chunk.tobytes())
                is_voice = pcm16_rms(pcm) >= float(energy_threshold)
                now = time.monotonic()

                if not speech_started:
                    if pre_roll_chunks:
                        pre_roll.append(pcm)
                    if is_voice:
                        speech_started = True
                        speech_started_at = now
                        voice_chunks = 1
                        silent_chunks = 0
                        frames.extend(pre_roll)
                        if not pre_roll_chunks:
                            frames.append(pcm)
                        if on_speech_start:
                            on_speech_start()
                    elif now - started_at >= idle_timeout_seconds:
                        return b""
                    continue

                if pre_roll_chunks and frames and frames[-1] is not pcm:
                    frames.append(pcm)
                elif not pre_roll_chunks:
                    pass

                if is_voice:
                    voice_chunks += 1
                    silent_chunks = 0
                else:
                    silent_chunks += 1

                if (
                    voice_chunks >= min_voice_chunks
                    and silent_chunks >= silence_chunks
                ):
                    break
                if now - speech_started_at >= max_record_seconds:
                    break
    except Exception as exc:  # pragma: no cover - depends on local audio hardware/permissions
        raise VoiceInputUnavailable(f"Microphone recording failed: {exc}") from exc

    if voice_chunks < min_voice_chunks:
        return b""
    return pcm16_to_wav_bytes(b"".join(frames), sample_rate=int(sample_rate), channels=1)


def transcribe_voice_command(
    wav_audio: bytes,
    *,
    model: str | None = None,
    api_key: str | None = None,
    wake_phrase: str | None = None,
) -> str:
    """Ask Gemini to turn a short audio clip into one Forge command."""

    api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise VoiceInputUnavailable("Set GOOGLE_API_KEY or GEMINI_API_KEY to use voice input.")
    if not wav_audio:
        raise VoiceInputUnavailable("No voice audio was recorded.")

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model or os.getenv("FORGE_VOICE_INPUT_MODEL", DEFAULT_VOICE_MODEL),
        contents=[
            types.Part.from_text(text=build_voice_command_prompt(wake_phrase=wake_phrase)),
            types.Part.from_bytes(data=wav_audio, mime_type="audio/wav"),
        ],
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=80,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return clean_voice_command(response.text or "")


def improve_build_prompt(
    prompt: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
) -> str:
    """Ask Gemini to expand a short object request into a richer Blender prompt."""

    original = str(prompt or "").strip()
    if not original:
        return ""
    api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise VoiceInputUnavailable("Set GOOGLE_API_KEY or GEMINI_API_KEY to improve prompts.")

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model or os.getenv("FORGE_PROMPT_IMPROVER_MODEL", DEFAULT_VOICE_MODEL),
        contents=build_build_prompt_improver_prompt(original),
        config=types.GenerateContentConfig(
            temperature=0.35,
            max_output_tokens=180,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return clean_voice_command(response.text or "") or original


def build_build_prompt_improver_prompt(prompt: str) -> str:
    return f"""Improve this Blender object generation command for Text2Blender.

Return exactly one detailed prompt line. Do not explain and do not use markdown.

Rules:
- Preserve the user's object and intent.
- Add named visible parts, clear proportions, colors/materials, and medium detail.
- Prefer features that are useful for a 3D model: shape, symmetry, structure, surface finish,
  key components, and readable scale.
- Do not add unrelated objects, scenes, backgrounds, text labels, or file names.
- Keep it concise enough to run as one generation command.

User command: {str(prompt or "").strip()}
"""


def build_voice_command_prompt(*, wake_phrase: str | None = None) -> str:
    wake_rule = ""
    if wake_phrase:
        wake_rule = f"""
Wake phrase:
- Only return a command if the user says "{wake_phrase}" near the beginning.
- If the wake phrase is missing, unclear, or only appears in background speech, return an empty string.
- Remove the wake phrase from the final command.
"""
    return f"""You are transcribing and polishing a voice command for Forge, a Blender demo.

Return exactly one command line that the terminal app can execute. Do not explain.
{wake_rule}

Rules:
- If the user asks to add, attach, or add on a new part to the selected/current part
  or object, return: /add-part <short description of the new part>
- If the user asks to replace, swap, regenerate, or remake the selected part,
  return: /replace <short description of the new part>
- If the user asks to create, build, spawn, generate, or make an object, return a clean,
  improved build prompt that preserves their intent and adds useful visual modeling detail:
  named visible parts, proportions, colors/materials, and a medium level of detail.
  Example: "build a detailed Falcon 9 rocket with white cylindrical first stage, black
  interstage band, pointed fairing, nine engine bells, grid fins, and four deployable
  landing legs"
- If the user asks what the selected object is, return: /info
- If the user asks for reference images for the selected part, return: /refs part
- If the user asks for reference images for the whole object, return: /refs object
- If the user asks to undo or go back after a reference refinement, return: /undo-ref
- If the user asks for quiet mode or to stop speaking, return: /speak off
- If the user asks to speak out loud again, return: /speak on
- If the user says quit, exit, or stop the program, return: /quit
- If the audio is unclear or empty, return an empty string.
"""


def clean_voice_command(text: str) -> str:
    command = str(text or "").strip()
    if command.startswith("```"):
        command = command.strip("`").strip()
    first_line = next((line.strip() for line in command.splitlines() if line.strip()), "")
    if first_line.lower().startswith("command:"):
        first_line = first_line.split(":", 1)[1].strip()
    return first_line.strip("\"' ")


def pcm16_to_wav_bytes(pcm: bytes, *, sample_rate: int, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(int(channels))
        wav.setsampwidth(2)
        wav.setframerate(int(sample_rate))
        wav.writeframes(pcm)
    return buffer.getvalue()


def pcm16_rms(pcm: bytes) -> float:
    sample_count = len(pcm) // 2
    if sample_count <= 0:
        return 0.0
    total = 0
    for (sample,) in struct.iter_unpack("<h", pcm[: sample_count * 2]):
        total += sample * sample
    return math.sqrt(total / sample_count)
