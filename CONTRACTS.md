# CONTRACTS.md — Forge frozen interfaces

> The two seams that decouple the work tracks. **Agree here first; change only by PR + version bump.**
> Person A (Interaction/Client) and Person B (Intelligence/Modeling) both code against this doc.
> See `plan.md` for context; `reference.md` (Wand) and `blender-mcp.md` (BlenderMCP) for the reused code.

```
Person B ── server ──┐                          ┌── Person A ── client ──┐                 ┌── shared ──┐
                     │  Seam 1: WebSocket        │                        │  Seam 2: TCP    │
   blender_agent  ───┼──  tool_call / result  ───┤  local_executor   ─────┼── JSON cmd  ────┤  Blender   │
   part_registry     │  audio · cursor · control │  blender_bridge        │  (localhost)    │  addon     │
                     └───────────────────────────┘                        └─────────────────┘            │
   shared data shapes (Build Spec, PartEntry) live in §5 and are owned jointly                  ─────────┘
```

**`PROTOCOL_VERSION = "0.1.0"`** — sent in the session handshake (§2.1) and the socket `hello` (§3.1). Bump on any breaking change to a message/command/schema below.

---

## 1. Ownership & rules

| Seam / schema | Producer | Consumer | Owner of the contract |
|---|---|---|---|
| §2 WebSocket messages | both | both | co-owned (frozen) |
| §3 Blender socket commands | client (A) | addon | co-owned (A leads spatial cmds, B leads modeling cmds) |
| §4 Tool catalog (the mapping) | server (B) → client (A) → addon | — | co-owned (the critical table) |
| §5 Build Spec | `spec_agent` (B) | `blender_agent` (B) | B, but shape frozen here |
| §5 PartEntry / Registry | server (B) | server (B), surfaced to A via results | B, but shape frozen here |

**Rules**
1. **Additive changes are free** (new optional fields, new commands). **Breaking changes** (rename/remove/retype a field, change semantics) require a PR that bumps `PROTOCOL_VERSION` and updates this file.
2. Unknown fields **must be ignored**, not rejected (forward-compatibility).
3. All JSON is UTF-8. All coordinates are **integers** unless stated. Angles in **radians**. Lengths in **Blender units (meters)**.
4. A receiver that can't satisfy a request returns the **error shape** for its seam (§2.4, §3.3) — it never silently drops.

---

## 2. Seam 1 — Server ↔ Client (WebSocket)

Reused verbatim from Wand (`app/runtime/session_bridge.py`, `client/companion_runtime.py`). URL carries identity: `…/ws/{user_id}/{session_id}`.

### 2.1 Frames
| Direction | Frame | Payload |
|---|---|---|
| both | **binary** | PCM16 audio. Client→server: 16 kHz mono. Server→client: 24 kHz mono. |
| both | **text** | JSON control/data messages (below), each a single complete JSON object. |

### 2.2 Client → Server (text)
```jsonc
// cursor stream, ~20 Hz (Wand). x,y are SCREEN px (for the overlay/human).
{"type": "cursor", "x": 1024, "y": 540, "source": "hand", "ts": 1717000000.123}

// tool result (reply to a tool_call)
{"type": "tool_result", "call_id": "uuid", "ok": true,  "result": { /* tool-specific */ }}
{"type": "tool_result", "call_id": "uuid", "ok": false, "error": "human-readable message"}

// client trace (optional telemetry/UI) — event ∈ a fixed allow-list (Wand)
{"type": "client_trace", "source": "client", "event": "tool_result_received", "...": "..."}
```

### 2.3 Server → Client (text)
```jsonc
// session handshake (first message)
{"type": "session_meta", "session_id": "…", "protocol_version": "0.1.0", "service": "…"}

// tool call — the agent asks the client to do something locally
{"type": "tool_call", "call_id": "uuid", "tool": "pick_object_at", "args": { /* see §4 */ }}

// barge-in: clear local audio playback immediately
{"type": "interrupt"}

// audio gate around agent handoffs (Wand) — client pauses/restores mic upload
{"type": "audio_gate", "state": "closed", "reason": "transfer_to_agent"}
{"type": "audio_gate", "state": "open",   "reason": "transfer_to_agent"}

// trace/cursor-ack events (optional, for UI)
{"type": "trace_event", "event": "…", "status": "ok", "summary": "…"}
```

### 2.4 Tool-result contract (the important one)
- Every `tool_call` is answered by **exactly one** `tool_result` with the same `call_id`.
- `ok:true` → `result` is the tool's payload (§4). `ok:false` → `error` is a string.
- The server enforces a **20 s timeout** per call (Wand `call_tool`); on timeout it raises and may rotate the session. Long Blender work (generation) must use the **async create→poll→import** tools, not one blocking call.
- **`call_id`** is a UUID minted by the server; the client must echo it unchanged.

---

## 3. Seam 2 — Client ↔ Blender addon (TCP, localhost:9876)

Reused from BlenderMCP (`addon.py` socket server; `BlenderConnection.send_command`/`receive_full_response` lifted into `client/blender_bridge.py`).

### 3.1 Wire format
- **No length framing.** A message is complete when the accumulated bytes parse as one valid JSON object (BlenderMCP behavior). Keep payloads reasonable; large blobs (screenshots) go via temp file (§4) or base64.
- Default host/port: `BLENDER_HOST=localhost`, `BLENDER_PORT=9876` (env-overridable).
- The addon executes every command on **Blender's main thread** via `bpy.app.timers.register` — handlers must be main-thread-safe.

### 3.2 Request
```jsonc
{"type": "<command>", "params": { /* command-specific */ }}
```

### 3.3 Response
```jsonc
{"status": "success", "result": { /* command-specific */ }}
{"status": "error",   "message": "human-readable message"}
```
- `client/blender_bridge.py` raises on `status:"error"` and returns `result` on success (BlenderMCP semantics).
- **A "miss" is not an error.** `pick_object_at` over empty space returns `{"status":"success","result":{"hit":false}}`.

### 3.4 Command set
**Reused from BlenderMCP (unchanged):** `execute_code`, `get_scene_info`, `get_object_info`, `get_viewport_screenshot`, `get_polyhaven_status`/`search`/`download`, `create_rodin_job`/`poll_rodin_job_status`/`import_generated_asset`, Hunyuan & Sketchfab equivalents.

**New for Forge (`addon/forge_addon.py`):**

| Command | params | result | Step |
|---|---|---|---|
| `get_view_geometry` | `{}` | `{"region": {"x","y","width","height"}, "is_perspective", "view_distance"}` | 2 |
| `get_window_geometry` | `{}` | `{"window": {"x","y","width","height"} (pts), "region": {"x","y","width","height"} (px), "pixel_size"}` | 3 |
| `pick_object_at` | `{"region_x": int, "region_y": int}` | `{"hit": bool, "name"?, "type"?, "hit_location"?: [x,y,z], "hit_normal"?, "world_bounding_box"?: [[minx,miny,minz],[maxx,maxy,maxz]]}` | 2 |
| `set_part_transform` | `{"name": str, "delta": Delta}` | `{"name","location":[x,y,z],"rotation":[x,y,z],"scale":[x,y,z],"world_bounding_box"}` | 9 |
| `orbit_view` | `{"d_azimuth": float, "d_elevation": float}` (radians) | `{"ok": true}` | 11 |
| `zoom_view` | `{"factor": float}` (>1 = closer) | `{"ok": true}` | 11 |
| `pan_view` | `{"dx": float, "dy": float}` (region px) | `{"ok": true}` | 11 |
| `frame_object` | `{"name": str}` | `{"ok": true}` | 8 |
| `save_build` | `{"path": str}` | `{"ok": true, "path": str}` | 12 |
| `load_build` | `{"path": str}` | `{"ok": true, "scene_objects": [str]}` | 12 |

`Delta` = `{"translate"?: [dx,dy,dz], "rotate"?: [rx,ry,rz], "scale"?: number | [sx,sy,sz]}`. Missing keys = no change. `scale` as a single number = uniform.

---

## 4. Tool catalog — the mapping (server tool → client → Blender command)

This is the table that lets A and B work apart. Each row: the **WS tool** name+args the `blender_agent` emits (B), how `local_executor` maps it (A), and the `result` returned to the agent.

| WS tool (`args`) | Cursor-injected? | Blender command | `result` to agent |
|---|---|---|---|
| `execute_blender_code` `{code}` | no | `execute_code {code}` | `{"executed":true,"result":"<stdout>"}` |
| `build_part` `{part}` (one Build-Spec part, §5) | no | `execute_code {code}` (generator output) | `{"name","kind","world_bounding_box"}` |
| `pick_object_at` `{}` | **YES** | `pick_object_at {region_x,region_y}` | `{"hit",...}` (§3.4) |
| `transform_part` `{name, delta}` | no | `set_part_transform {name,delta}` | transform + bbox (§3.4) |
| `regenerate_part` `{name, params}` | no | `execute_code {code}` (re-run generator) | `{"name","world_bounding_box"}` |
| `get_object_info` `{name}` | no | `get_object_info {name}` | object info (BlenderMCP shape) |
| `get_viewport_screenshot` `{max_size?=800}` | no | `get_viewport_screenshot {max_size,filepath,format}` | MCP `Image` (PNG) |
| `frame_object` `{name}` | no | `frame_object {name}` | `{"ok":true}` |
| `orbit_view`/`zoom_view`/`pan_view` `{delta}` | no | same (§3.4) | `{"ok":true}` |
| `save_build`/`load_build` `{...}` | no | same (§3.4) | `{"ok":true,...}` |
| `generate_hyper3d_model_via_text/images`, `poll_rodin_job_status`, `import_generated_asset`, `search/download_sketchfab_model`, Poly Haven | no | BlenderMCP commands (unchanged) | BlenderMCP shapes |

### 4.1 Cursor injection (the Wand "here" pattern — pin this down)
Tools marked **Cursor-injected** carry **no coordinates from the server**. At execution time the **client** (A):
1. takes the latest cursor sample,
2. maps it through the calibrated **fingertip→region homography** (Step 3),
3. fills `region_x, region_y`, and
4. sends the Blender command.

So `blender_agent` calls `pick_object_at` with `{}`; the *client* decides "where." This mirrors Wand's `click_here`/`scroll_here`. If no calibration or no recent cursor: return `tool_result {ok:false, error:"no calibrated cursor"}`.

### 4.2 End-to-end example: "scale this leg up 20%"
```
1. user points + says it          → Gemini Live (server)
2. blender_agent →  tool_call {tool:"pick_object_at", args:{}}            [Seam 1]
3. client injects cursor → region (412, 287)                              [§4.1]
   blender_bridge →  {"type":"pick_object_at","params":{"region_x":412,"region_y":287}}  [Seam 2]
4. addon raycast → {"status":"success","result":{"hit":true,"name":"landing_leg_2","world_bounding_box":[...]}}
5. client →  tool_result {call_id, ok:true, result:{hit:true, name:"landing_leg_2", ...}}  [Seam 1]
6. server looks up "landing_leg_2" in PartRegistry (§5)
   blender_agent →  tool_call {tool:"transform_part", args:{name:"landing_leg_2", delta:{scale:1.2}}}
7. client →  {"type":"set_part_transform","params":{"name":"landing_leg_2","delta":{"scale":1.2}}}
8. addon scales live object → returns transform+bbox
9. server updates registry params + bbox; agent speaks "done."
```

---

## 5. Shared data schemas

### 5.1 Build Spec (output of `spec_agent`, input to the executor) — `plan.md` §5a
```jsonc
{
  "object": "falcon9",                       // slug; basis for object names
  "summary": "SpaceX Falcon 9, ~70m...",     // one line; concierge speaks this
  "lod_budget": "medium",                    // "low" | "medium" | "high" (~8–15 parts default)
  "parts": [
    {
      "name": "stage1_body",                 // unique within the build == Blender object name
      "generator": "cylinder",               // key into the generator library
      "params": {"height": 42.6, "radius": 1.83},
      "parent": "falcon9",                    // assembly graph; "falcon9" = root collection/empty
      "place": "origin",                      // human-readable placement hint for the executor
      "material": "white_matte"               // optional
    }
    // …more parts…
  ],
  "materials": {"white_matte": {"base_color": [0.9,0.9,0.9], "roughness": 0.8}}  // optional palette
}
```
**Required per part:** `name`, `generator`, `params`, `parent`. `name` values must be unique and become the Blender object names (so `pick_object_at` → registry lookup works).

### 5.2 PartEntry / Registry (server session state) — `plan.md` §4, Step 6
```jsonc
// PartEntry — keyed by Blender object name. STORE NAMES + PARAMS ONLY, never live bpy refs.
{
  "name": "landing_leg_2",
  "kind": "procedural",                      // "procedural" | "generated" | "library_asset"
  "params": {"length": 8.0, "radius": 0.3, "count": 1},
  "generator": "build_landing_leg",          // null for generated/library kinds
  "parent": "stage1_assembly",               // matches Build Spec parent graph
  "bbox": [[minx,miny,minz],[maxx,maxy,maxz]]  // last known world AABB; refreshed on edit
}

// Registry (whole build) — the JSON that gets saved (Step 12) / synced to Firestore (Step 14)
{
  "build_id": "…",
  "object": "falcon9",
  "protocol_version": "0.1.0",
  "parts": { "stage1_body": {…PartEntry}, "landing_leg_2": {…PartEntry}, … }
}
```
**Invariant (load-bearing):** a `PartEntry` is **pure JSON** — no `bpy` objects, no callables. This is what lets "scale this leg" (resolve live object by `name`) and "save/sync this build" (dump JSON) coexist, and makes Firebase (Step 14) a drop-in.

---

## 6. Coordinate conventions (get these exactly right — the pick seam depends on it)

| Space | Origin | +X | +Y | Units | Where |
|---|---|---|---|---|---|
| Camera-normalized (MediaPipe) | top-left | right | down | [0,1] | tracker output |
| Screen px (overlay/human) | top-left | right | down | px | cursor stream §2.2 |
| **Blender region px** | **bottom-left** | right | **up** | px | `pick_object_at`, `pan_view` args |
| World | scene origin | — | — | meters | bbox, hit_location |

- The client's **calibration homography maps camera-normalized → Blender region px directly** (Step 3b), so it bakes in the screen↔region flip. Everyone downstream of the client uses **region px** (bottom-left origin).
- `get_view_geometry.region` is in Blender-window coords; used only during calibration to know the target corners.
- `world_bounding_box` is `[[min],[max]]` in world meters (BlenderMCP `_get_aabb`).

---

## 7. Errors, timeouts, identity

- **Blender → client:** `{"status":"error","message":…}`. `blender_bridge` raises; `local_executor` returns `tool_result {ok:false, error:message}`.
- **Socket timeout:** 180 s (BlenderMCP). On timeout the client invalidates the socket and returns `ok:false`.
- **WS tool timeout:** 20 s (Wand). Keep individual tools fast; offload long work to async generation tools.
- **Miss ≠ error:** `pick_object_at` empty space → `ok:true, result:{hit:false}`.
- **Identity:** stable per-machine `user_id` (Wand `get_stable_user_id`), rotating `session_id` per connection. Registry `build_id` is independent of session.

---

## 8. Change process
1. Propose in a PR that edits this file and bumps `PROTOCOL_VERSION` for breaking changes.
2. Both track owners approve.
3. Server logs the negotiated `protocol_version` at connect; mismatch → warn (v0.x) / refuse (v1+).

**Frozen for parallel work as of `PROTOCOL_VERSION 0.1.0`.**
