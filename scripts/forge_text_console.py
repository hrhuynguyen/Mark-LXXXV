"""Forge text console — voice-free testing of the Blender build pipeline.

This is a BlenderMCP-style harness: you *type* a prompt ("add a cube",
"build a rocket"), the cloud agent plans it, and the tool calls are executed
against your local Blender so you can watch the object render — with **no
microphone and no audio playback** (voice control is fully inactive here).

It plays the same role as ``client.companion_app`` on the Blender side (it owns
the ``BlenderConnection`` and runs ``LocalToolExecutor`` to satisfy the agent's
tool calls), but instead of a mic + cursor overlay it just reads stdin and sends
``{"type": "user_text", "text": ...}`` frames the server feeds to Gemini Live.

Usage:
    # Terminal A
    uv run uvicorn server.server:app
    # Terminal B
    uv run python -m scripts.forge_text_console

Then type a prompt and press Enter. Type 'quit' (or Ctrl-D) to exit.
Blender is launched/attached automatically; it is left running on exit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Optional

import websockets

from client.blender_bridge import BlenderConnection
from client.blender_launcher import DEFAULT_BLENDER_APP, ensure_blender_running
from client.local_executor import LocalToolExecutor
from client.session_ids import (
    DEFAULT_WS_ROOT_URL,
    build_ws_session_url,
    generate_session_id,
    get_stable_user_id,
    normalize_ws_root_url,
)
from client.ws_guard import OutboundTelemetry, WSSender

# Trace events worth surfacing in the console, mapped to a short prefix.
_TRACE_PREFIX = {
    "user_spoke": "you",
    "agent_spoke": "agent",
    "tool_called": "tool->",
    "tool_finished": "tool<-",
    "agent_started": "agent",
    "agent_finished": "agent",
    "session_connected": "sys",
    "session_error": "ERR",
}


def _build_ws_root(ws_url: str, user_id: str) -> str:
    """Honour an explicit --ws-url, else point the default root at --user."""
    if ws_url != DEFAULT_WS_ROOT_URL:
        return normalize_ws_root_url(ws_url)
    return normalize_ws_root_url(f"ws://127.0.0.1:8000/ws/{user_id}")


def _print(prefix: str, text: str) -> None:
    print(f"  [{prefix}] {text}", flush=True)


class _TracePrinter:
    """Collapses the stream of partial transcripts into readable lines."""

    def __init__(self) -> None:
        self._last: dict[str, str] = {}

    def handle(self, event: dict[str, Any]) -> None:
        name = str(event.get("event", ""))
        summary = str(event.get("summary", "") or "")
        tool = event.get("tool_name")
        status = str(event.get("status", ""))
        prefix = _TRACE_PREFIX.get(name)
        if prefix is None:
            # Surface everything else (tool_deduped, session_disconnected, ...)
            # so nothing the agent does is hidden during debugging.
            extra = f" tool={tool}" if tool else ""
            _print(f"trace:{name}", f"{status}{extra} {summary}".strip())
            return

        if name in ("tool_called", "tool_finished", "agent_started", "agent_finished"):
            if not tool:
                return
            line = f"{tool}: {summary}" if summary else tool
            if status == "error":
                line = f"FAILED {line}"
            _print(prefix, line)
            return

        if name in ("agent_spoke", "user_spoke"):
            # Skip pure growth of the same partial transcript.
            if summary and summary != self._last.get(name):
                self._last[name] = summary
                _print(prefix, summary)
            return

        if name == "session_error":
            _print(prefix, summary)
            return

        # session_connected etc.
        _print(prefix, summary or name)


async def _receiver(
    ws: websockets.ClientConnection,
    executor: LocalToolExecutor,
    printer: _TracePrinter,
) -> None:
    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                continue  # agent audio — ignored in the text console
            try:
                payload = json.loads(msg)
            except Exception:
                continue
            msg_type = payload.get("type")
            if msg_type == "trace_event":
                printer.handle(payload)
                continue
            if msg_type == "tool_call":
                _print("tool_call", f"{payload.get('tool')} args={payload.get('args')}")
                await executor.handle_message(payload)
                continue
            # session_meta / cursor_ack / interrupt / audio_gate — irrelevant here.
    except websockets.exceptions.ConnectionClosed as exc:
        _print("ws", f"connection closed: code={exc.code} reason={exc.reason!r}")


async def _stdin_sender(ws: websockets.ClientConnection, stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    print("\nType a prompt for Blender (e.g. 'add a red cube'). 'quit' to exit.\n", flush=True)
    while not stop.is_set():
        print("> ", end="", flush=True)
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if line == "":  # EOF (Ctrl-D)
            break
        text = line.strip()
        if not text:
            continue
        if text.lower() in ("quit", "exit", ":q"):
            break
        await ws.send(json.dumps({"type": "user_text", "text": text}))
    stop.set()


async def _run(ws_root: str, bridge: BlenderConnection) -> None:
    session_id = generate_session_id()
    url = build_ws_session_url(ws_root, session_id)
    print(f"[ws] connecting to {url}", flush=True)
    async with websockets.connect(url, max_size=None, ping_interval=20, ping_timeout=20) as ws:
        print("[ws] connected", flush=True)
        sender = WSSender(ws, OutboundTelemetry())
        executor = LocalToolExecutor(
            bridge=bridge,
            sender=sender,
            event_callback=lambda p: _print(
                "local", f"{p.get('tool_name')}: {p.get('status')} {p.get('summary', '')}"
            ),
        )
        printer = _TracePrinter()
        stop = asyncio.Event()

        receiver = asyncio.create_task(_receiver(ws, executor, printer))
        stdin = asyncio.create_task(_stdin_sender(ws, stop))
        done, pending = await asyncio.wait(
            [receiver, stdin], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                raise task.exception()  # type: ignore[misc]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Forge voice-free text console.")
    parser.add_argument("--ws-url", default=DEFAULT_WS_ROOT_URL, help="WebSocket root URL.")
    parser.add_argument("--user", default=None, help="User id (default: stable per-machine id).")
    parser.add_argument("--blender-host", default="localhost")
    parser.add_argument("--blender-port", type=int, default=9876)
    parser.add_argument("--blender-app", default=DEFAULT_BLENDER_APP)
    parser.add_argument(
        "--no-launch-blender",
        action="store_true",
        help="Attach to an already-running Blender; never spawn it.",
    )
    args = parser.parse_args(argv)

    user_id = args.user or get_stable_user_id()
    ws_root = _build_ws_root(args.ws_url, user_id)

    print(f"[blender] ensuring Blender is up on {args.blender_host}:{args.blender_port} ...", flush=True)
    try:
        handle = ensure_blender_running(
            port=args.blender_port,
            host=args.blender_host,
            blender_app=args.blender_app,
            spawn=not args.no_launch_blender,
        )
    except (FileNotFoundError, RuntimeError, TimeoutError) as exc:
        print(f"[blender] ERROR: {exc}", file=sys.stderr, flush=True)
        return 2
    print(
        f"[blender] {'attached to running' if handle.attached else 'launched'} "
        f"Blender on port {handle.port}",
        flush=True,
    )

    bridge = BlenderConnection(host=args.blender_host, port=args.blender_port)
    try:
        asyncio.run(_run(ws_root, bridge))
    except KeyboardInterrupt:
        pass
    finally:
        bridge.disconnect()
        print("\n[done] text console closed (Blender left running).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
