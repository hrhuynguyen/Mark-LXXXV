# Mark 58 — Architecture & Tooling

How Gemini, Google ADK, Blender, Firebase, and the supporting tools fit together.
Solid lines = built today · dashed lines / ⏳ = planned (see `plan.md` Steps 12–15).

## System diagram

```mermaid
flowchart LR
    User(["🎙️ Voice + ✋ Hand gestures"])

    subgraph LOCAL["🖥️ Local macOS"]
        direction TB
        Client["Companion Client<br/>(PyObjC · mic 16k/24k · cursor overlay)"]
        MP["MediaPipe<br/>hand-landmark tracking"]
        subgraph BL["Blender"]
            Addon["Forge Addon<br/>TCP socket :9876"]
            BPY["bpy scene<br/>(runs on main thread)"]
        end
    end

    subgraph SERVER["☁️ Agent Server — FastAPI ⏳(Cloud Run)"]
        direction TB
        WS["WebSocket endpoint<br/>(audio · cursor · tool calls)"]
        subgraph ADK["🤖 Google ADK — multi-agent"]
            direction TB
            Concierge["concierge (router)"]
            BlenderAgent["blender_agent"]
            SpecAgent["spec_agent"]
            SearchAgent["search_agent"]
        end
        Registry["Part Registry<br/>(session state)"]
    end

    subgraph GOOGLE["Google AI / Cloud"]
        direction TB
        Gemini["✨ Gemini Live API<br/>(native audio, BIDI streaming)"]
        GSearch["🔎 Google Search<br/>(grounding)"]
        Firebase["🔥 Firebase / Firestore<br/>(persistence)"]
    end

    User -->|speech + hand video| Client
    Client --> MP
    Client <==>|"WebSocket — Seam 1"| WS
    WS <--> Concierge
    Concierge --> BlenderAgent
    Concierge --> SpecAgent
    Concierge --> SearchAgent
    SpecAgent --> BlenderAgent
    ADK <==>|"bidirectional streaming"| Gemini
    SearchAgent --> GSearch
    BlenderAgent --> Registry
    WS -->|"tool_call"| Client
    Client <==>|"JSON over TCP — Seam 2"| Addon
    Addon --> BPY
    Registry -.->|"⏳ save / restore"| Firebase
    Client -.->|"⏳ local + cloud sync"| Firebase
```

## What each tool does

| Tool | Role in Mark 58 | Status |
|---|---|---|
| **Gemini Live API** (native audio) | The realtime brain: continuous listening, natural speech in/out, barge-in interruption. Streams bidirectionally with the agent server. | ✅ Built |
| **Google ADK** | Orchestrates the multi-agent system and tool calling — `concierge` routes to `blender_agent` (modeling), `spec_agent` (terse request → Build Spec), `search_agent` (facts). | ✅ Built |
| **Gemini function/tool calling** | Bridges language → action: the model calls `execute_blender_code`, `pick_object_at`, `get_object_info`, `frame_object`. | ✅ Built |
| **Google Search** | Grounding for factual questions and reference dimensions via `search_agent`. | ✅ Built |
| **FastAPI + WebSockets** | Streams audio, cursor, and tool calls between the cloud agent and the local client (Seam 1). | ✅ Built |
| **MediaPipe** | Webcam hand-landmark tracking → fingertip cursor for pointing/picking. | ✅ Built |
| **Blender + Forge addon** | The 3D workshop. Addon hosts a TCP socket (Seam 2) and runs each command on Blender's main thread via `bpy.app.timers`. | ✅ Built |
| **PyObjC / AppKit** | Native macOS client: mic I/O, audio playback, on-screen cursor overlay sidebar. | ✅ Built |
| **Part Registry** | Per-session record of every built object so it can be inspected and re-edited. | ✅ Built |
| **Firebase / Firestore** | Persist sessions, scenes, and the Part Registry locally then to the cloud. | ⏳ Planned (Steps 12, 14) |
| **Cloud Run** | Host the agent server remotely instead of local dev. | ⏳ Planned (Step 15) |

## Example: "make this taller" (request → action)

```mermaid
sequenceDiagram
    actor User
    participant Client as Local Client
    participant Gemini as Gemini Live
    participant ADK as ADK blender_agent
    participant Blender

    User->>Client: says "make this taller" + points
    Client->>Gemini: audio stream + cursor (x,y)
    Gemini->>ADK: intent + tool calls
    ADK-->>Client: tool_call pick_object_at()
    Client->>Blender: raycast at cursor → object name
    Blender-->>ADK: hit: "landing_leg_2"
    ADK-->>Client: tool_call execute_blender_code(scale Z)
    Client->>Blender: run on main thread
    Blender-->>User: leg grows in the live viewport
    Gemini-->>User: "Done — made it taller."
```
