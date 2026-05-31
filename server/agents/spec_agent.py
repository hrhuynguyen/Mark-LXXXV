"""Text-model agent that expands terse build requests into Forge Build Specs."""

from __future__ import annotations

import os

from google.adk.agents import Agent
from google.adk.tools.agent_tool import AgentTool

from server.agents.search_agent import search_agent


MODEL = os.getenv("FORGE_SPEC_MODEL", "gemini-2.5-flash")

SPEC_AGENT_INSTRUCTION = """
You expand terse 3D modeling requests into a Forge Build Spec JSON object.
Return only valid JSON. Do not wrap it in markdown.

Schema:
{
  "object": "slug_name",
  "summary": "one short sentence the voice concierge can say before building",
  "lod_budget": "low|medium|high",
  "parts": [
    {
      "name": "unique_blender_object_name",
      "generator": "cylinder|cone|cube|engine_cluster|grid_fin|landing_leg",
      "params": {"json": "serializable generator params"},
      "parent": "root_or_existing_part_name",
      "place": "origin|base|top|upper|side",
      "material": "optional_material_name"
    }
  ],
  "materials": {
    "material_name": {"base_color": [r, g, b, a], "roughness": 0.7}
  }
}

Rules:
- Default to medium LOD, about 8-15 distinct Blender objects.
- Names must be unique after count expansion and should become Blender object names.
- Use separate structural parts for things the user may point at and edit later.
- Use procedural generators first: cylinder, cone, cube, engine_cluster, grid_fin, landing_leg.
- Real-world objects should use search_agent for reference dimensions before producing the JSON.
- For Falcon 9-like rockets, include stage1_body, octaweb, grid_fin count 4,
  landing_leg count 4, interstage, stage2_body, and fairing.
- Keep all params JSON-serializable. Never include bpy objects, code, comments, or prose.
""".strip()

spec_agent = Agent(
    name="spec_agent",
    model=MODEL,
    description=(
        "Expands a terse object-building request into a JSON Forge Build Spec with "
        "named procedural parts."
    ),
    instruction=SPEC_AGENT_INSTRUCTION,
    tools=[AgentTool(agent=search_agent, skip_summarization=True)],
)
