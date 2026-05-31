# Forge — a Holographic 3D Workshop

> Speak and point at a live Blender scene; a multi-agent AI researches, procedurally
> builds **named, individually-editable parts**, and refines them in real time.
> A "Tony Stark / Peter Parker workshop" built by fusing real-time computer vision,
> multi-agent orchestration (Gemini Live), and programmatic 3D modeling (Blender).

> **Status:** 🚧 Scaffold. The repo structure, design docs, and build plan are in place;
> implementation follows `plan.md` step by step. Most modules are stubs today.

---

## What it is

You say *"build a Falcon 9"* and point at the screen. Forge:

1. **Expands** the terse prompt into a structured **Build Spec** (a parts manifest).
2. **Researches** real dimensions/images when the object is real (Google Search).
3. **Builds procedurally** in a live Blender window — each part a separate, named, clickable object.
4. Lets you **point at any part** and ask about it or edit it by voice ("scale these legs up 20%"),
   and **navigate** the view with hand gestures (orbit / zoom / pan).

It's built on two foundations (analyzed in `reference.md` and `blender-mcp.md`):

- **Wand** → live vision + hand tracking + Gemini Live multi-agent orchestration.
- **BlenderMCP** → programmatic Blender control over a JSON socket.

The web browser from Wand is removed entirely; Blender takes the browser's place as the
local app the client drives.

---

## Architecture (three processes)

```
CLOUD or LOCAL — ADK + Gemini Live (server/)
  concierge (native audio router)
    ├── search_agent   (google_search — reference dims/images)
    ├── spec_agent     (text model — terse prompt → Build Spec)
    └── blender_agent  (generator library + Build-Spec executor + remote tools)
        + Part Registry (names + params, JSON-serializable)
        │  WebSocket: PCM16 audio ⇅, 20Hz cursor →, tool_call/result ⇅
LOCAL macOS — Forge client (client/)
  mic / speaker · webcam → MediaPipe → cursor + gestures · transparent overlay
  local_executor → blender_bridge
        │  JSON over TCP (localhost:9876)
LOCAL — Blender app + Forge addon (addon/forge_addon.py)
  socket server · main-thread bpy exec · pick_object_at (raycast) · orbit/zoom/pan
```

The cloud agent can't reach Blender directly (it's behind your NAT), so **the client is the
bridge**: the `blender_agent`'s remote tools send `tool_call`s to the client, which forwards
them to the local Blender socket. See `plan.md` §2.

---

## Repo layout

```
addon/forge_addon.py     Blender addon: socket server + handlers (from BlenderMCP)
server/                  ADK + Gemini Live orchestrator (from Wand app/)
  agents/                concierge · search_agent · spec_agent · blender_agent
  tools/remote_blender   remote tool stubs bridged to local Blender
  runtime/               session_bridge · realtime_pointer · part_registry
  live/ · callbacks/      audio gate, queue, handoff guard (reused infra)
client/                  local macOS app (from Wand client/)
  companion_app          overlay-only window; launches Blender
  companion_runtime      audio I/O, cursor sender, reconnect
  local_executor         tool_call dispatch → blender_bridge
  blender_bridge         socket client to localhost:9876
  blender_launcher       launch/attach Blender + addon
  cursor/                MediaPipe tracker, homography mapper, calibration, overlay
  gestures/recognizer    21-landmark gesture recognizer
scripts/spike_cube.py    Spike S1: the first client→Blender round-trip
```

Design docs live at the repo root:
- **`plan.md`** — the build plan: locked decisions, architecture, and concrete step-by-step build (§11).
- **`reference.md`** — analysis of Wand (vision + Gemini Live).
- **`blender-mcp.md`** — analysis of BlenderMCP (Blender control).

---

## Prerequisites

- **macOS** (v1 is macOS-only — the client uses native Cocoa via pyobjc)
- **Blender 3.0+**
- **Python 3.10+** and [`uv`](https://docs.astral.sh/uv/) (`brew install uv`)
- A **Gemini API key** (`GOOGLE_API_KEY`)
- Webcam + microphone (use **headphones** — see note below)

---

## Setup

```bash
# 1. Install dependencies
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# 2. Configure
cp .env.example .env        # then add your GOOGLE_API_KEY

# 3. Install the Blender addon
#    Blender > Edit > Preferences > Add-ons > Install... > select addon/forge_addon.py
#    Enable it, open the "Forge" tab in the 3D View sidebar (press N), click "Connect".
```

## Run (local dev)

```bash
# Terminal 1 — orchestrator (ADK + Gemini Live)
uvicorn server.server:app --host 127.0.0.1 --port 8000

# Terminal 2 — Forge client (launches Blender, mic, camera, overlay)
python -m client.companion_app --ws-url ws://127.0.0.1:8000/ws
```

> **Headphones recommended.** Like Wand, open-speaker audio causes echo loops with the
> live barge-in microphone; headphones fix it at the physical layer.

### First milestone (smoke test)
With Blender open and "Connect" clicked:

```bash
python scripts/spike_cube.py   # Spike S1 — should spawn a cube in the live Blender window
```

---

## Where to start building

Follow **`plan.md` §11 (Concrete Build Steps)** in order. The first gate is **Steps 1–3**
(socket round-trip → raycast pick → fingertip→region calibration) — don't build features
until pointing selects the right part ≥90% of the time.

## Key design rules (don't violate)

- **Procedural-first.** Build structural parts as separate named objects; AI generation
  (Hyper3D/Hunyuan) and asset libraries are fallbacks for complex *single* props only.
- **Registry stores names + params, never live `bpy` references** — this is what keeps
  "scale this leg" (needs the live object) and "save this build" (needs pure JSON) from
  fighting, and makes the eventual Firebase swap a drop-in.
- **Picking happens via a Blender camera raycast**, not by matching 2D screenshot boxes.

---

## License

MIT. Forge is a third-party integration and is not affiliated with Blender, Google, or the
upstream Wand / BlenderMCP projects.
