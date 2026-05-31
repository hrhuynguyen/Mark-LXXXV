# Wand — Reference Analysis

> A reference document analyzing **Wand: A Live Agent That Sees, Browses, and Clicks With You**, built on the Gemini Live API.
>
> - **GitHub:** https://github.com/fuyuan-li/gemini_live_agent
> - **Devpost:** https://devpost.com/software/wand-a-live-agent-that-sees-browses-and-clicks-with-you
> - **Recognition:** Winner — *Best Multimodal Integration & User Experience*, Gemini Live Agent Challenge.

---

## App Summary

**Wand** is a voice-first, pointer-aware AI agent that lets a user operate a web browser entirely through natural speech and hand gestures — **no typing, no mouse**. The user speaks naturally ("open Google Maps", "play this", "what is this?") and points at the screen with their index finger; a webcam tracks the finger, and the agent executes the corresponding browser action at the location the user is pointing at.

Its primary purpose is to close the gap between modern AI's multimodal capabilities (speech, vision, context) and the still keyboard-and-mouse-bound interfaces we use to drive computers. The core question the project poses is: *"What if you could just point at something on screen and say what you want?"*

Target use cases:

- **Hands-busy situations** — cooking, presenting, working with tools.
- **Accessibility** — elderly users, people with limited hand mobility.
- **Tasks where pointing is faster than describing** — shopping, research, maps.

Architecturally, Wand is a **split-deployment system**: a cloud server (Google Cloud Run) runs all agent orchestration and Gemini Live inference, while a local macOS client owns the microphone, speaker, webcam, hand tracking, and an embedded Playwright/Chromium browser. The two halves communicate over a single persistent WebSocket.

---

## Core Abilities

- **Universal browser control** — works across any common website: maps, video streaming (YouTube), online shopping (Amazon), news, and general browsing. It can act on any page element: media players, images, buttons, links, and figures.
- **Voice navigation & actions** — navigate, search, scroll, zoom, pan, click, drag, go back, and play/pause, all by speaking naturally. The agent infers intent and acts immediately (act-first, confirm-briefly-after).
- **Hand pointer / "here" actions** — a webcam tracks the index fingertip via **MediaPipe**. Saying *"click here"*, *"scroll here"*, *"zoom in here"*, or *"drag this"* acts on whatever the user is physically pointing at on screen.
- **Visual understanding ("what is this?")** — captures an on-demand screenshot **annotated with the cursor position** and injects it into Gemini's live context so the model can describe exactly what the user is pointing at (e.g., identifying a cooking ingredient, reading a product's reviews/delivery info).
- **Live & factual Q&A** — answers questions about the current screen *and* general/real-time facts (weather, "who made this?") via **Google Search**, without opening a browser tab.
- **Multi-agent routing** — a root **concierge** agent transparently routes requests to a **browser specialist** or a **search specialist** with no user-visible handoff.
- **Barge-in (interruption)** — the user can interrupt the agent mid-sentence by speaking; in-flight audio playback is cleared instantly.
- **Auto-recovery** — on any session crash, WebSocket disconnect, or Gemini Live API error (e.g., 1007/1011), the client reconnects within ~2 seconds with a fresh session ID.
- **Guided hand calibration** — an interactive 4-point calibration (Top-Left → Top-Right → Bottom-Right → Bottom-Left) maps finger position to screen coordinates, adapting to different camera angles and screen sizes.
- **Multi-user support** — each machine derives a stable, unique user ID from its hostname, so many users can connect to the shared backend simultaneously without conflicts.
- **Live transcription & conversation log** — both user speech and agent responses are transcribed and displayed in a sidebar, alongside agent/tool status and a webcam preview with overlaid cursor.

---

## Technical Implementation

This section walks through how Wand is built end-to-end, with specific files, functions, and code snippets from the repository.

### 0. High-Level Architecture

```
Cloud Run (server)                       Local machine (client)
──────────────────                       ──────────────────────
FastAPI + Google ADK                     companion_app (wand)
Gemini Live API (BIDI streaming)   ←→    mic / speaker / webcam
concierge → browser_agent / search       Playwright/Chromium browser
                                         MediaPipe hand tracking
```

- **Server** (`app/`): FastAPI WebSocket endpoint, ADK `Runner`, multi-agent definitions, and "remote" tools that delegate physical actions back to the client.
- **Client** (`client/`): a native macOS (AppKit/Cocoa) window, microphone/speaker streaming, MediaPipe hand tracking, and a local executor that runs the actual browser actions via Playwright.
- **Bridge**: a single persistent WebSocket carries (a) PCM16 audio both ways, (b) a ~20 Hz cursor coordinate stream, (c) `tool_call` / `tool_result` messages, and (d) trace/telemetry events.

### 1. The Gemini Live API Integration (ADK, BIDI streaming)

The server drives Gemini Live through **Google's Agent Development Kit (ADK)**. The WebSocket handler in `app/server.py` is the heart of the integration. Each connected client gets one streaming session built around a `LiveRequestQueue` and the ADK `Runner.run_live()` async generator.

**Endpoint & run configuration** — `app/server.py`:

```python
@app.websocket("/ws/{user_id}/{session_id}")
async def ws(user_id: str, session_id: str, websocket: WebSocket) -> None:
    await websocket.accept()
    ...
    # LiveRequestQueue: one per streaming session
    queue = ResettableLiveRequestQueue()

    run_config = RunConfig(
        streaming_mode=StreamingMode.BIDI,
        response_modalities=[types.Modality.AUDIO],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                prefix_padding_ms=200,
                silence_duration_ms=800,
            ),
            activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
        ),
    )
```

Key points:

- `StreamingMode.BIDI` + `response_modalities=[AUDIO]` gives true bidirectional voice.
- `AutomaticActivityDetection` with `START_OF_ACTIVITY_INTERRUPTS` is what enables **barge-in** — the model's turn is interrupted as soon as the user starts speaking.
- Input/output `AudioTranscriptionConfig()` produce the live transcripts used for the on-screen conversation log.

**The `Runner`** is constructed once at module scope around the root agent (`app/server.py`):

```python
runner = Runner(
    agent=root_agent,
    app_name=APP_NAME,
    session_service=session_service,
)
```

**Upstream (client → Gemini)** — the `upstream()` coroutine inside `ws()` receives binary audio frames and forwards them into the queue as PCM blobs, gated by the audio gate (see §5):

```python
INPUT_MIME = "audio/pcm;rate=16000"
...
async with audio_gate.lock:
    if not audio_gate.allow_audio_upload:
        dropped_audio_chunk_count += 1
        ...
        continue
    queue.send_realtime(types.Blob(mime_type=INPUT_MIME, data=b))
```

Text frames on the same socket are control messages — cursor updates, `tool_result`s, and client traces — parsed and dispatched in the same loop.

**Downstream (Gemini → client)** — the `downstream()` coroutine consumes ADK events from `runner.run_live(...)`, extracts audio parts and transcripts, and forwards audio bytes back over the socket (`app/server.py`):

```python
async for event in runner.run_live(
    user_id=user_id,
    session_id=session_id,
    live_request_queue=queue,
    run_config=run_config,
):
    ...
    if interrupted:
        await bridge.send_json({"type": "interrupt"})
    ...
    for part in event.content.parts:
        if not part.inline_data:
            continue
        mt = part.inline_data.mime_type
        data = part.inline_data.data
        if mt and mt.startswith("audio/pcm") and data is not None:
            await bridge.send_bytes(data)
```

When the model is interrupted, the server sends a `{"type": "interrupt"}` control message; the client uses this to clear its playback buffer (barge-in, §6).

### 2. The Multi-Agent System (concierge / browser / search)

Three agents are defined in `app/agents/`, each on a Gemini model chosen for its job.

**Root concierge** — `app/agents/concierge.py`. Uses Gemini native-audio, owns the conversation, and routes. It holds the search agent as an `AgentTool` (call-and-return) and the browser agent as a `sub_agent` (ownership transfer):

```python
MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

root_agent = Agent(
    name="concierge",
    model=MODEL,
    description="A voice-first concierge that chats with the user and delegates browser tasks.",
    instruction=(
        "You are the voice-first concierge. Always respond in English. Keep responses to 1-2 sentences.\n\n"
        "DELEGATE TO browser_agent immediately ... for:\n"
        "- Any browser/navigation/click/scroll/zoom/drag task.\n"
        ...
    ),
    before_tool_callback=[
        echo_dedupe_before_tool_callback,
        transfer_audio_gate_before_tool_callback,
    ],
    tools=[AgentTool(agent=search_agent, skip_summarization=True), remote_screenshot],
    sub_agents=[browser_agent],
)
```

> The team explicitly documents three ADK "topologies" and when to use each: **sub-agent** (full ownership transfer — used for the browser agent so it owns the conversation while browsing), **AgentTool** (called like a function, control returns to caller — used for search), and **direct tool** (pure execution — the remote tools). Matching topology to ownership intent is treated as a core design decision.

**Browser agent** — `app/agents/browser_agent.py`. Runs on `gemini-2.5-flash-native-audio-latest` and is given the full toolbox of remote browser actions plus a *very* detailed instruction that disambiguates intent (the kind of "prompt boundary clarity" the team found essential):

```python
browser_agent = Agent(
    name="browser_agent",
    model="gemini-2.5-flash-native-audio-latest",
    description="Controls the user's local browser through a remote tool bridge, supports 'here' via the user's local pointer cursor.",
    instruction=(
        "You control the user's local browser. The user points with a cursor (hand tracking or mouse).\n"
        "'Here', 'this', 'right there', 'this one' always refer to the current cursor position.\n"
        ...
        "CLICK vs PLAY/PAUSE — THIS IS CRITICAL:\n"
        "- remote_play_pause() is ONLY for toggling a video that is already open ...\n"
        "- remote_click_here() when user points at a thumbnail/result and says ... 'play this video' ...\n"
        ...
        "SEARCH — ALWAYS USE URL PARAMETERS, NEVER TYPE IN SEARCH BARS:\n"
        "- YouTube search: remote_navigate('https://www.youtube.com/results?search_query=QUERY')\n"
        "- Amazon search: remote_navigate('https://www.amazon.com/s?k=QUERY')\n"
        ...
    ),
    tools=[remote_navigate, remote_pan, remote_click_here, remote_scroll_here,
           remote_drag_here, remote_screenshot, remote_play_pause, remote_go_back],
)
```

Notably, search is done by constructing **URL templates** rather than typing into search bars — a robustness trick that avoids brittle text-field interaction.

**Search agent** — `app/agents/search_agent.py`. Plain `gemini-2.5-flash` with ADK's built-in `google_search` tool, returning short plain-prose answers:

```python
from google.adk.tools import google_search

search_agent = Agent(
    name="search_agent",
    model="gemini-2.5-flash",
    instruction=(
        "You answer questions by searching the web with Google Search.\n"
        "Always use google_search before answering.\n"
        "Return a concise, factual answer in plain text — 1-3 sentences max.\n"
    ),
    tools=[google_search],
)
```

### 3. The "Remote Tool Bridge" — running actions on the user's machine

A defining design choice: the Gemini-side tools don't *do* anything locally — they are thin RPC stubs that forward a `tool_call` over the WebSocket to the client, which performs the real browser action and returns a `tool_result`. This is what the project calls **client-side cursor resolution at execution time**: the server reasons about *what* to do; the client resolves *where* (current pointer position) when the action actually runs.

**Server-side stubs** — `app/tools/remote_browser.py`. Each tool is a coroutine that calls `call_local_tool(...)` and emits trace events:

```python
async def remote_click_here(tool_context: ToolContext) -> dict:
    return await _call(tool_context, "click_here", {})

async def remote_scroll_here(tool_context: ToolContext, delta_y: int, delta_x: int = 0) -> dict:
    return await _call(tool_context, "scroll_here",
                       {"delta_y": int(delta_y), "delta_x": int(delta_x)})
```

**The bridge** — `app/runtime/session_bridge.py`. `SessionBridge.call_tool()` assigns a `call_id`, sends a `tool_call` frame, and awaits a future resolved when the matching `tool_result` arrives:

```python
async def call_tool(self, tool, args, timeout_s=20.0, *, request_id=None, agent_name=None):
    call_id = str(uuid4())
    fut: asyncio.Future[dict] = loop.create_future()
    self.pending_calls[call_id] = fut
    ...
    await self.send_json({"type": "tool_call", "call_id": call_id, "tool": tool, "args": args})
    payload = await asyncio.wait_for(fut, timeout=timeout_s)
    ...
```

`complete_tool_call()` resolves that future when the client replies, and a module-level registry (`_bridges[(user_id, session_id)]`) lets any tool find the right socket for a session.

**Client-side executor** — `client/local_executor.py`. `LocalToolExecutor.handle_message()` receives `tool_call`s, de-dupes by `call_id`, dispatches to the real implementation, and sends back a `tool_result`. The dispatcher maps tool names to Playwright actions:

```python
async def _dispatch(self, tool: str, args: Dict[str, Any]) -> dict:
    if tool == "navigate":
        return await navigate(url=args["url"])
    if tool == "click_here":
        x, y, geometry = await self._prepare_here_action()
        return await click_screen_point(x=x, y=y, geometry=geometry)
    if tool == "scroll_here":
        x, y, geometry = await self._prepare_here_action()
        return await scroll_screen_point(x=x, y=y,
            delta_y=int(args.get("delta_y", 0)),
            delta_x=int(args.get("delta_x", 0)), geometry=geometry)
    if tool == "screenshot":
        return await take_screenshot(cursor_x=args.get("cursor_x"),
                                     cursor_y=args.get("cursor_y"))
    ...
```

`_prepare_here_action()` is where the cursor is resolved **at execution time** — it checks calibration, refreshes browser geometry, and reads the *current* fingertip position, guaranteeing "here" always means where the finger is *now*.

### 4. Hand Tracking & the "Hand Cursor" (MediaPipe + OpenCV + homography)

This is the project's signature UX. The pipeline is: **webcam → MediaPipe fingertip detection → normalized coords → homography mapping to screen pixels → smoothing → on-screen overlay → ~20 Hz cursor stream to server.**

**Fingertip detection** — `client/cursor/webcam_tracker.py`. A worker thread runs MediaPipe's `HandLandmarker` in video mode and extracts landmark **#8** (the index fingertip):

```python
def _create_landmarker(self):
    model_path = _resolve_model_path()
    options = mp_vision.HandLandmarkerOptions(
        base_options=mp_tasks_python.BaseOptions(model_asset_path=str(model_path)),
        running_mode=mp_vision.RunningMode.VIDEO,
        num_hands=self.num_hands,
        min_hand_detection_confidence=self.min_detection_confidence,
        ...
    )
    return mp_vision.HandLandmarker.create_from_options(options)

def _detect_fingertip(self, frame_bgr, landmarker):
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    result = landmarker.detect_for_video(mp_image, int(time.monotonic() * 1000))
    if not result.hand_landmarks:
        return None
    tip = result.hand_landmarks[0][self.index_tip_id]  # index_tip_id == 8
    return (min(1.0, max(0.0, float(tip.x))),
            min(1.0, max(0.0, float(tip.y))), score)
```

The model file `hand_landmarker.task` is bundled in `client/models/` (float16). Frames are mirrored (`cv2.flip`) so movement feels natural, and the preview frame is annotated with a yellow dot at the fingertip.

**Normalized → screen mapping with calibration** — `client/cursor/mapper.py`. The mapper applies a **perspective homography** (estimated by OpenCV from the 4 calibration points) and then **exponential smoothing**:

```python
def calibrate_from_correspondences(self, camera_points_norm, screen_points_px):
    src = np.array(camera_points_norm[:4], dtype=np.float32)
    dst = np.array(screen_points_px[:4], dtype=np.float32)
    H = cv2.getPerspectiveTransform(src, dst)
    self._homography = H
    ...

def _map_to_screen(self, x_norm, y_norm):
    if self._homography is not None:
        pts = np.array([[[x_norm, y_norm]]], dtype=np.float32)
        mapped = cv2.perspectiveTransform(pts, self._homography)
        return float(mapped[0][0][0]), float(mapped[0][0][1])
    # fallback: linear stretch to screen size
    return x_norm * (self.screen_geometry.width - 1), y_norm * (self.screen_geometry.height - 1)
```

```python
# update_from_normalized(): smoothing toward the raw mapped point
alpha = self.smoothing   # default 0.35
smoothed_x = alpha * raw_x + (1.0 - alpha) * self._smoothed_xy[0]
smoothed_y = alpha * raw_y + (1.0 - alpha) * self._smoothed_xy[1]
```

A `get_fallback()` returns the last cursor for a short `stale_timeout_s` (default 0.4 s) so brief detection dropouts don't make the cursor vanish.

**Guided 4-point calibration** — `client/cursor/provider.py`, `HandCursorProvider.run_guided_calibration()`. The user moves the virtual cursor into each target ring (TL → TR → BR → BL); after a short stable dwell the median camera-space sample is captured and paired with the target's screen coordinates, then fed to `calibrate_from_correspondences`:

```python
if distance <= target_radius:
    if inside_since is None:
        inside_since = now
        stable_camera_samples = []
    stable_camera_samples.append((float(sample.x), float(sample.y)))
    if now - inside_since >= dwell_s and len(stable_camera_samples) >= 3:
        x_med = statistics.median(p[0] for p in stable_camera_samples)
        y_med = statistics.median(p[1] for p in stable_camera_samples)
        camera_points.append((x_med, y_med))
        screen_points.append((sx, sy))
        break
...
ok, msg = self.mapper.calibrate_from_correspondences(camera_points, screen_points)
```

**Provider abstraction** — `client/cursor/provider.py` defines a `CursorProvider` Protocol with two implementations: `HandCursorProvider` (webcam) and `MouseCursorProvider` (a plain mouse fallback using `pynput`). Both expose `get_cursor() -> CursorSample`, so the rest of the app is source-agnostic.

**Streaming the cursor at ~20 Hz** — `client/companion_runtime.py`, `_cursor_sender()`. The latest cursor is sent as a JSON control frame on the same socket:

```python
async def _cursor_sender(self, sender: WSSender) -> None:
    interval = 1.0 / self.cursor_send_hz          # default 20 Hz
    while not self._stop_evt.is_set():
        snapshot = self.state.snapshot()
        if snapshot.local_cursor is not None:
            payload = {"type": "cursor",
                       "x": snapshot.local_cursor.x, "y": snapshot.local_cursor.y,
                       "source": self.cursor_source, "ts": snapshot.local_cursor.ts}
            await sender.send_json("cursor", payload)
        await asyncio.sleep(interval)
```

On the server, `app/server.py` parses these and stores them in a per-session cache (`app/runtime/realtime_pointer.py` → `set_cursor()` / `get_cursor()`), which the screenshot tool later reads.

### 5. Visual Grounding — screenshot annotated with the cursor, injected into Gemini

When the user asks *"what is this?"*, the server reads the cached cursor, asks the client for a screenshot, draws a marker at the cursor, and **injects the JPEG straight into the live audio stream** as an inline image blob.

**Server tool** — `app/tools/remote_vision.py`, `remote_screenshot()`:

```python
async def remote_screenshot(tool_context: ToolContext) -> dict:
    user_id, session_id = _get_uid_sid(tool_context)
    cursor = await get_cursor(user_id, session_id)
    args = {"cursor_x": cursor["x"], "cursor_y": cursor["y"]} if cursor else {}

    result = await call_local_tool(user_id=user_id, session_id=session_id,
                                   tool="screenshot", args=args, request_id=str(uuid4()),
                                   agent_name="concierge")

    gate = get_audio_gate(user_id, session_id)
    if gate and result.get("data"):
        raw = base64.b64decode(result["data"])
        gate.queue.send_realtime(types.Blob(mime_type="image/jpeg", data=raw))   # inject into Gemini

    response = {"status": "screenshot captured", ...}
    if cursor:
        response["note"] = ("The screenshot has been annotated with a cursor marker ... "
                            "Describe what is at or near the cursor, not the whole screen.")
    return response
```

**Client capture & annotation** — `client/actions/screenshot.py`. In embedded headless mode it screenshots the Playwright page directly (so the AI sees the page, not the app chrome), converts screen-space cursor coords to viewport coords, draws a **red circle + crosshair**, and downsizes/compresses to stay under Gemini Live's inline-image size limit:

```python
raw = await page.screenshot(type="jpeg", quality=55, scale="css")
...
draw = ImageDraw.Draw(img)
r = 18
draw.ellipse([vp_x - r, vp_y - r, vp_x + r, vp_y + r], outline="red", width=3)
draw.line([vp_x - r, vp_y, vp_x + r, vp_y], fill="red", width=2)
draw.line([vp_x, vp_y - r, vp_x, vp_y + r], fill="red", width=2)
```

> **Why a screenshot and not video?** The Devpost write-up notes Gemini Live's native-audio model doesn't accept continuous video input, so the team uses **on-demand screenshot injection** plus the **20 Hz cursor stream** as their substitute for "the model seeing the screen."

### 6. The "Audio Gate" — stabilizing multi-agent handoffs

A core finding: ADK's `transfer_to_agent` can destabilize Gemini Live because buffered audio (and an orphaned transfer function-response) arrives at the *new* agent's session out of context, triggering `APIError 1007`. Wand solves this with two mechanisms.

**Gate state** — `app/live/audio_gate.py` defines `SessionAudioGate` (an `allow_audio_upload` flag + lock wrapping the queue) and a per-session registry.

**Transfer guard callback** — `app/callbacks/handoff_guard.py`. Registered as a `before_tool_callback` on the agents, it fires when `transfer_to_agent` is about to run: it **closes the gate**, **drops the realtime audio backlog**, tells the client to stop sending mic audio, then reopens after a short delay:

```python
async def transfer_audio_gate_before_tool_callback(tool, args, tool_context):
    if getattr(tool, "name", "") != "transfer_to_agent":
        return None
    ...
    async with gate.lock:
        gate.allow_audio_upload = False
        gate.handoff_pending = True
        gate.target_agent = target_agent
        gate.queue.drop_realtime_backlog()
        gate.reopen_task = asyncio.create_task(
            _reopen_after_transfer_delay(user_id=user_id, session_id=session_id))
    ...
    await bridge.send_json(make_audio_gate_message(session_id=session_id,
                                                   state="closed", reason="transfer_to_agent"))
```

**Dropping orphaned responses** — `app/live/resettable_queue.py`. A `LiveRequestQueue` subclass intercepts the orphaned `transfer_to_agent` function-response (the documented root cause of 1007) and drops it, plus a `drop_realtime_backlog()` that flushes queued audio blobs while preserving content/close requests:

```python
class ResettableLiveRequestQueue(LiveRequestQueue):
    def send_content(self, content: types.Content) -> None:
        if content and content.parts:
            for part in content.parts:
                fr = getattr(part, "function_response", None)
                if fr and getattr(fr, "name", None) == "transfer_to_agent":
                    return  # Drop orphaned transfer response (prevents 1007)
        super().send_content(content)
```

The client honors the gate in `client/companion_runtime.py` (`_handle_audio_gate()` sets `_audio_gate_open` and drains the mic queue), so no stale audio reaches the new agent.

### 7. Real-time Audio I/O & Barge-in (client)

`client/companion_runtime.py` owns the audio. Input is PCM16 mono @ **16 kHz**; output is @ **24 kHz**, both via `sounddevice`.

**Mic capture & upload** — `_mic_sender()` opens a `RawInputStream`, prefers the built-in mic, and pushes 100 ms chunks over the socket (unless muted or gated):

```python
IN_RATE = 16000; OUT_RATE = 24000; CHUNK_MS = 100
...
with sd.RawInputStream(samplerate=IN_RATE, channels=1, dtype="int16",
                       blocksize=CHUNK_SAMPLES, callback=callback, device=builtin_mic):
    while not self._stop_evt.is_set():
        chunk = await asyncio.wait_for(q.get(), timeout=0.25)
        if self.state.snapshot().muted: continue
        if not self._audio_gate_open: continue
        await sender.send_bytes("mic_chunk", chunk)
```

**Playback & barge-in** — `_receiver_loop()` uses a pull-mode `RawOutputStream` callback reading from a thread-safe buffer. On the server's `{"type":"interrupt"}` message, it **clears the buffer**, so output goes silent within ~43 ms (one block at 24 kHz):

```python
if payload.get("type") == "interrupt":
    cleared_bytes = _playback_clear()
    print(f"[barge-in] interrupt: cleared {cleared_bytes} bytes ...")
    continue
```

> **Echo / AEC note:** the team tried `speexdsp` AEC and RMS-based gating but both compromised barge-in responsiveness, so the shipped recommendation is to **use headphones** as a physical-layer fix (documented in `Description.md` and the README).

### 8. Auto-Recovery & Session Rotation

`client/companion_runtime.py` `_run_forever()` wraps the whole session in a reconnect loop. On disconnect or error it waits `RECONNECT_DELAY_S = 2.0` and rotates to a **new session ID** before reconnecting:

```python
RECONNECT_DELAY_S = 2.0
...
except (OSError, websockets.exceptions.ConnectionClosed) as exc:
    self.state.set_connected(False)
    await asyncio.sleep(RECONNECT_DELAY_S)
...
def _rotate_session(self) -> None:
    self.state.set_session_id(generate_session_id())
```

Stable per-machine identity and fresh sessions come from `client/session_ids.py`:

```python
def get_stable_user_id() -> str:
    # hostname + random suffix, persisted to ~/.config/companion-agent/user_id
    ...
    uid = f"{hostname}_{secrets.token_hex(4)}"   # e.g. "macbook-pro_a3f29c1b"

def generate_session_id() -> str:
    return f"local_session_{secrets.token_hex(6)}"
```

The server, on its side, deletes the session in the `finally` block of `ws()` and clears the audio gate, so a crashed session leaves no stale state.

### 9. The Embedded Browser & Native macOS Shell

**Headless Chromium via Playwright + CDP screencast** — `app/runtime/browser_runtime.py`. The browser runs headless; its frames are streamed to the app via Chrome DevTools Protocol `Page.startScreencast` and rendered into a native `NSImageView`:

```python
await cdp.send("Page.startScreencast",
               {"format": "jpeg", "quality": 75, "maxWidth": width, "maxHeight": height})
...
async def _on_frame(event):
    _screencast_frame = base64.b64decode(event["data"])
    ...
    await cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
```

**Screen → viewport coordinate conversion** — `app/tools/browser/mouse.py`. "Here" actions take OS screen coordinates (where the finger points) and convert them into Playwright viewport coordinates before clicking/scrolling:

```python
async def screen_to_viewport(x, y, *, geometry=None):
    resolved = geometry or await refresh_browser_geometry()
    origin_x, origin_y = resolved.viewport_origin
    vx = int(x) - origin_x
    vy = int(y) - origin_y
    ...  # clamp into viewport
    return vx, vy

async def click_screen_point(x, y, *, geometry=None):
    vx, vy = await screen_to_viewport(x, y, geometry=resolved_geometry)
    page = await get_page(headless=False)
    await page.mouse.click(vx, vy)
    return {"ok": True, "action": "click_here", "x": vx, "y": vy,
            "screen_cursor": {"x": x, "y": y}, ...}
```

Play/pause is done by injecting JS into the page — `app/tools/browser/media.py`:

```python
result = await page.evaluate("""() => {
    const video = document.querySelector('video');
    if (!video) return {ok:false, error:'No video element found ...'};
    if (video.paused) { video.play(); return {ok:true, state:'playing'}; }
    else { video.pause(); return {ok:true, state:'paused'}; }
}""")
```

**Native window** — `client/companion_app.py` builds a Cocoa (`AppKit`/`pyobjc`) window: a left **`BrowserView`** showing the screencast (and forwarding direct mouse clicks/scrolls back into the page), and a right **sidebar** with connection status, current agent/tool, a webcam preview with overlaid cursor, the conversation log, a debug console, and buttons (Mute / Reconnect / Calibrate / Debug / Quit). An `NSTimer` ticks at 10 Hz (`tick:`) to refresh the UI and push the latest screencast frame.

### 10. Deployment & Distribution

- **Server** is containerized (`Dockerfile`) and deployed to **Google Cloud Run** via `deploy.sh`, which enables required GCP APIs, verifies the Gemini key exists in **Secret Manager**, builds from source, and injects the key with `--set-secrets` (never hardcoded). Live backend: `wss://adk-agent-orchestrator-385929302643.us-central1.run.app/ws`.
- **Client** installs with a one-line `install.sh` that clones to `~/.local/share/companion-agent`, creates a venv, installs deps, runs `playwright install chromium`, and adds a `wand` command to PATH. The app connects to the shared backend with **no API keys or config** on the user's side.

---

## Technology Stack (at a glance)

| Layer | Technologies |
| --- | --- |
| **AI / Agents** | Google ADK (`Runner`, `LiveRequestQueue`, `AgentTool`, `transfer_to_agent`), Gemini Live API |
| **Models** | `gemini-2.5-flash-native-audio-*` (concierge & browser, bidirectional audio); `gemini-2.5-flash` (search) |
| **Grounding** | ADK built-in `google_search`; on-demand screenshot injection (`image/jpeg` blobs) |
| **Cloud** | Google Cloud Run, Secret Manager, IaC via `deploy.sh`, Docker |
| **Backend** | Python 3.11/3.12, FastAPI, WebSockets |
| **Client** | Playwright + headless Chromium (CDP screencast), MediaPipe (`HandLandmarker`), OpenCV, NumPy (homography), `sounddevice` (PCM16 16k/24k), `pyobjc` (AppKit/Quartz/Foundation), `pynput` (mouse fallback) |
| **Comms** | Single persistent WebSocket: PCM16 audio, ~20 Hz cursor stream, `tool_call`/`tool_result` RPC, trace events |

## Key Files Index

| File | Responsibility |
| --- | --- |
| `app/server.py` | FastAPI WS endpoint; ADK `Runner.run_live`, `RunConfig`, up/downstream audio loops |
| `app/agents/concierge.py` | Root router agent (native audio); sub-agent + AgentTool wiring |
| `app/agents/browser_agent.py` | Browser specialist; detailed intent-disambiguation prompt + remote tools |
| `app/agents/search_agent.py` | Google-Search-backed Q&A agent |
| `app/tools/remote_browser.py` | Server-side RPC stubs for browser actions |
| `app/tools/remote_vision.py` | `remote_screenshot`: cursor read + JPEG injection into Gemini |
| `app/tools/browser/mouse.py` | Screen→viewport mapping, click/scroll/drag/pan via Playwright |
| `app/tools/browser/media.py` | JS-injected play/pause |
| `app/runtime/session_bridge.py` | `SessionBridge` RPC over WS (`call_tool`/`complete_tool_call`) |
| `app/runtime/browser_runtime.py` | Headless Chromium, CDP screencast, geometry computation |
| `app/runtime/realtime_pointer.py` | Per-session cursor cache (`set_cursor`/`get_cursor`) |
| `app/live/audio_gate.py` | `SessionAudioGate` + registry |
| `app/live/resettable_queue.py` | Drops orphaned `transfer_to_agent` responses; backlog flush |
| `app/callbacks/handoff_guard.py` | `before_tool_callback` that gates audio across transfers |
| `client/companion_app.py` | Native macOS (Cocoa) window, sidebar UI, browser view |
| `client/companion_runtime.py` | Audio I/O, barge-in, cursor sender, reconnect loop |
| `client/local_executor.py` | Receives `tool_call`s, runs Playwright actions, returns results |
| `client/cursor/webcam_tracker.py` | MediaPipe fingertip detection (landmark #8) |
| `client/cursor/mapper.py` | Homography calibration + smoothing (normalized → screen px) |
| `client/cursor/provider.py` | `HandCursorProvider` / `MouseCursorProvider`; guided calibration |
| `client/actions/screenshot.py` | Playwright screenshot + red cursor annotation + downscale |
| `client/session_ids.py` | Stable user IDs + rotating session IDs |
| `deploy.sh` / `install.sh` | Cloud Run deploy / one-line client installer |
