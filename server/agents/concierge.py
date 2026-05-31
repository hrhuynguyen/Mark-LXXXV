"""Root native-audio concierge agent."""

from __future__ import annotations

import os

from google.adk.agents import Agent
from google.adk.tools.agent_tool import AgentTool

from server.agents.blender_agent import blender_agent
from server.agents.search_agent import search_agent
from server.callbacks.echo_dedupe import echo_dedupe_before_tool_callback
from server.callbacks.handoff_guard import transfer_audio_gate_before_tool_callback


MODEL = os.getenv("FORGE_CONCIERGE_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025")

root_agent = Agent(
    name="concierge",
    model=MODEL,
    description="Voice-first Forge concierge that routes Blender modeling and factual requests.",
    instruction=(
        "You are Forge's voice-first concierge. Always respond in English. "
        "Keep responses to 1-2 sentences.\n\n"
        "DELEGATE TO blender_agent immediately, with no summary or confirmation first, for:\n"
        "- Any Blender, 3D modeling, scene creation, object inspection, or viewport task.\n"
        "- Anything the user points at: 'here', 'this', 'right there', 'what is this?'.\n"
        "- Simple build requests like 'add a cube', 'make a rocket', or 'frame this object'.\n\n"
        "USE search_agent for standalone factual questions, especially current facts or reference dimensions.\n\n"
        "HANDLE YOURSELF:\n"
        "- Greetings, brief conversation, and non-technical acknowledgements.\n\n"
        "Never ask clarifying questions before delegating. Transfer with the full user request intact."
    ),
    before_tool_callback=[
        echo_dedupe_before_tool_callback,
        transfer_audio_gate_before_tool_callback,
    ],
    tools=[AgentTool(agent=search_agent, skip_summarization=True)],
    sub_agents=[blender_agent],
)
