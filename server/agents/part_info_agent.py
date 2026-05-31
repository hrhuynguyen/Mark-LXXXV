"""Gemini Live part explainer for selected Blender objects."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types

DEFAULT_LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
DEFAULT_FALLBACK_MODEL = "gemini-2.5-flash"
LIVE_AUDIO_SAMPLE_RATE = 24000
SPEECH_PLAYBACK_LOCK = threading.Lock()

SYSTEM_INSTRUCTION = """\
You are Forge's Blender part explainer.

The user has selected one object or part in Blender. Use the provided Blender
metadata to explain what the part likely is and what it is used for.
Keep the answer complete, conversational, and short:
- answer "what is this?" and "what is it used for?";
- say the plain object category, not the internal Blender object/file name;
- use the name only as a clue, never read it verbatim unless it is a public label;
- do not list raw mesh statistics, coordinates, filenames, or collection names;
- if the object is ambiguous, state the uncertainty in plain language;
- finish in 1-2 short sentences.
"""


@dataclass
class GeminiLivePartInfoAgent:
    model: str | None = None
    fallback_model: str | None = None
    api_key: str | None = None
    live_api_version: str = "v1alpha"
    last_live_error: str | None = None
    live_available: bool | None = None
    last_audio_bytes: int = 0

    def __post_init__(self) -> None:
        self.model = self.model or os.getenv("FORGE_PART_INFO_LIVE_MODEL", DEFAULT_LIVE_MODEL)
        self.fallback_model = self.fallback_model or os.getenv(
            "FORGE_PART_INFO_MODEL", DEFAULT_FALLBACK_MODEL
        )
        self.api_key = self.api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not self.api_key:
            raise RuntimeError("Set GOOGLE_API_KEY or GEMINI_API_KEY before asking Gemini for part info.")

    def explain_part(self, snapshot: dict[str, Any]) -> str:
        """Ask Gemini Live for a concise explanation, falling back to GenerateContent."""

        prompt = build_part_info_prompt(snapshot)
        if self.live_available is not False:
            try:
                reply = asyncio.run(self.explain_part_live(prompt))
            except Exception as live_exc:
                self.last_live_error = str(live_exc)
                self.live_available = False
            else:
                self.live_available = True
                return reply
        return self.explain_part_generate(prompt)

    def speak_part_live(self, snapshot: dict[str, Any]) -> str:
        """Ask Gemini Live for audio output, play it, and return its transcript."""

        explanation = self.explain_part_generate_from_snapshot(snapshot)
        audio_chunks = [
            asyncio.run(self.speak_text_live_audio(segment))
            for segment in _split_text_for_speech(explanation)
        ]
        audio = _join_audio_chunks(audio_chunks)
        self.last_audio_bytes = len(audio)
        if audio:
            play_pcm_audio(audio)
        else:
            raise RuntimeError("Gemini Live returned no audio bytes.")
        return explanation

    async def explain_part_live(self, prompt: str) -> str:
        client = genai.Client(
            api_key=self.api_key,
            http_options={"api_version": self.live_api_version},
        )
        config = types.LiveConnectConfig(
            response_modalities=["TEXT"],
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.2,
            max_output_tokens=180,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        chunks: list[str] = []
        async with client.aio.live.connect(model=str(self.model), config=config) as session:
            await session.send_client_content(
                turns={"role": "user", "parts": [{"text": prompt}]},
                turn_complete=True,
            )
            async for message in session.receive():
                text = _extract_live_text(message)
                if text:
                    chunks.append(text)
                server_content = getattr(message, "server_content", None)
                if server_content is not None and getattr(server_content, "turn_complete", False):
                    break

        reply = "".join(chunks).strip()
        return reply or "(Gemini Live returned no text.)"

    async def explain_part_live_audio(self, prompt: str) -> tuple[str, bytes]:
        client = genai.Client(
            api_key=self.api_key,
            http_options={"api_version": self.live_api_version},
        )
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription=types.AudioTranscriptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
                )
            ),
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.2,
            max_output_tokens=220,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        transcript_chunks: list[str] = []
        audio_chunks: list[bytes] = []
        async with client.aio.live.connect(model=str(self.model), config=config) as session:
            await session.send_client_content(
                turns={"role": "user", "parts": [{"text": prompt}]},
                turn_complete=True,
            )
            async for message in session.receive():
                transcript = _extract_live_output_transcription(message)
                if transcript:
                    transcript_chunks.append(transcript)
                audio = _extract_live_audio(message)
                if audio:
                    audio_chunks.append(audio)
                server_content = getattr(message, "server_content", None)
                if server_content is not None and getattr(server_content, "turn_complete", False):
                    break

        return "".join(transcript_chunks).strip(), b"".join(audio_chunks)

    async def speak_text_live_audio(self, text: str) -> bytes:
        client = genai.Client(
            api_key=self.api_key,
            http_options={"api_version": self.live_api_version},
        )
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
                )
            ),
            system_instruction="Read the provided text aloud exactly. Do not add anything.",
            temperature=0.0,
            max_output_tokens=512,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        audio_chunks: list[bytes] = []
        async with client.aio.live.connect(model=str(self.model), config=config) as session:
            await session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True,
            )
            async for message in session.receive():
                audio = _extract_live_audio(message)
                if audio:
                    audio_chunks.append(audio)
                server_content = getattr(message, "server_content", None)
                if server_content is not None and getattr(server_content, "turn_complete", False):
                    break

        return b"".join(audio_chunks)

    def explain_part_generate(self, prompt: str) -> str:
        client = genai.Client(api_key=self.api_key)
        response = client.models.generate_content(
            model=str(self.fallback_model),
            contents=prompt + "\n\nAnswer in 1-2 complete short sentences.",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.2,
                max_output_tokens=220,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return (response.text or "").strip() or "(Gemini returned no text.)"

    def explain_part_generate_from_snapshot(self, snapshot: dict[str, Any]) -> str:
        return self.explain_part_generate(build_part_info_prompt(snapshot))


def build_part_info_prompt(snapshot: dict[str, Any]) -> str:
    compact = _compact_snapshot(snapshot)
    return (
        "Explain this selected Blender object to the user. "
        "Do not say the internal object name. Say what it is and its application.\n\n"
        "Selected object metadata:\n"
        f"{json.dumps(compact, indent=2, sort_keys=True)}"
    )


def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "name",
        "type",
        "location",
        "rotation",
        "scale",
        "dimensions",
        "visible",
        "parent",
        "children",
        "collections",
        "materials",
        "mesh",
        "world_bounding_box",
        "object_info",
        "object_info_error",
    )
    compact = {key: snapshot[key] for key in keys if key in snapshot}
    if isinstance(compact.get("children"), list) and len(compact["children"]) > 12:
        compact["children"] = compact["children"][:12]
    return compact


def _extract_live_text(message: Any) -> str:
    direct = getattr(message, "text", None)
    if direct:
        return str(direct)

    server_content = getattr(message, "server_content", None)
    model_turn = getattr(server_content, "model_turn", None) if server_content else None
    parts = getattr(model_turn, "parts", None) if model_turn else None
    if not parts:
        return ""
    return "".join(str(part.text) for part in parts if getattr(part, "text", None))


def _extract_live_audio(message: Any) -> bytes:
    server_content = getattr(message, "server_content", None)
    model_turn = getattr(server_content, "model_turn", None) if server_content else None
    parts = getattr(model_turn, "parts", None) if model_turn else None
    if not parts:
        return b""

    chunks: list[bytes] = []
    for part in parts:
        inline_data = getattr(part, "inline_data", None)
        if inline_data is None:
            continue
        data = getattr(inline_data, "data", None)
        if isinstance(data, bytes):
            chunks.append(data)
        elif isinstance(data, str):
            chunks.append(_decode_audio_data(data))
    return b"".join(chunks)


def _extract_live_output_transcription(message: Any) -> str:
    server_content = getattr(message, "server_content", None)
    transcription = getattr(server_content, "output_transcription", None) if server_content else None
    text = getattr(transcription, "text", None)
    return str(text) if text else ""


def play_pcm_audio(audio: bytes, sample_rate: int = LIVE_AUDIO_SAMPLE_RATE) -> None:
    if not audio:
        return
    player = shutil.which("afplay")
    if not player:
        raise RuntimeError("macOS afplay was not found; cannot play Gemini Live audio.")

    with SPEECH_PLAYBACK_LOCK:
        path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                path = tmp.name
            with wave.open(path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(audio)
            subprocess.run([player, path], check=True)
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def wait_for_speech_to_finish(timeout: float | None = None) -> bool:
    """Block until current speech playback finishes, then release the lock."""

    if timeout is None:
        SPEECH_PLAYBACK_LOCK.acquire()
        SPEECH_PLAYBACK_LOCK.release()
        return True
    acquired = SPEECH_PLAYBACK_LOCK.acquire(timeout=max(0.0, float(timeout)))
    if acquired:
        SPEECH_PLAYBACK_LOCK.release()
    return acquired


def _split_text_for_speech(text: str, *, max_chars: int = 220) -> list[str]:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return []

    segments: list[str] = []
    current = ""
    for sentence in _sentence_fragments(normalized):
        if not current:
            current = sentence
            continue
        if len(current) + 1 + len(sentence) <= max_chars:
            current += " " + sentence
        else:
            segments.extend(_hard_wrap_speech_segment(current, max_chars=max_chars))
            current = sentence
    if current:
        segments.extend(_hard_wrap_speech_segment(current, max_chars=max_chars))
    return segments


def _sentence_fragments(text: str) -> list[str]:
    fragments: list[str] = []
    start = 0
    for index, char in enumerate(text):
        if char in ".!?" and (index + 1 == len(text) or text[index + 1].isspace()):
            fragments.append(text[start : index + 1].strip())
            start = index + 1
    tail = text[start:].strip()
    if tail:
        fragments.append(tail)
    return fragments or [text]


def _hard_wrap_speech_segment(text: str, *, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current = ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > max_chars:
            chunks.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        chunks.append(current)
    return chunks


def _join_audio_chunks(chunks: list[bytes], *, pause_seconds: float = 0.18) -> bytes:
    audio_chunks = [chunk for chunk in chunks if chunk]
    if not audio_chunks:
        return b""
    silence = b"\x00\x00" * int(LIVE_AUDIO_SAMPLE_RATE * pause_seconds)
    return silence.join(audio_chunks)


def _decode_audio_data(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))
