Inspiration:
This project is inspired by the scene in Spider-Man: Far From Home where Peter makes his own new Spider-Man suit. It is a "Tony Stark / Peter Parker holographic workshop" created by combining real-time computer vision, multi-agent AI orchestration, and programmatic 3D modeling.
By marrying a live vision/interaction agent (Wand - reference.md) with a headless or API-driven 3D engine (Blender via MCP/CLI - Blender-MCP.md), you can bypass traditional, complex CAD UI barriers and let users build via natural language and spatial gestures.
Here is a structured breakdown to polish this concept, map out the technical architecture, and deeply explore how the hand commands can work.
Architectural Overview: How It Fits Together
The Orchestrator (Gemini Live API): Maintains a live audio/visual websocket session. It listens to the user's intent ("Build a Falcon 9 rocket") and acts as the brain, spawning sub-agents.
The Research & Execution Agents: One agent uses web search (through Google ADK and Google Search) to find reference blueprints, dimensions, and images. Another translates these specifications into Python scripts (bpy—Blender Python API).
The Spatial Control Layer (Wand + Vision): Tracks the user's hand in 3D or 2D space, converting physical gestures into viewport navigation (pan, zoom, rotate) and mesh manipulations (extrude, scale).
Designing the Hand Command System
Because our goal is to replace the traditional mouse and keyboard shortcut-heavy Blender workflow, the hand-tracking system needs to be intuitive, low-latency, and map cleanly to Blender's internal state. The Wand project (reference.md) is perfectly fine. Let's reuse the code if needed and add some more commands like click, scroll, rotate, etc.
Refining the System Workflow
Here is exactly how a single user request moves through this proposed application:
Step 1: Intent & Research Phase
User: "Gemini, let's design a SpaceX Falcon 9 rocket."
Gemini Orchestrator: Recognizes the high-level intent. It dispatches a Search Agent (if needed) via Google Search/Vision toolcards to pull authentic mechanical drawings, diameter ratios, and high-res images of the Falcon 9. Otherwise, it uses Gemini’s creativity to generate them.
Important Note: Blender MUST render multiple parts of an item and assemble them together. Since the user wants to click on the parts, re-design them, or move them around, it must have assembly and disassembly functions.
Step 2: Code Generation & Blender-CLI Render (reuse the Blender-CLI repo I gave you)
Blender Agent: Receives the structural dimensions (e.g., height, diameter, number of Merlin engines) from the Search Agent.
Execution: The agent writes a programmatic Python script using Blender's bpy module to procedurally generate the base cylinders, rocket nozzles, and grid fins. It pushes this script to Blender via CLI or an open MCP server.
Streaming Feed: The live-rendered viewport stream is sent back to the user's interface in real-time (using WebRTC or an optimized WebSocket frame stream similar to how Wand pipes screen state).
Step 3: Interactive Refinement Loop
User: Points at the base of the rocket using the hand-cursor and says, "Let's scale up these landing legs a bit." / Points at a part and asks the agent information about that part.
Vision Layer: Calculates the on-screen pixel coordinates of the user's finger, matching it to the 3D bounding box of the landing leg meshes in Blender.
Gemini Agent: Detects the verbal modification command, rewrites the localized scaling parameter in the Python block, and updates the active Blender instance instantly.
