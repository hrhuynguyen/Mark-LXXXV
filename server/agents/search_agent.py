"""Google Search-backed factual agent."""

from __future__ import annotations

import os

from google.adk.agents import Agent
from google.adk.tools import google_search


MODEL = os.getenv("FORGE_SEARCH_MODEL", "gemini-2.5-flash")

search_agent = Agent(
    name="search_agent",
    model=MODEL,
    description="Answers factual questions and looks up current reference facts using Google Search.",
    instruction=(
        "You answer factual questions by searching the web with Google Search.\n"
        "Always use google_search before answering.\n"
        "Return concise plain prose, usually 1-3 sentences. No markdown."
    ),
    tools=[google_search],
)
