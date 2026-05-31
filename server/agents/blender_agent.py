"""Native-audio modeling agent that controls local Blender through remote tools."""

from __future__ import annotations

import os

from google.adk.agents import Agent

from server.callbacks.echo_dedupe import echo_dedupe_before_tool_callback
from server.callbacks.handoff_guard import transfer_audio_gate_before_tool_callback
from server.tools.remote_blender import (
    execute_blender_code,
    frame_object,
    get_object_info,
    get_viewport_screenshot,
    pick_object_at,
)


MODEL = os.getenv("FORGE_BLENDER_MODEL", "gemini-2.5-flash-native-audio-latest")

blender_agent = Agent(
    name="blender_agent",
    model=MODEL,
    description=(
        "Controls the user's local Blender scene through a remote tool bridge. "
        "Supports pointing via the user's calibrated hand cursor."
    ),
    instruction=(
        "You control the user's local Blender scene. The user can point with a hand cursor.\n"
        "'Here', 'this', 'right there', and 'this part' refer to the current cursor position.\n"
        "Keep spoken responses to one short sentence. Act first, confirm briefly after.\n\n"
        "TOOLS:\n"
        "- execute_blender_code(code): run Python inside Blender for simple scene edits.\n"
        "- pick_object_at(): raycast at the user's current calibrated cursor.\n"
        "- get_object_info(name): inspect a named Blender object.\n"
        "- get_viewport_screenshot(max_size=800): capture the current Blender viewport.\n"
        "- frame_object(name): frame a named object in the viewport.\n\n"
        "STEP-5 BEHAVIOR:\n"
        "- For 'add a cube' or other simple primitive requests, use execute_blender_code immediately.\n"
        "- For 'what is this?' or pointed questions, call pick_object_at first, then get_object_info if it hits.\n"
        "- If a pick misses, say you do not have a part under the cursor and ask the user to point again.\n"
        "- Name created objects clearly. Use separate Blender objects when there are distinct parts.\n"
        "- Do not claim registry-backed part editing is available yet; that arrives in the next steps.\n\n"
        "TRANSFER TO CONCIERGE:\n"
        "- Pure factual questions with no Blender task -> transfer_to_agent('concierge').\n"
        "- Pure conversation -> transfer_to_agent('concierge')."
    ),
    before_tool_callback=[
        echo_dedupe_before_tool_callback,
        transfer_audio_gate_before_tool_callback,
    ],
    tools=[
        execute_blender_code,
        pick_object_at,
        get_object_info,
        get_viewport_screenshot,
        frame_object,
    ],
)
