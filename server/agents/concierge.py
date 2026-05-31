"""Root native-audio concierge agent."""

from __future__ import annotations

import os

from google.adk.agents import Agent
from google.adk.tools.agent_tool import AgentTool

from server.agents.blender_agent import (
    blender_agent,
    build_from_spec,
    build_named_part,
)
from server.agents.search_agent import search_agent
from server.agents.spec_agent import spec_agent
from server.callbacks.echo_dedupe import echo_dedupe_before_tool_callback
from server.callbacks.handoff_guard import transfer_audio_gate_before_tool_callback
from server.tools.remote_blender import (
    execute_blender_code,
    frame_object,
    get_object_info,
    get_viewport_screenshot,
    pick_object_at,
)


MODEL = os.getenv("FORGE_CONCIERGE_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025")

root_agent = Agent(
    name="concierge",
    model=MODEL,
    description="Voice-first Forge concierge that routes Blender modeling and factual requests.",
    instruction=(
        "You are Forge's voice-first concierge. Always respond in English. "
        "Keep responses to 1-2 sentences. Forge is a digital Blender modeling app: "
        "when the user says build, make, create, add, spawn, design, decide, or model an object, "
        "they mean build a 3D model in Blender, not a physical real-world object. "
        "You are not a web-browser assistant; never describe your role as navigating the web.\n\n"
        "YOU CAN DIRECTLY CONTROL BLENDER:\n"
        "- You have Blender tools in this agent. Use them instead of only talking.\n"
        "- For build/create/make/design/decide/model requests, call spec_agent, then call build_from_spec.\n"
        "- For simple primitives like 'add a cube', call execute_blender_code directly.\n"
        "- If a transfer is uncertain, build directly here; do not refuse or discuss limitations.\n\n"
        "DELEGATE TO blender_agent immediately, with no summary or confirmation first, for:\n"
        "- Any Blender, 3D modeling, scene creation, object inspection, or viewport task.\n"
        "- Anything the user points at: 'here', 'this', 'right there', 'what is this?'.\n"
        "- Any request to build/create/make/add/spawn/design/decide/model a visible object, "
        "including 'build a rocket', 'make a Falcon 9', 'add a cube', or 'frame this object'.\n"
        "- Never answer that you cannot physically build something; instead route it as a "
        "digital Blender model request.\n\n"
        "FOR BUILD INTENTS:\n"
        "- Preferred path: call spec_agent to get the Build Spec JSON, then call build_from_spec.\n"
        "- Alternative path: transfer the original request to blender_agent immediately.\n"
        "- Do not ask clarification first; build now at medium LOD unless the user specified detail.\n\n"
        "USE search_agent for standalone factual questions, especially current facts or reference dimensions.\n\n"
        "HANDLE YOURSELF:\n"
        "- Greetings, brief conversation, and non-technical acknowledgements.\n\n"
        "Never ask clarifying questions before delegating. Transfer with the full user request intact."
    ),
    before_tool_callback=[
        echo_dedupe_before_tool_callback,
        transfer_audio_gate_before_tool_callback,
    ],
    tools=[
        AgentTool(agent=search_agent, skip_summarization=True),
        AgentTool(agent=spec_agent, skip_summarization=True),
        build_from_spec,
        build_named_part,
        execute_blender_code,
        pick_object_at,
        get_object_info,
        get_viewport_screenshot,
        frame_object,
    ],
    sub_agents=[blender_agent],
)
