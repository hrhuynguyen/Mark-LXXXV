"""Forge ADK server: FastAPI WebSocket endpoint + Gemini Live BIDI audio."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from google.genai.errors import APIError

from server.agents import root_agent
from server.callbacks.echo_dedupe import LATEST_MODEL_OUTPUT_KEY, LATEST_USER_INPUT_KEY
from server.callbacks.handoff_guard import clear_transfer_audio_gate
from server.live.audio_gate import SessionAudioGate, register_audio_gate, unregister_audio_gate
from server.live.resettable_queue import ResettableLiveRequestQueue
from server.live.trace import log_trace_event, make_cursor_ack, parse_trace_payload
from server.runtime.cursor_payload import parse_cursor_payload
from server.runtime.realtime_pointer import clear_cursor, set_cursor
from server.runtime.session_bridge import (
    emit_server_trace,
    handle_tool_result,
    register_bridge,
    send_session_meta,
    unregister_bridge,
)


load_dotenv()

APP_NAME = "forge"
INPUT_MIME = "audio/pcm;rate=16000"
SERVICE_NAME = os.getenv("K_SERVICE", "local-dev")
SERVICE_REVISION = os.getenv("K_REVISION", "local-revision")
GIT_COMMIT_SHA = os.getenv("GIT_COMMIT_SHA", "unknown")
EXPECTED_AUDIO_CHUNK_BYTES = 3200
AUDIO_LOG_EVERY_CHUNKS = int(os.getenv("AUDIO_LOG_EVERY_CHUNKS", "20"))
CURSOR_TRACE_INTERVAL_S = float(os.getenv("CURSOR_TRACE_INTERVAL_S", "1.0"))
CURSOR_TRACE_MIN_DELTA_PX = int(os.getenv("CURSOR_TRACE_MIN_DELTA_PX", "24"))

logger = logging.getLogger("forge.server")

app = FastAPI(title="Forge", version="0.1.0")
session_service = InMemorySessionService()
runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)


def _info(message: str) -> None:
    logger.info(message)
    print(message)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "stage": "live",
        "agent": root_agent.name,
        "model": getattr(root_agent, "model", "unknown"),
    }


@app.websocket("/ws/{user_id}/{session_id}")
async def ws(websocket: WebSocket, user_id: str, session_id: str) -> None:
    await websocket.accept()
    bridge = await register_bridge(user_id=user_id, session_id=session_id, websocket=websocket)
    _info(
        "[session] accepted ws "
        f"user={user_id} session={session_id} service={SERVICE_NAME} "
        f"revision={SERVICE_REVISION} commit={GIT_COMMIT_SHA} "
        f"model={getattr(root_agent, 'model', 'unknown')} input_mime={INPUT_MIME}"
    )
    await send_session_meta(
        user_id=user_id,
        session_id=session_id,
        service=SERVICE_NAME,
        revision=SERVICE_REVISION,
        commit=GIT_COMMIT_SHA,
    )
    await emit_server_trace(
        user_id=user_id,
        session_id=session_id,
        request_id=session_id,
        event="session_connected",
        status="ok",
        summary=f"connected to {SERVICE_NAME}@{SERVICE_REVISION}",
        agent_name=root_agent.name,
    )

    session = await session_service.get_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
    )
    if session is None:
        session = await session_service.create_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
        )

    async def append_session_state_text(*, key: str, value: str, author: str | None) -> None:
        text = str(value or "").strip()
        if not text:
            return
        await session_service.append_event(
            session=session,
            event=Event(
                invocation_id=f"state:{session_id}:{key}:{int(time.time() * 1000)}",
                author=str(author or root_agent.name),
                actions=EventActions(state_delta={key: text}),
            ),
        )

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
    audio_gate = SessionAudioGate(queue=queue)
    register_audio_gate(user_id=user_id, session_id=session_id, gate=audio_gate)

    async def upstream() -> None:
        audio_chunk_count = 0
        audio_bytes_total = 0
        dropped_audio_chunk_count = 0
        last_cursor_trace_ts = 0.0
        last_logged_cursor: tuple[int, int] | None = None
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break

                if (audio := msg.get("bytes")) is not None:
                    if not audio:
                        continue
                    audio_chunk_count += 1
                    audio_bytes_total += len(audio)
                    if (
                        len(audio) != EXPECTED_AUDIO_CHUNK_BYTES
                        or audio_chunk_count == 1
                        or audio_chunk_count % AUDIO_LOG_EVERY_CHUNKS == 0
                    ):
                        _info(
                            "[upstream.audio] "
                            f"user={user_id} session={session_id} chunks={audio_chunk_count} "
                            f"total_bytes={audio_bytes_total} last_chunk={len(audio)}"
                        )
                    async with audio_gate.lock:
                        if not audio_gate.allow_audio_upload:
                            dropped_audio_chunk_count += 1
                            if dropped_audio_chunk_count == 1:
                                _info(
                                    "[upstream.audio] gated "
                                    f"user={user_id} session={session_id}"
                                )
                            continue
                        queue.send_realtime(types.Blob(mime_type=INPUT_MIME, data=audio))
                    continue

                text = msg.get("text")
                if text is None:
                    continue
                try:
                    payload = json.loads(text)
                except Exception:
                    continue

                if payload.get("type") == "tool_result":
                    await handle_tool_result(user_id=user_id, session_id=session_id, payload=payload)
                    continue

                client_trace = parse_trace_payload(
                    payload,
                    expected_type="client_trace",
                    expected_source="client",
                    expected_session_id=session_id,
                )
                if client_trace is not None:
                    log_trace_event(client_trace)
                    continue

                pos = parse_cursor_payload(payload)
                if pos is not None:
                    x_i, y_i = pos
                    await set_cursor(user_id=user_id, session_id=session_id, x=x_i, y=y_i)
                    request_id = f"cursor:{payload.get('client_msg_id', 'na')}"
                    await bridge.send_json(
                        make_cursor_ack(
                            session_id=session_id,
                            request_id=request_id,
                            x=x_i,
                            y=y_i,
                        )
                    )
                    now = time.time()
                    delta_ok = (
                        last_logged_cursor is None
                        or abs(x_i - last_logged_cursor[0]) >= CURSOR_TRACE_MIN_DELTA_PX
                        or abs(y_i - last_logged_cursor[1]) >= CURSOR_TRACE_MIN_DELTA_PX
                    )
                    if delta_ok and now - last_cursor_trace_ts >= CURSOR_TRACE_INTERVAL_S:
                        await emit_server_trace(
                            user_id=user_id,
                            session_id=session_id,
                            request_id=request_id,
                            event="cursor_received",
                            status="ok",
                            summary=f"cursor=({x_i},{y_i})",
                            cursor={"x": x_i, "y": y_i},
                        )
                        last_cursor_trace_ts = now
                        last_logged_cursor = (x_i, y_i)
                    continue
        except WebSocketDisconnect:
            pass

    async def downstream() -> None:
        output_chunk_count = 0
        output_bytes_total = 0
        last_input_transcript = ""
        last_output_transcript = ""
        last_final_input_transcript = ""
        last_final_output_transcript = ""
        try:
            _info(f"[downstream] run_live start user={user_id} session={session_id}")
            async for event in runner.run_live(
                user_id=user_id,
                session_id=session_id,
                live_request_queue=queue,
                run_config=run_config,
            ):
                author = getattr(event, "author", None)
                partial = bool(getattr(event, "partial", False))
                turn_complete = bool(getattr(event, "turn_complete", False))
                interrupted = bool(getattr(event, "interrupted", False))
                input_tx = getattr(event, "input_transcription", None)
                output_tx = getattr(event, "output_transcription", None)

                if input_tx is not None:
                    input_text = str(getattr(input_tx, "text", "") or "").strip()
                    if input_text and input_text != last_input_transcript:
                        last_input_transcript = input_text
                        await emit_server_trace(
                            user_id=user_id,
                            session_id=session_id,
                            request_id=session_id,
                            event="user_spoke",
                            status="ok",
                            summary=input_text,
                            agent_name=str(author) if author else None,
                        )
                    if not partial and input_text and input_text != last_final_input_transcript:
                        await append_session_state_text(
                            key=LATEST_USER_INPUT_KEY,
                            value=input_text,
                            author=str(author) if author else None,
                        )
                        last_final_input_transcript = input_text

                if output_tx is not None:
                    output_text = str(getattr(output_tx, "text", "") or "").strip()
                    if output_text and output_text != last_output_transcript:
                        last_output_transcript = output_text
                        await emit_server_trace(
                            user_id=user_id,
                            session_id=session_id,
                            request_id=session_id,
                            event="agent_spoke",
                            status="ok",
                            summary=output_text,
                            agent_name=str(author) if author else None,
                        )
                    if not partial and output_text and output_text != last_final_output_transcript:
                        await append_session_state_text(
                            key=LATEST_MODEL_OUTPUT_KEY,
                            value=output_text,
                            author=str(author) if author else None,
                        )
                        last_final_output_transcript = output_text

                if interrupted:
                    await bridge.send_json({"type": "interrupt"})
                if turn_complete or interrupted:
                    _info(
                        "[downstream.event] "
                        f"user={user_id} session={session_id} author={author} "
                        f"turn_complete={turn_complete} interrupted={interrupted}"
                    )
                if not event.content or not event.content.parts:
                    continue
                for part in event.content.parts:
                    if not part.inline_data:
                        continue
                    mt = part.inline_data.mime_type
                    data = part.inline_data.data
                    if mt and mt.startswith("audio/pcm") and data is not None:
                        output_chunk_count += 1
                        output_bytes_total += len(data)
                        if output_chunk_count == 1 or output_chunk_count % AUDIO_LOG_EVERY_CHUNKS == 0:
                            _info(
                                "[downstream.audio] "
                                f"user={user_id} session={session_id} chunks={output_chunk_count} "
                                f"total_bytes={output_bytes_total} last_chunk={len(data)}"
                            )
                        await bridge.send_bytes(data)
        except Exception as exc:
            is_api_error = isinstance(exc, APIError)
            await clear_transfer_audio_gate(user_id=user_id, session_id=session_id)
            await emit_server_trace(
                user_id=user_id,
                session_id=session_id,
                request_id=session_id,
                event="session_error",
                status="error",
                summary=(
                    f"APIError status={getattr(exc, 'status_code', None)} {exc}"
                    if is_api_error
                    else f"{type(exc).__name__}: {exc}"
                ),
                agent_name=root_agent.name,
            )
            traceback.print_exc()
            try:
                await websocket.close(
                    code=1011,
                    reason="live_api_error" if is_api_error else "downstream_error",
                )
            except Exception:
                pass
            raise

    try:
        results = await asyncio.gather(upstream(), downstream(), return_exceptions=True)
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                _info(
                    "[session] task exception "
                    f"user={user_id} session={session_id} task={idx} "
                    f"type={type(result).__name__} message={result}"
                )
    finally:
        await clear_transfer_audio_gate(user_id=user_id, session_id=session_id)
        event = {
            "event_id": f"disconnect-{session_id}",
            "request_id": session_id,
            "session_id": session_id,
            "source": "server",
            "event": "session_disconnected",
            "status": "ok",
            "summary": "websocket disconnected",
            "ts": time.time(),
        }
        log_trace_event(event)
        try:
            await bridge.send_json({"type": "trace_event", **event})
        except Exception:
            pass
        queue.close()
        unregister_audio_gate(user_id=user_id, session_id=session_id)
        await clear_cursor(user_id=user_id, session_id=session_id)
        await unregister_bridge(user_id=user_id, session_id=session_id, bridge=bridge)
        try:
            await session_service.delete_session(
                app_name=APP_NAME,
                user_id=user_id,
                session_id=session_id,
            )
        except Exception as exc:
            _info(
                "[session] delete failed "
                f"user={user_id} session={session_id} type={type(exc).__name__} message={exc}"
            )
