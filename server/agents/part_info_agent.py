"""Gemini Live part explainer for selected Blender objects."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types

DEFAULT_LIVE_MODEL = "gemini-live-2.5-flash-preview"
DEFAULT_FALLBACK_MODEL = "gemini-2.5-flash"

SYSTEM_INSTRUCTION = """\
You are Forge's Blender part explainer.

The user has selected one object or part in Blender. Use the provided Blender
metadata to explain what the part likely is and why it matters in the model.
Keep the answer concise and useful for a builder:
- identify the part by name and likely role;
- mention visible shape/material/size/location clues from metadata;
- if the name is ambiguous, state the uncertainty instead of inventing facts;
- do not suggest unrelated edits unless the user asks.
"""


@dataclass
class GeminiLivePartInfoAgent:
    model: str | None = None
    fallback_model: str | None = None
    api_key: str | None = None
    live_api_version: str = "v1alpha"
    last_live_error: str | None = None
    live_available: bool | None = None

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

    async def explain_part_live(self, prompt: str) -> str:
        client = genai.Client(
            api_key=self.api_key,
            http_options={"api_version": self.live_api_version},
        )
        config = types.LiveConnectConfig(
            response_modalities=["TEXT"],
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.2,
            max_output_tokens=320,
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

    def explain_part_generate(self, prompt: str) -> str:
        client = genai.Client(api_key=self.api_key)
        response = client.models.generate_content(
            model=str(self.fallback_model),
            contents=prompt + "\n\nAnswer in 2-4 complete sentences.",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.2,
                max_output_tokens=512,
            ),
        )
        return (response.text or "").strip() or "(Gemini returned no text.)"


def build_part_info_prompt(snapshot: dict[str, Any]) -> str:
    compact = _compact_snapshot(snapshot)
    return (
        "Explain this selected Blender object to the user.\n\n"
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
