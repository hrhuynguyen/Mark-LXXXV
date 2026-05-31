# Mark 58

## Inspiration

I wanted Tony Stark's workshop — the scene where he *talks* to an AI, waves his hands at a glowing 3D model, and reshapes it in the air. Today the closest thing we have is typing prompts into a chat box and waiting for a tool to spit out a mesh. That breaks the creative flow: 3D modeling is spatial and conversational, but our interfaces are textual and turn-based.

So I set out to build **Mark 58** — a voice + gesture "holographic workshop" that drives real **Blender** in real time. You point at the screen and say "make this taller," "build me a Falcon 9," "what is this part?" — and it just happens, in the live Blender viewport, while you keep talking.

## What it does

Mark 58 turns Blender into an agent-controllable, voice-and-gesture-driven modeling studio:

- **Talk to it.** A Gemini Live voice agent listens continuously, with barge-in — you can interrupt it mid-sentence and it stops talking and reacts.
- **Point at things.** Your webcam tracks your hand; your fingertip becomes a cursor mapped onto the Blender viewport. "What's this?" or "scale this up 20%" resolves "this" to whatever you're pointing at via a raycast pick.
- **Build by describing.** "Build a rocket" expands into a structured Build Spec (named procedural parts — nose cone, body, fins, landing legs, engine cluster), which executes in Blender and registers every object so it can be inspected and edited later.
- **Add primitives instantly.** "Add a red cube" runs Python inside Blender immediately.
- **Inspect & frame.** Ask about an object and it returns transforms, mesh stats, and a world-space bounding box; "frame this" reframes the viewport.

Under the hood it's three processes: a **cloud-style agent server** (Gemini Live + Google ADK), a **local macOS client** (mic, webcam, cursor overlay), and **Blender** running a custom addon. They talk over two clean seams: a WebSocket between server and client, and a JSON-over-TCP socket between the client and Blender.

## How I built it

The heart of Mark 58 is **Google's Gemini stack**, which makes the whole "just talk and point" experience possible:

- **Gemini Live API (native audio, bidirectional streaming)** is what lets you hold a real conversation with the app — continuous listening, natural speech in and out, and instant barge-in so you can cut the agent off and redirect it mid-sentence. This realtime audio loop is the feature everything else hangs off of.
- **Google Agent Development Kit (ADK)** structures the intelligence as a multi-agent system: a `concierge` router that delegates to a `blender_agent` for modeling and a `search_agent` for facts, plus a `spec_agent` that turns a terse request like "build a Falcon 9" into a structured Build Spec of named, procedural parts. ADK's tool-calling is how the model reaches out of the conversation and actually *does* things.
- **Gemini's function/tool calling** bridges language to action: the model calls tools like `execute_blender_code`, `pick_object_at`, `get_object_info`, and `frame_object`, which are executed against the live Blender scene and registered so objects can be inspected and re-edited later.

Around that core I built the real-time plumbing. A **FastAPI WebSocket** server streams audio, cursor, and tool calls between the cloud agent and a local macOS client; the client owns the microphone, the webcam, and an on-screen cursor overlay, and relays the agent's tool calls into Blender over a lightweight JSON socket. Inside Blender, a custom addon executes each command on the main thread (since `bpy` isn't thread-safe) and streams results back.

For pointing, I used **MediaPipe** hand landmarks and built a calibration that maps a fingertip's normalized webcam coordinates to the screen, then through an affine transform into Blender viewport pixels, finishing with a radius raycast to pick whatever object is under the cursor — so "make *this* taller" resolves to exactly what you're aiming at.

Stack: Python, **Gemini Live + Google ADK**, FastAPI + WebSockets, MediaPipe, OpenCV, PyObjC/AppKit, sounddevice, Blender `bpy`; `uv` for env, `pytest` + `ruff` for quality.

## Challenges I ran into

- **Hand tracking that actually points.** My first calibration pinned the cursor to a corner with ~0% pick accuracy. Live instrumentation revealed the real cause: I was tracking the palm, which barely translates when you point by rotating your wrist — the mapped range collapsed to ~84px. Switching to the *index fingertip* plus a linear inner-area map (which can't collapse like a homography) got it to ≥90% accuracy.
- **The thread-safety trap.** Blender will crash if you touch `bpy` off the main thread. The socket runs on a background thread, so every command has to be scheduled back onto Blender's main loop and the reply sent from inside that callback.
- **Real-time audio.** Getting low-latency barge-in right — interrupting playback the instant the user speaks, suppressing the mic during the agent's own speech so it doesn't talk to itself — took careful buffer management (pull-mode playback with a thread-safe ring buffer).
- **Resolving "this."** "Make *this* taller" only works if the agent, the cursor, and Blender all agree on coordinates. I had to pin down three coordinate systems and a clean contract so the agent could call `pick_object_at()` with no arguments and let the *client* decide where "here" is.
- **Decoupling voice for debugging.** When builds failed it was hard to tell whether the bug was in speech, the agent, or Blender. I built a voice-free text console that reproduces the exact pipeline so I could bisect failures stage by stage.

## Accomplishments that I'm proud of

- A **self-driving** Blender agent — no external MCP client required; the whole loop is mine end to end.
- **≥90% point-and-pick accuracy** from a plain webcam, with live coordinate readouts I can trust.
- A genuinely **conversational** feel: continuous listening, barge-in, and "act first, confirm briefly after."
- Going from "add a cube" all the way to **describing a multi-part rocket** that builds as named, inspectable, re-editable parts via a Build Spec + Part Registry.
- Clean architecture: two frozen seam contracts and a monorepo where the server, client, and addon evolve independently.

## What I learned

- The reusable core of tools like BlenderMCP isn't the AI integration — it's the **"tool stub → socket → main-thread host handler"** bridge pattern, which generalizes to controlling almost any scriptable desktop app.
- Multimodal UX lives or dies on **coordinate discipline**; a small mapping error makes the whole "point at it" illusion fall apart.
- **Instrument first, fix second.** Every hard bug here was solved by surfacing live data (cursor spreads, trace events, per-stage logs) rather than guessing.
- Native-audio realtime models behave very differently from text models — streaming, interruption, and gating are first-class design problems, not afterthoughts.

## What's next for Mark 58

- **Gesture commands** beyond pointing — pinch-to-grab, two-handed scale, orbit/zoom/pan the viewport with your hands (21-landmark recognizer).
- **Registry-driven edits** — "make the fins 20% bigger," "regenerate this part," undo/redo over tracked objects.
- **Contextual answers on point** — richer "what is this and how big is it?" with a viewport screenshot the agent can actually see.
- **Asset & generation tools** — wire in text-to-3D and asset libraries so "drop in a textured rock" works.
- **Persistence** — save/restore sessions locally, then to the cloud.
- **Going truly holographic** — projecting the workshop into AR/spatial displays so the model really does float in front of you.
