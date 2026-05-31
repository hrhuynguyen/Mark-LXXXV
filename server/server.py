"""server.server

Forge ADK server: FastAPI WebSocket endpoint + Gemini Live (BIDI audio). Reuse
Wand app/server.py. Built out in plan.md Step 5.

Right now this is a **scaffold**: a `/health` route plus a minimal `/ws` *sink*
that accepts the companion's connection and counts the frames it streams
(mic audio, cursor, client traces). The sink does not run any agent and never
sends audio back — it exists only so Step 4 can verify the client's
connect/stream loop end to end. Step 5 replaces `/ws` with the real Gemini Live
runner.
"""

from __future__ import annotations

import json
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI(title="Forge (scaffold)", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "stage": "scaffold", "note": "Step 5 adds Gemini Live; /ws is a sink"}


@app.websocket("/ws/{user_id}/{session_id}")
async def ws_sink(websocket: WebSocket, user_id: str, session_id: str) -> None:
    """Accept the companion connection and tally streamed frames.

    Matches client.session_ids.build_ws_session_url, which produces
    ``/ws/<user>/<session_id>``. Verification-only: logs a periodic tally of
    mic bytes + cursor/trace frames so Step 4 can confirm the client streams.
    """
    await websocket.accept()
    started = time.time()
    mic_chunks = mic_bytes = cursor_frames = trace_frames = other_frames = 0
    last_log = started
    print(f"[ws-sink] connected user={user_id} session={session_id}")
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            if (data := message.get("bytes")) is not None:
                mic_chunks += 1
                mic_bytes += len(data)
            elif (text := message.get("text")) is not None:
                try:
                    payload = json.loads(text)
                except Exception:
                    other_frames += 1
                else:
                    kind = payload.get("type")
                    if kind == "cursor":
                        cursor_frames += 1
                    elif kind == "client_trace":
                        trace_frames += 1
                        print(f"[ws-sink] client_trace: {payload.get('event')} ({payload.get('summary', '')})")
                    else:
                        other_frames += 1

            now = time.time()
            if now - last_log >= 2.0:
                print(
                    f"[ws-sink] tally @ {now - started:0.0f}s: "
                    f"mic={mic_chunks} chunks/{mic_bytes}B  "
                    f"cursor={cursor_frames}  trace={trace_frames}  other={other_frames}"
                )
                last_log = now
    except WebSocketDisconnect:
        pass
    finally:
        print(
            f"[ws-sink] closed session={session_id}: "
            f"mic={mic_chunks} chunks/{mic_bytes}B  cursor={cursor_frames}  trace={trace_frames}"
        )
