"""client.local_executor

Receives ``tool_call`` frames from the server (Seam 1) and forwards them to the
local Blender socket bridge (Seam 2). This is the Forge replacement for Wand's
browser executor: the generic message envelope is kept verbatim in spirit
(validation, dedup, ``tool_result`` success/error, trace emit), but every tool is
relayed to ``blender_bridge.send_command`` — the actual tool *set*
(``execute_blender_code``, ``pick_object_at``, …) is defined server-side in
Step 5. The client stays a thin, tool-agnostic relay.

The bridge socket call is synchronous; it is offloaded to a thread via
``run_in_executor`` so the asyncio loop (audio, cursor) is never blocked.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections import deque
from typing import Any, Callable, Deque, Optional, Set

from client.blender_bridge import BlenderConnection, BlenderError
from client.ws_guard import WSSender


class LocalToolExecutor:
    def __init__(
        self,
        bridge: BlenderConnection,
        sender: WSSender,
        *,
        event_callback: Optional[Callable[[dict[str, Any]], None]] = None,
        region_point_provider: Optional[Callable[[], tuple[float, float] | None]] = None,
    ) -> None:
        self.bridge = bridge
        self.sender = sender
        self.event_callback = event_callback
        self.region_point_provider = region_point_provider
        self._recent_ids: Deque[str] = deque(maxlen=256)
        self._recent_id_set: Set[str] = set()

    async def handle_message(self, payload: dict[str, Any]) -> bool:
        if payload.get("type") != "tool_call":
            return False

        call_id = payload.get("call_id")
        tool = payload.get("tool")
        args = payload.get("args", {})

        if not isinstance(call_id, str) or not call_id:
            return True
        if not isinstance(tool, str) or not tool:
            await self._send_error(call_id, "Missing tool name.")
            return True
        if not isinstance(args, dict):
            await self._send_error(call_id, "Tool arguments must be a JSON object.")
            return True

        if call_id in self._recent_id_set:
            return True

        try:
            self._emit(
                {
                    "event": "tool_result_received",
                    "status": "started",
                    "summary": f"local executor handling {tool}",
                    "tool_name": tool,
                    "request_id": call_id,
                }
            )
            result = await self._dispatch(tool, args, call_id=call_id)
        except Exception as exc:
            await self._send_error(call_id, str(exc))
            self._emit(
                {
                    "event": "tool_result_received",
                    "status": "error",
                    "summary": str(exc),
                    "tool_name": tool,
                    "request_id": call_id,
                }
            )
        else:
            await self.sender.send_json(
                "tool_result",
                {
                    "type": "tool_result",
                    "call_id": call_id,
                    "ok": True,
                    "result": result,
                },
            )
            self._emit(
                {
                    "event": "tool_result_received",
                    "status": "ok",
                    "summary": str(result),
                    "tool_name": tool,
                    "request_id": call_id,
                }
            )

        self._remember_call_id(call_id)
        return True

    async def _dispatch(self, tool: str, args: dict[str, Any], *, call_id: str = "") -> dict:
        """Relay the tool call to the Blender bridge on a worker thread.

        The bridge returns the addon's ``result`` dict (or raises BlenderError).
        Non-dict results are wrapped so the ``tool_result`` payload stays a JSON
        object.
        """
        command, params = self._prepare_blender_command(tool, args, call_id=call_id)
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, self.bridge.send_command, command, params)
        except BlenderError as exc:
            raise RuntimeError(f"Blender error: {exc}") from exc
        return result if isinstance(result, dict) else {"result": result}

    def _prepare_blender_command(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        call_id: str,
    ) -> tuple[str, dict[str, Any]]:
        """Apply the small Seam-1 -> Seam-2 adaptations from CONTRACTS.md §4."""
        command = "execute_code" if tool == "execute_blender_code" else tool
        params = dict(args)

        if tool == "pick_object_at" and (
            "region_x" not in params or "region_y" not in params
        ):
            if self.region_point_provider is None:
                raise RuntimeError("no calibrated cursor")
            point = self.region_point_provider()
            if point is None:
                raise RuntimeError("no calibrated cursor")
            params["region_x"] = int(round(point[0]))
            params["region_y"] = int(round(point[1]))

        if tool == "get_viewport_screenshot" and not params.get("filepath"):
            safe_call_id = "".join(ch for ch in call_id if ch.isalnum() or ch in "-_") or "shot"
            params["filepath"] = os.path.join(
                tempfile.gettempdir(),
                f"forge_viewport_{safe_call_id}.png",
            )
            params.setdefault("format", "png")

        return command, params

    async def _send_error(self, call_id: str, message: str) -> None:
        await self.sender.send_json(
            "tool_result",
            {
                "type": "tool_result",
                "call_id": call_id,
                "ok": False,
                "error": message,
            },
        )

    def _remember_call_id(self, call_id: str) -> None:
        if call_id in self._recent_id_set:
            return
        if len(self._recent_ids) == self._recent_ids.maxlen:
            old_id = self._recent_ids.popleft()
            self._recent_id_set.discard(old_id)
        self._recent_ids.append(call_id)
        self._recent_id_set.add(call_id)

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.event_callback is None:
            return
        self.event_callback(payload)
