"""Optional Google ADK concierge for Forge's local demo workflow."""

from __future__ import annotations

import os

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types

from server.adk_tools import (
    explain_selected_object,
    generate_object,
    get_selected_object,
)

APP_NAME = "forge"
DEFAULT_ADK_MODEL = "gemini-2.5-flash"

FORGE_CONCIERGE_INSTRUCTION = """\
You are Forge's demo-safe Blender concierge.

You help the user control Blender through local tools. Keep responses short.
Important:
- For build/create/generate requests, call generate_object with the user's full prompt.
- For "what is this?", "what is selected?", or part questions, call explain_selected_object.
- For selection metadata/debug questions, call get_selected_object.
- Do not claim an object was created or inspected unless a tool confirms it.
- Do not mention internal implementation unless asked.
- The room may be noisy, so typed commands are first-class.
"""


def create_forge_agent(model: str | None = None) -> Agent:
    """Create the optional ADK agent without starting any runner/session."""

    return Agent(
        name="forge_concierge",
        model=model or os.getenv("FORGE_ADK_MODEL", DEFAULT_ADK_MODEL),
        instruction=FORGE_CONCIERGE_INSTRUCTION,
        tools=[generate_object, get_selected_object, explain_selected_object],
        generate_content_config=types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=512,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )


def create_runner(agent: Agent | None = None) -> InMemoryRunner:
    return InMemoryRunner(agent=agent or create_forge_agent(), app_name=APP_NAME)


def run_forge_adk_text(
    message: str,
    *,
    user_id: str = "local-demo-user",
    session_id: str = "local-demo-session",
    runner: InMemoryRunner | None = None,
) -> str:
    """Run one typed message through ADK and return the final response text."""

    if not message.strip():
        return ""
    runner = runner or create_runner()
    if runner.session_service.get_session_sync(
        app_name=runner.app_name,
        user_id=user_id,
        session_id=session_id,
    ) is None:
        runner.session_service.create_session_sync(
            app_name=runner.app_name,
            user_id=user_id,
            session_id=session_id,
        )

    user_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=message)],
    )
    final_text = ""
    last_text = ""
    for event in runner.run(
        user_id=user_id,
        session_id=session_id,
        new_message=user_message,
    ):
        text = _event_text(event)
        if text:
            last_text = text
        if hasattr(event, "is_final_response") and event.is_final_response():
            final_text = text or last_text
    return final_text or last_text


def _event_text(event) -> str:
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) if content else None
    if not parts:
        return ""
    return "".join(str(part.text) for part in parts if getattr(part, "text", None))
