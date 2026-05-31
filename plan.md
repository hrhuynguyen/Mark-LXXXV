# Plan — Holographic 3D Workshop ("Forge")

> A Tony-Stark / Peter-Parker style holographic modeling workshop: speak and point at a live Blender scene; a multi-agent AI researches, procedurally builds named/editable parts, and refines them in real time.
>
> Built by fusing **Wand** (live vision + Gemini Live multi-agent orchestration — see `reference.md`) with **BlenderMCP** (programmatic Blender control — see `blender-mcp.md`).

---

## 1. Locked Decisions

| Decision | Choice | Consequence |
|---|---|---|
| **Where Blender runs** | Local app on macOS | Mirrors Wand's "local browser" model → maximum reuse of Wand's client. |
| **Primary interaction** | Hybrid: **always-on voice commands + typed fallback + hand gestures now** | Automatic voice input starts with the demo, stops after a short silence, then Gemini cleans/enriches the command and sends spoken builds directly to Text2Blender for reliability. Typed build prompts are also improved by Gemini before generation. `/mic off` pauses listening, `--voice-adk` tests the optional ADK route, and `--no-auto-voice-input` starts quiet for loud rooms. Gestures handle continuous 3D navigation. |
| **Display model (v1)** | **Real Blender window + transparent cursor overlay** | The user watches native Blender at full framerate. **No custom viewport streaming in v1.** The *model* only gets on-demand screenshots. |
| **Edit source-of-truth** | **Hybrid part registry** | Named parts + their generating params; edits apply a delta to the live object or regenerate just that one part. |
| **Geometry strategy** | **Procedural-first** | Procedural `bpy` yields a decomposed, clickable part hierarchy. AI-generation (Hyper3D/Hunyuan) and asset libraries (Sketchfab/Poly Haven) are fallbacks for complex *single* props only. |
| **Persistence** | **Local save first; Firebase later** | v1 saves `.blend` + registry JSON locally; cloud sync (Firestore + Storage + Auth) is a later phase. Registry is JSON-serializable from day one so it drops in. |
| **Orchestrator host** | **Service (local process *or* Cloud Run)** | The agent brain is an external orchestrator, not a Blender panel. For a single-machine workshop it can run locally (`--ws-url ws://127.0.0.1:8000`); only Gemini Live calls leave the machine. |
| **Prompt enrichment** | **Spec Agent → Build Spec; build now + 1-line summary; medium LOD** | A terse prompt is expanded into a structured Build Spec by a text model, then built immediately with a one-sentence spoken summary (no blocking questions). Default ~8–15 parts, voice-overridable. |

**Why procedural-first is non-negotiable for v1:** the clickable-parts requirement depends on each part being a *separate named object*. Procedural code produces exactly that; AI generators produce a single fused mesh with no sub-parts to pick (BlenderMCP's own strategy prompt even warns against generating-then-assembling parts).

---

## 2. Architecture

Three processes, all of which already exist in some form across the two reference repos:

```
┌──────────────────────────────────────────────────────────────────────┐
│ CLOUD (Google Cloud Run)            reuse: Wand app/                    │
│ FastAPI + Google ADK + Gemini Live (BIDI audio)                        │
│   concierge (native audio, router)                                     │
│     ├── search_agent      (google_search)          [reuse as-is]       │
│     └── blender_agent      (sub-agent)             [was browser_agent]  │
│           remote tools: build_part, pick_at, transform_part,           │
│                         frame_object, screenshot, generate_*           │
│   + Part Registry (session state)                                      │
└───────────────▲────────────────────────────────────────────────────┘
                │  WebSocket: PCM16 audio ⇅, 20Hz cursor →, tool_call/result ⇅
                │  (reuse Wand's session_bridge + audio_gate + reconnect)
┌───────────────┴────────────────────────────────────────────────────┐
│ LOCAL macOS — "Forge client"        reuse: Wand client/               │
│  mic / speaker (sounddevice)                                          │
│  webcam → MediaPipe → cursor + GESTURES   [extend tracker]            │
│  transparent hand-cursor OVERLAY over the Blender window [ScreenDot]  │
│  local_executor → Blender socket bridge   [lift BlenderMCP socket]    │
└───────────────▲────────────────────────────────────────────────────┘
                │  JSON over TCP (localhost:9876)   [BlenderMCP protocol]
┌───────────────┴────────────────────────────────────────────────────┐
│ LOCAL — Blender app + addon         reuse: BlenderMCP addon.py        │
│  socket server, main-thread exec (bpy.app.timers.register)           │
│  handlers: execute_code, get_scene_info, get_object_info,            │
│            get_viewport_screenshot, Hyper3D/Sketchfab/PolyHaven      │
│  + NEW: pick_object_at, get_view_geometry, orbit/zoom/pan_view,      │
│         build_named_part, set_part_transform                         │
└──────────────────────────────────────────────────────────────────────┘
```

### Key topology insight — the client is the bridge to Blender
Because Blender is **local** (behind the user's NAT), the cloud agent cannot reach it directly. So we reuse Wand's pattern exactly: **the cloud `blender_agent`'s remote tools forward a `tool_call` over the WebSocket to the local client; the client translates each into a JSON command on `localhost:9876`** (BlenderMCP's socket protocol). Blender replaces the Playwright browser as "the local resource the client owns."

This means we **reuse BlenderMCP's addon + wire protocol directly from Wand's client** and **drop BlenderMCP's FastMCP server layer** (`src/blender_mcp/server.py`) — its `BlenderConnection.send_command` / `receive_full_response` logic is lifted into the client's executor. (The standalone MCP server can still be kept for text-only/testing.)

### No browser — what's dropped vs kept
The web browser is removed entirely. **Dropped:** `browser_agent`, Playwright/Chromium, the CDP screencast, and all DOM actions (`navigate`, page `click`/`scroll`). **Kept** (none of it is browser-specific — it's general spatial/vision/voice infra): MediaPipe tracking, the homography cursor mapper + calibration, the transparent overlay, mic/speaker audio I/O + barge-in, Gemini Live + ADK orchestration, the audio gate, auto-reconnect, and the remote-tool bridge.

### "One app" launch & native feel
The Forge client **launches (or attaches to) Blender itself** with the addon's socket server already running — the user opens a single app, not "open Blender, then click Connect." The agent is an *external orchestrator driving Blender*, not a Blender panel; the experience is native because every action targets the live Blender scene via voice + point.

### Where assembly intelligence lives
BlenderMCP supplies only **primitives** (`execute_code`, `get_object_info`/AABB, GLB import). The **assembly logic lives in the `blender_agent`**: it writes a bpy *generator per part* (each emitting a named object), then an **assemble step** positions parts via AABB math (e.g. seat 9 nozzles in a ring at the body base, legs at the body radius) and **parents** them under a Collection/Empty. The registry records the resulting parent/child graph, so disassembly is just unparent / hide / move a node. (This works for *procedural* parts; AI-generated props arrive as one fused mesh and are reserved for non-decomposed single items.)

---

## 3. The Integration Seam: 2D fingertip → 3D part selection

This is the one genuinely new mechanism and the highest-risk piece. **Picking happens inside Blender via a camera ray-cast, not by matching screenshot bounding boxes.**

### 3a. Coordinate chain
```
fingertip (normalized cam coords)        ← MediaPipe landmark 8   [Wand reuse]
  → homography → Blender VIEW_3D region px  ← see 3b (direct calibration) [NEW]
  → region_2d_to_origin/vector_3d → ray     ← bpy view3d_utils       [NEW]
  → scene.ray_cast() → hit object + AABB     ← addon pick_object_at   [NEW]
```

### 3b. Simplification — calibrate straight into Blender's region space
Wand already does a guided **4-point homography calibration** (`HandCursorProvider.run_guided_calibration` in `client/cursor/provider.py`). We **reuse it but place the 4 targets at known Blender VIEW_3D region corners**, producing a direct `fingertip → region-pixel` homography. This **eliminates the need to know Blender's OS window position** on screen — the hardest part of the chain disappears. The transparent overlay (Wand's `ScreenDotOverlay`) still draws the dot at the OS-screen position for the human.

### 3c. New Blender tool: `pick_object_at`
```python
# addon.py (new handler) — runs on Blender main thread
def pick_object_at(self, region_x, region_y):
    area, region, rv3d = self._get_view3d()              # VIEW_3D area/region/RegionView3D
    coord = (region_x, region_y)
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    deps = bpy.context.evaluated_depsgraph_get()
    hit, loc, normal, idx, obj, mat = bpy.context.scene.ray_cast(deps, origin, direction)
    if not hit:
        return {"hit": False}
    return {
        "hit": True,
        "name": obj.name,
        "type": obj.type,
        "hit_location": [*loc],
        "world_bounding_box": self._get_aabb(obj),       # reuse BlenderMCP _get_aabb
    }
```
The returned `name` is the key into the **Part Registry** — the agent then reasons over *that named part*, not over pixels.

---

## 4. The Part Registry (Hybrid edit model)

**Authoritative geometry = Blender. Registry metadata = cloud session state**, keyed by Blender object name, kept in sync via tool results (analogous to Wand's `CompanionState`).

```python
# server-side session state
PartEntry = {
  "name": "landing_leg_2",          # == Blender object name (raycast returns this)
  "kind": "procedural",             # procedural | generated | library_asset
  "params": {"length": 8.0, "radius": 0.3, "count": 1},
  "generator": "build_landing_leg", # the bpy routine + args that produced it
  "parent": "stage1_assembly",      # assembly hierarchy
  "bbox": [...],
}
```

**Edit resolution:**
- *Transform edit* ("scale these legs 20%") → `set_part_transform(name, delta)` applies a delta to the live object **and** updates `params` in the registry. Fast, preserves the rest of the scene.
- *Structural edit* ("give it 6 fins not 4") → regenerate **only that part's** subtree from its `generator` + new params; leave siblings untouched.
- Assembly ops (parent/unparent, show/hide, duplicate) operate on registry nodes → emitted as `bpy` via `execute_code`.

### 4a. Persistence & Storage
Two stores for two kinds of data — but **local-first in v1**:

| Data | Nature | v1 (local) | Later (cloud) |
|---|---|---|---|
| Scene (`.blend`, or exported glTF/GLB) | large binary blob | `bpy.ops.wm.save_as_mainfile` to disk | **Firebase Storage** |
| Part registry, assembly graph, params, build history, prompts | structured JSON | JSON file on disk | **Firestore** |
| User / session identity | — | Wand's stable per-machine user ID | **Firebase Auth** |

**Design constraint for v1:** the registry must be **fully JSON-serializable from day one** (no live `bpy` object references stored — only names + params), so cloud sync is a drop-in. Firebase is the chosen cloud stack (Google-native, matches Gemini/ADK/Cloud Run; Wand's calibration mapper already has a `TODO` to persist to Firestore keyed by `(user_id, device_id)`).

---

## 5. Agent Topology (ADK)

Reuse Wand's `app/agents/` wiring nearly verbatim; swap the specialist.

- **`concierge`** (root, `gemini-*-native-audio`) — routes intent. Reused from `concierge.py`. Updated instruction: delegate modeling/pointing to `blender_agent`, factual lookups to `search_agent`.
- **`search_agent`** — unchanged (`google_search`); now also fetches *reference dimensions + images* for procedural builds.
- **`spec_agent`** (NEW; text model `gemini-2.5-flash`/`pro`, wrapped as an `AgentTool` — **not** the native-audio model) — expands a terse prompt into a structured **Build Spec** (parts manifest), grounded by `search_agent` results when the object is real. See §5a.
- **`blender_agent`** (sub-agent, native audio) — replaces `browser_agent`. Owns the modeling session. Tools (remote, bridged to local Blender):
  - `build_named_part(spec)` / `execute_blender_code(code)` — procedural creation (registers parts).
  - `pick_object_at(x, y)` — raycast selection (cursor resolved client-side at execution time, exactly like Wand's "here" actions).
  - `transform_part(name, delta)` / `regenerate_part(name, params)` — registry-driven edits.
  - `frame_object(name)`, `get_object_info(name)`, `get_viewport_screenshot()`.
  - **Fallbacks:** `generate_hyper3d_model_via_text/images`, `search_sketchfab_models` + `download_sketchfab_model`, Poly Haven HDRIs — reused straight from BlenderMCP for complex single props / environment.
- **Reused infra:** `audio_gate` (handoff stability), `ResettableLiveRequestQueue` (drops orphaned transfer responses → avoids APIError 1007), auto-reconnect + session rotation, on-demand screenshot injection for visual grounding.

**Strategy prompt** (adapt BlenderMCP's `asset_creation_strategy`): *check scene → build structural parts procedurally as named objects → only use generation/libraries for complex single props → always register parts and verify `world_bounding_box` to prevent clipping.*

### 5a. Prompt Enrichment — the Build Spec
A terse prompt ("spawn a rocket") would otherwise yield a plain primitive. The `spec_agent` expands it into a structured **Build Spec** — a parts manifest that *is* the registry seed (names, generators, params, placement, hierarchy, materials):

```jsonc
{
  "object": "falcon9",
  "summary": "SpaceX Falcon 9, ~70m, 3.7m dia, two-stage",
  "lod_budget": "medium",                 // default ~8–15 parts; voice-overridable
  "parts": [
    {"name": "stage1_body", "generator": "cylinder",       "params": {"height": 42.6, "radius": 1.83}, "parent": "falcon9",     "place": "origin"},
    {"name": "octaweb",      "generator": "engine_cluster", "params": {"count": 9, "layout": "ring+center"}, "parent": "stage1_body", "place": "base"},
    {"name": "grid_fin",     "generator": "grid_fin",       "params": {"count": 4}, "parent": "stage1_body", "place": "upper, 90° apart"},
    {"name": "landing_leg",  "generator": "landing_leg",    "params": {"count": 4, "length": 8}, "parent": "stage1_body", "place": "base, 90° apart"}
    // … interstage, stage2_body, fairing …
  ],
  "materials": {"body": "white_matte", "engines": "dark_metal"}
}
```

**Behavior (locked):**
- **Build now + 1-line summary.** No blocking questions (preserves Wand's act-first concierge style). The concierge speaks one sentence ("Falcon 9: 42 m stage-1, 9 engines, 4 legs — building now"), then `blender_agent` executes a generator per `part` and registers each. The user refines via the point+voice loop.
- **Grounding.** Real objects pull dims/images from `search_agent`; otherwise the spec uses model knowledge + sensible defaults.
- **Detail = medium (~8–15 parts) by default**, always adjustable by voice ("add more detail" / "keep it simple").
- **Model choice matters:** the spec is produced by a *text* model for better decomposition, keeping the native-audio voice loop snappy.
- **Coherence:** the Build Spec is the high-level parametric description; per-part `params` flow straight into the Part Registry (§4), so "scale the legs" and "save this build" both trace back to the same source.

---

## 6. Gesture Layer (new) — voice edits + gesture nav

Wand's tracker uses only landmark 8 (index tip). Extend it to the full 21-landmark hand(s) and a small, **mode-gated** gesture vocabulary so *pointing ≠ navigating*.

| Gesture | Signal | Action (→ Blender) |
|---|---|---|
| Point | no touch gesture | move the real cursor from ring-finger MCP |
| Thumb-index touch | handTrack adaptive touch distance | **left mouse down**; release fingers → **left mouse up** (tap = click, hold/move = drag) |
| Thumb-middle touch + vertical motion | adaptive touch distance + middle-tip delta | mouse wheel scroll → zoom |
| Thumb-ring touch + drag | adaptive touch distance | hold **middle mouse** and drag → orbit |
| Peace-sign drag | index + middle extended, ring/pinky curled | hold **Shift + middle mouse** and drag → pan |
| Open palm | four fingers extended | plain cursor movement only; avoids accidental pan |
| Thumb-pinky touch | explicit release gesture | **stop/neutral** (exit nav mode) |
| Fist | all fingers curled | **stop/neutral** (exit nav mode) |

- Implement `HandMouseCursorProvider`/`HandMouseController` consuming MediaPipe 21-landmark hands.
- Follow Google's MediaPipe Hand Landmarker task contract: run the bundled model in `VIDEO` mode, consume its 21 image landmarks, world landmarks, handedness, and confidence stream, and keep `num_hands=1` by default for latency/stability. Tune `min_hand_detection_confidence`, `min_hand_presence_confidence`, and `min_tracking_confidence` as live knobs.
- Borrow Kazuhito00's MediaPipe gesture-recognition preprocessing pattern: convert landmarks to wrist-relative normalized coordinates before classifying static hand shapes, so taps/combos are less sensitive to camera distance. Reuse handTrack's key stabilization ideas: a centered inner camera area maps to the full screen; ring-finger MCP blended with palm center is the cursor anchor; low-confidence hands release safely; drag modes require consecutive frames; tiny motion is ignored to reduce jitter.
- **Mesh edits stay voice-driven** ("scale this", "make it taller") — more reliable and unambiguous than manipulation gestures.

---

## 7. What We Reuse vs. Build

### Reuse from Wand (`reference.md`)
- `client/cursor/`: `webcam_tracker.py`, `mapper.py` (homography), `provider.py` (guided calibration), `ui_overlay.py` (`ScreenDotOverlay`), `displays.py`.
- `client/companion_runtime.py`: audio I/O (16k in / 24k out), barge-in, cursor sender (20 Hz), reconnect loop.
- `client/local_executor.py`: tool_call dispatch (retarget to Blender socket).
- `app/server.py`, `app/agents/*`, `app/live/audio_gate.py`, `app/live/resettable_queue.py`, `app/callbacks/*`, `app/runtime/session_bridge.py`.

### Reuse from BlenderMCP (`blender-mcp.md`)
- `addon.py`: socket server, `bpy.app.timers.register` main-thread dispatch, `execute_code`, `get_scene_info`, `get_object_info`, `_get_aabb`, `get_viewport_screenshot`, Hyper3D/Hunyuan/Sketchfab/Poly Haven handlers, UI panel.
- `BlenderConnection.send_command` / `receive_full_response` socket logic (lifted into the client).

### Build new
1. **Client→Blender socket bridge** inside `local_executor` (localhost:9876).
2. **`pick_object_at`** + **`get_view_geometry`** Blender handlers (raycast selection).
3. **Direct-to-region calibration** (reuse Wand calibration with Blender-region targets).
4. **Part Registry** + edit-resolution logic (server session state).
5. **`blender_agent`** + its remote tools + adapted strategy prompt.
6. **Gesture layer** (`GestureRecognizer` + `orbit/zoom/pan_view` tools).
7. **Overlay positioning** over the Blender window.

---

## 8. Milestones

### Phase 0 — De-risking spikes (proves the seam)
- **S1:** Wand client opens a socket to local Blender and round-trips an `execute_code` ("create a cube"). *Exit:* a voice/manual trigger spawns a cube in the live Blender window.
- **S2:** `pick_object_at` raycast in Blender returns the correct object name for given region coords. *Exit:* clicking a known region px selects the right mesh.
- **S3:** Direct fingertip→region homography calibration against Blender's VIEW_3D. *Exit:* pointing at a part raycasts to that part ≥90% of the time after calibration.

### Phase 1 — Procedural build loop (incl. Spec Agent)
- `concierge` + `spec_agent` (terse prompt → Build Spec) + `blender_agent`; spec drives a generator per part → **named-part hierarchy**; parts registered. One-line spoken summary, build immediately. On-demand screenshots feed the model. *Exit:* "Build a Falcon 9" → spoken summary, then named `stage1_body/octaweb/grid_fin_*/landing_leg_*` (~8–15 parts) appear in Blender.

### Phase 2 — Point-and-edit
- Cursor/selection context in Blender; selected-object Gemini Live info wired into the current terminal harness, with `pick_object_at` still planned for true fingertip raycast. *Exit:* select a leg and Forge explains what it likely is; point at a leg + "scale these up 20%" scales *only* the legs and updates the registry.
- **Reference-image refinement tool:** after selecting a part/object and getting its explanation, use `/refs part` or `/refs object` to pull a short Google image reference list for that target. The current demo opens a small thumbnail chooser; clicking an image analyzes it with Gemini vision and asks Text2Blender to refine only that selected part or the related object group. Terminal fallback remains `/use-ref N`. A hidden Blender restore point is created first, and `/undo-ref` restores the previous version if the refinement is worse. Requires `GOOGLE_CSE_ID` / `GOOGLE_SEARCH_ENGINE_ID` in `.env`.
- **Selected-part replacement:** use `/replace <new part description>` after selecting a part. Forge creates a hidden backup of the original selected part, hides that original, then prompts Text2Blender to create only a new replacement part in the same location/scale/attachment context. `/undo-replace` restores the original and removes newly generated replacement objects.
- **Selected-part add-on:** use `/add-part <new part description>` after selecting an anchor part. Forge creates a restore point while keeping the original object visible, then prompts Text2Blender to create only the new attached part near the selected anchor. `/undo-add` restores the previous object state.

### Phase 3 — Gesture navigation
- 21-landmark hand mouse controller; thumb-ring orbit / thumb-middle zoom / peace-sign pan through Blender's native mouse bindings. *Current test path:* `scripts/run_prompt_gesture_test.py` lets the user type a Text2Blender prompt while hand gestures control Blender. *Exit:* hands-only orbit/zoom/pan feels responsive (<150 ms).

- **Local persistence** lands here: save/load `.blend` + registry JSON to disk (`bpy.ops.wm.save_as_mainfile`); registry kept JSON-serializable. *Exit:* "save this build" / "reopen the Falcon 9" round-trips locally.

### Phase 4 — Generation & library fallback
- Wire Hyper3D/Hunyuan + Sketchfab/Poly Haven for complex single props and HDRI lighting; auto-place via AABB into the assembly. *Exit:* "add a detailed astronaut next to it" pulls a generated/library asset and seats it without clipping.

### Phase 5 — Cloud & polish (later / optional)
- **Firebase persistence:** Firestore (registry/metadata/history) + Storage (`.blend`/glTF blobs) + Auth; sync the local saves up, enable reload across devices and sharing.
- Continuous viewport streaming (needed only if/when going cloud/headless): GPU offscreen render loop → WebSocket/WebRTC.
- Multi-user; undo-to-parameters; length-prefixed socket framing.

---

## 9. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| **Coordinate seam (screen→region) fragility** | Direct fingertip→region calibration (3b) removes the OS-window-position unknown. Spike S3 validates early. |
| **Gesture ambiguity** (pointing vs navigating) | Explicit **mode-gate** (pinch/fist to enter/exit nav); keep mesh edits voice-only. |
| **Model can't see live video** (Gemini Live native-audio limit) | Reuse Wand's on-demand cursor-annotated screenshot injection; human watches real Blender. |
| **Blender main-thread blocking** on long generations | Reuse BlenderMCP's async **create→poll→import** pattern; keep procedural edits small. |
| **Round-trip latency / timeouts** | Small, incremental edits; 180 s socket timeout from BlenderMCP retained; dedicated low-latency nav tools mutate `RegionView3D` directly. |
| **`execute_blender_code` = arbitrary Python** | Local, single-user, user-owned machine (acceptable for v1); enforce "save before build," consider an allow-list later. |
| **AI-generated meshes are monolithic** (fight assembly) | Procedural-first default; generation reserved for single non-decomposed props. |
| **Single-client Blender socket** | Fine for one user; revisit framing + concurrency only at Phase 5. |

---

## 10. Open Questions (track, don't block)
- Exact gesture set & thresholds — tune empirically in Phase 3.
- Registry granularity: per-object vs per-sub-mesh — start per-object.
- How much "creativity" the agent gets when no reference image is found (pure-generative dimensions) vs. always grounding in search.
- Final-render path: keep the persistent app for the session; optionally use `blender --background` CLI for high-quality offline renders later.

---

## 11. Concrete Build Steps

Each step has a **Goal · Files · Do · Verify**. Steps are ordered so every one is runnable/testable before the next. Don't advance past a step whose **Verify** fails.

### Proposed repo layout (monorepo merging both forks)
```
forge/
├── addon/
│   └── forge_addon.py            # BlenderMCP addon.py + new handlers (single-file Blender addon)
├── server/                       # Wand app/ — ADK + Gemini Live (runs local in dev, Cloud Run later)
│   ├── server.py                 # FastAPI WS endpoint (Wand app/server.py)
│   ├── agents/
│   │   ├── concierge.py          # root router (reused, instruction updated)
│   │   ├── search_agent.py       # reused as-is
│   │   ├── spec_agent.py         # NEW — terse prompt → Build Spec (text model)
│   │   └── blender_agent.py      # NEW — was browser_agent
│   ├── tools/
│   │   └── remote_blender.py     # NEW — was remote_browser.py
│   ├── runtime/
│   │   ├── session_bridge.py     # reused (server↔client tool bridge)
│   │   ├── realtime_pointer.py   # reused (per-session cursor cache)
│   │   └── part_registry.py      # NEW — Build Spec + registry state
│   ├── live/                     # audio_gate.py, resettable_queue.py (reused)
│   └── callbacks/                # handoff_guard.py, echo_dedupe.py (reused)
├── client/                       # Wand client/ — local macOS
│   ├── companion_app.py          # overlay-only window (browser view removed)
│   ├── companion_runtime.py      # audio I/O, cursor sender, reconnect (reused)
│   ├── local_executor.py         # tool_call dispatch → Blender bridge
│   ├── blender_bridge.py         # NEW — lifted BlenderConnection (localhost:9876)
│   ├── blender_launcher.py       # NEW — launch/attach Blender + addon
│   ├── cursor/                   # webcam_tracker, mapper, provider, ui_overlay, displays (reused)
│   └── gestures/recognizer.py    # NEW — 21-landmark gestures
└── pyproject.toml
```

---

### Step 0 — Environment & skeleton
**Goal:** both halves run and can talk to Google.
**Do:**
1. `git init forge`; copy BlenderMCP `addon.py` → `addon/forge_addon.py`; copy Wand `app/` → `server/` and `client/` → `client/`.
2. `uv venv && source .venv/bin/activate`; install server deps (`google-adk`, `google-genai`, `fastapi`, `uvicorn`, `websockets`) and client deps (`mediapipe`, `opencv-python`, `numpy`, `sounddevice`, `pyobjc`, `websockets`).
3. Set `GOOGLE_API_KEY` in `.env` (Gemini Live).
4. Install Blender 3.0+; install `forge_addon.py` via *Edit > Preferences > Add-ons > Install*; enable it.
**Verify:** `uvicorn server.server:app --port 8000` starts; Blender shows the addon panel in the N-sidebar; `GOOGLE_API_KEY` loads.

---

### Phase 0 — De-risking spikes

#### Step 1 — Client → Blender socket round-trip (Spike S1)
**Goal:** the client can drive the live Blender scene.
**Files:** `client/blender_bridge.py` (new), throwaway `scripts/spike_cube.py`.
**Do:** lift BlenderMCP's `BlenderConnection.send_command` / `receive_full_response` into `blender_bridge.py`. Click "Connect" in Blender's panel (starts the socket server on 9876). Run a script that sends one command:
```python
# scripts/spike_cube.py
from client.blender_bridge import BlenderConnection
c = BlenderConnection("localhost", 9876); c.connect()
print(c.send_command("execute_code", {"code": "import bpy; bpy.ops.mesh.primitive_cube_add()"}))
```
**Verify:** a cube appears in the running Blender window; the call returns `{"executed": true, ...}`.

#### Step 2 — `pick_object_at` raycast (Spike S2)
**Goal:** map a 2D viewport pixel to the 3D object under it.
**Files:** `addon/forge_addon.py` (add handlers + register in the `handlers` dict).
**Do:** add `_get_view3d()` (find the `VIEW_3D` area/region/`RegionView3D`), `get_view_geometry()` (return region rect + size), and `pick_object_at()` (the §3c snippet). Register both command names.
```python
def _get_view3d(self):
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            region = next(r for r in area.regions if r.type == 'WINDOW')
            return area, region, area.spaces.active.region_3d
    raise RuntimeError("No VIEW_3D area")

def get_view_geometry(self):
    area, region, rv3d = self._get_view3d()
    return {"region": {"x": region.x, "y": region.y,
                       "width": region.width, "height": region.height}}
```
**Verify:** from a spike script, call `pick_object_at` with a region pixel known to be over the cube → returns the cube's `name` + `world_bounding_box`; an empty pixel → `{"hit": false}`.

#### Step 3 — Direct fingertip → region calibration (Spike S3)
**Goal:** pointing maps to the right region pixel without knowing Blender's OS window position.
**Files:** `client/cursor/provider.py` (adapt `run_guided_calibration`), `client/cursor/mapper.py` (reused homography).
**Do:** drive the 4 calibration targets to Blender's VIEW_3D region corners (from `get_view_geometry`), capture fingertip medians, fit the homography → `fingertip → region px`. Feed that into `pick_object_at`.
**Verify:** after calibration, pointing at the cube selects it **≥90%** of attempts; the `ScreenDotOverlay` dot visually sits on the fingertip.

> **Gate:** Steps 1–3 prove the riskiest seam. Do not build features until S3's 90% holds.

---

### Phase 1 — Procedural build loop (+ Spec Agent)

#### Step 4 — Strip the client to overlay-only
**Goal:** remove all browser code; client = mic + camera + overlay + Blender bridge.
**Files:** `client/companion_app.py`, `client/companion_runtime.py`, `client/local_executor.py`.
**Do:** delete the `BrowserView`/Playwright/CDP paths; keep the sidebar/overlay + audio + cursor sender. Point `local_executor` dispatch at `blender_bridge` instead of browser tools. Add `client/blender_launcher.py` to spawn Blender with the addon and auto-start its server.
**Verify:** launching the client opens Blender automatically, connects the socket, streams mic audio, and shows the cursor overlay — no browser anywhere.

#### Step 5 — `blender_agent` + remote tool bridge
**Goal:** the cloud/Local ADK agent executes Blender tools via the client.
**Files:** `server/adk_app.py`, `server/adk_tools.py`, `server/agents/blender_agent.py`, `server/tools/remote_blender.py`, `server/agents/concierge.py`, `client/local_executor.py`.
**Done:** added an optional Google ADK concierge (`server/adk_app.py`) and local ADK tools (`server/adk_tools.py`) that wrap the current stable actions: `generate_object`, `get_selected_object`, and `explain_selected_object`. The demo harness keeps direct Text2Blender as the default smooth path; `/adk <message>` runs one command through ADK, and `/adk on|off` toggles raw typed prompts between ADK routing and direct Text2Blender.
**Do:** rename `browser_agent` → `blender_agent`; replace its tools with `execute_blender_code`, `pick_object_at`, `get_object_info`, `get_viewport_screenshot`, `frame_object`. Each is a thin RPC stub (reuse Wand's `_call`) that the client maps to a `blender_bridge.send_command`. Update the concierge instruction to delegate modeling/pointing to `blender_agent`.
**Verify:** current path: `/adk build a cube` routes through the ADK concierge and tool layer. Later full voice path: say "add a cube" → it appears; "what's at my finger?" → screenshot + `pick_object_at` returns the right name and the agent describes it.

#### Step 6 — Part Registry
**Goal:** every created object is a tracked, JSON-serializable part.
**Files:** `server/runtime/part_registry.py`.
**Do:** implement `PartEntry` (§4) + a per-session `PartRegistry` (add/get/update/children/to_json/from_json). **Store names + params only — never live `bpy` refs.** `build_*` tools register entries; `pick_object_at` results are looked up here.
```python
@dataclass
class PartEntry:
    name: str; kind: str; params: dict
    generator: str | None = None; parent: str | None = None; bbox: list | None = None
class PartRegistry:
    def __init__(self): self.parts: dict[str, PartEntry] = {}
    def add(self, e): self.parts[e.name] = e
    def to_json(self): return {n: asdict(e) for n, e in self.parts.items()}
```
**Verify:** after a build, `registry.to_json()` round-trips through `json.dumps`/`loads` with no errors and matches the scene's object names.

#### Step 7 — Spec Agent + Text2Blender Build-Spec executor
**Goal:** terse prompt → detailed Build Spec → Text2Blender creates the object in Blender.
**Files:** `server/agents/spec_agent.py` (new), `server/build/executor.py`, `server/build/text2blender_adapter.py`, `scripts/run_prompt_gesture_test.py`, local Text2Blender checkout at `/Users/dothanhtam91/Desktop/PROJECT /Text2Blender`.
**Do:**
- `spec_agent` = `gemini-2.5-flash`/`pro`, `AgentTool`, returns the **Build Spec** JSON (§5a). Instruction: target ~8–15 parts (medium LOD), ground in `search_agent` when the object is real, emit names/generators/params/placement/parent/materials.
- In `server/build/executor.py`, convert the Build Spec into a concise Text2Blender prompt and run the local Text2Blender CLI from `/Users/dothanhtam91/Desktop/PROJECT /Text2Blender` using its standalone `.venv/bin/text2blender --prompt ...` flow. Register the resulting root object as `kind:"text2blender"`. Set `FORGE_USE_TEXT2BLENDER=0` or `spec["generator"]="procedural"` to use the older Forge procedural snippets.
- Until the full Forge server/client stack is wired, use `scripts/run_prompt_gesture_test.py` for live testing: it starts the hand gesture mouse loop and reads object prompts from the terminal, improves short typed/voice build prompts with Gemini, then calls Text2Blender's standalone CLI. Direct typed prompts remain the reliable default; `/adk <message>` and `/adk on` enable the optional ADK concierge without replacing the stable path.
- Concierge: on a build intent → call `spec_agent` → speak one-sentence summary → run executor immediately (no blocking questions).
**Verify:** `python scripts/run_prompt_gesture_test.py` → type "build a Falcon 9" → Text2Blender creates the object in Blender through the same CLI used by the standalone Text2Blender project, while hand gestures continue to move/orbit/zoom/pan Blender.

---

### Phase 2 — Point-and-edit

#### Step 8 — Contextual info on point
**Goal:** point at a part → the agent tells you what it is.
**Files:** `server/agents/blender_agent.py`, `server/agents/part_info_agent.py`, `server/tools/blender_selection.py`, `client/blender_bridge.py`, `scripts/run_prompt_gesture_test.py`, `client/cursor/ui_overlay.py` (optional label).
**Done:** current live harness watches Blender's active selected object, waits for the same object to remain selected for 3 seconds, fetches its metadata through the Text2Blender/BlenderMCP socket, and explains the part. Explanations answer "what is this?" and "what is it used for?" in 1-2 short complete sentences without reading internal object/file names aloud. Voice output is on by default; `--no-speak-info`, `/speak off`, or `/mode quiet` switch to quiet text mode for loud rooms. Gemini Live native audio output writes the returned 24 kHz PCM to a temporary WAV and plays it through macOS `afplay`; the terminal prints a complete Gemini text summary after the Live voice plays. Command input is now always-listening by default: say "Hey Travis" first, speak a command, pause, then Gemini transcribes/enriches it and sends spoken natural-language builds directly to Text2Blender. Typed build prompts also pass through the Gemini prompt improver before Text2Blender. Selected-part replacement is available with `/replace <new part description>`: the original selected part is backed up and hidden, a new replacement is generated in place, and `/undo-replace` restores the original. Selected-part add-on is available with `/add-part <new part description>`: the original object stays visible, a new attached part is generated near the selected anchor, and `/undo-add` restores the previous object state. Manual `/voice [seconds]` records without requiring the wake phrase, `/mic off` pauses automatic listening, `/mic on` resumes it, `--wake-phrase "Hey Forge"` changes the trigger, `--no-wake-phrase` disables the trigger, `--voice-adk` tests the optional ADK route, and `--no-auto-voice-input` starts with the mic paused. Demo controls are available at runtime: `/voice [seconds]` (manual one-shot), `/mic on|off`, `/speak on|off`, `/auto on|off`, `/hold <seconds>`, `/mode quiet`, `/mode voice`, `/mode silent`, `/status`, `/info`, `/add-part <prompt>`, `/replace <prompt>`, and `/build <prompt>`. Gesture status logs stay quiet unless `--gesture-debug` is used.
**Do:** wire "what's this?" → resolve cursor (client-side, like Wand "here") → `pick_object_at` → registry lookup + `get_object_info` → spoken answer; optionally render a cursor-anchored text label. Keep the selected-object monitor as the fallback path until the Forge addon has `pick_object_at`.
**Verify:** run `python scripts/run_prompt_gesture_test.py`, select a leg/object in Blender and keep it selected for 3 seconds → Gemini Live speaks the selected-part explanation and the terminal prints the explanation; `/speak off` immediately switches to quiet typed-demo mode, `/speak on` re-enables voice output, and `/info` repeats the explanation manually without waiting. Later: point at a leg + "what is this?" → "Landing leg 2, ~8 m, part of stage-1."

#### Step 9 — Registry-driven edits
**Goal:** edits hit only the targeted part.
**Files:** `server/tools/remote_blender.py` (`transform_part`, `regenerate_part`), `addon/forge_addon.py` (`set_part_transform`).
**Do:** *transform* → delta on the live object by name + update registry `params`; *structural* → re-run that part's generator with new params, leave siblings untouched.
**Verify:** point at legs + "scale these up 20%" → only legs scale, registry `params` update; "give it 6 fins" → only fins regenerate.

---

### Phase 3 — Gesture navigation

#### [~] Step 10 — handTrack-style hand mouse controller  *(code done + automated verified; live feel pending webcam tuning)*
**Goal:** control/monitor the real mouse from hand gestures, following `small-cactus/handTrack`'s interaction model.
**Files:** `client/cursor/webcam_tracker.py` (emit full 21 landmarks), `client/gestures/hand_mouse.py`, `client/companion_app.py`.
**Done:** added `HandMouseCursorProvider` and `HandMouseController`: ring-finger MCP blended with palm center controls the real cursor through a centered inner camera area; thumb-index uses the original handTrack click model (`mouseDown` while touching, `mouseUp` on release, so tap=click and hold/move=drag); thumb-middle vertical motion scrolls for zoom; thumb-ring touch holds middle mouse for Blender orbit; peace sign holds Shift+middle for pan; open palm only points to avoid accidental pan; thumb-pinky, fist, low-confidence hands, and no-hand release all buttons. The webcam tracker now exposes MediaPipe image landmarks, world landmarks, handedness, and handedness confidence so gesture distances can use world-space geometry when available. The provider reports the actual mouse position back into Forge's cursor stream; gesture status lines are quiet by default and only print when the test/client is launched with `--gesture-debug`. Automated tests pass; the current live path is `scripts/run_prompt_gesture_test.py`.
**Do:** live tune inner-area percentage, smoothing, jitter threshold, and touch/scroll sensitivity if needed.
**Verify:** real mouse follows the hand smoothly with <100 ms perceived latency; pointing/moving alone never clicks or navigates.

#### [~] Step 11 — Native Blender mouse navigation  *(code done + live socket verified; live hand feel pending Blender GUI)*
**Goal:** hands-only camera control through Blender's native mouse bindings instead of direct camera RPCs.
**Files:** `client/gestures/hand_mouse.py`, `scripts/run_prompt_gesture_test.py`, `client/companion_runtime.py`, `client/companion_app.py`.
**Done:** `scripts/run_prompt_gesture_test.py` starts the hand provider in handTrack-style mouse mode while terminal prompts call Text2Blender. The provider drives native left-click/drag, middle-drag, wheel, and Shift+middle gestures with a short consecutive-frame guard before drag modes. `--gestures` remains the intended full-client flag once `client.companion_runtime` is wired.
**Do:** live tune mouse gesture mappings in Blender GUI.
**Verify:** thumb-index touch/release clicks, thumb-index hold/move left-drags, thumb-ring drag orbits, thumb-middle vertical motion zooms, peace-sign drag pans, thumb-pinky/fist releases — responsive (<150 ms), smooth, no mesh changes.

#### Step 12 — Local persistence
**Goal:** save/reopen a build locally.
**Files:** `server/runtime/part_registry.py`, `addon/forge_addon.py` (save/load handlers).
**Do:** "save this build" → `bpy.ops.wm.save_as_mainfile` + write `registry.to_json()` beside it; "reopen X" → load both, rehydrate registry.
**Verify:** save, quit, relaunch, "reopen the Falcon 9" → scene + registry restored; parts still pick/edit correctly.

---

### Phase 4 — Generation & library fallback

#### Step 13 — Wire generation/asset tools
**Goal:** complex single props + environment.
**Files:** `server/agents/blender_agent.py` (add BlenderMCP's `generate_hyper3d_*`, `search/download_sketchfab_*`, Poly Haven HDRI tools), strategy prompt.
**Do:** keep these as **fallbacks** (registered as `kind:"generated"|"library_asset"`, single non-decomposed parts); auto-place via AABB into the assembly; HDRI → world lighting.
**Verify:** "add a detailed astronaut beside it" → generated/library asset seated without clipping; "studio lighting" → HDRI applied.

---

### Phase 5 — Cloud & polish (later / optional)

#### Step 14 — Firebase persistence
**Files:** `server/runtime/part_registry.py` (Firestore sync), storage client.
**Do:** Firestore for registry/metadata/history (keyed by `user_id`/`build_id`), Firebase Storage for `.blend`/glTF blobs, Firebase Auth for identity. Because the registry is already JSON-serializable (Step 6), this is an upload/download swap of the local save path.
**Verify:** save on machine A, reopen on machine B.

#### Step 15 — Streaming / hardening (only if going remote)
GPU-offscreen viewport stream (WebSocket/WebRTC); length-prefixed socket framing; multi-user; undo-to-parameters.

---

**Start here:** Step 0 → Step 1. The S1 cube round-trip is the foundation everything else rests on.
