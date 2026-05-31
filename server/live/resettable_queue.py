"""LiveRequestQueue with transfer cleanup helpers from Wand."""

from __future__ import annotations

import asyncio

from google.adk.agents.live_request_queue import LiveRequest, LiveRequestQueue
from google.genai import types


class ResettableLiveRequestQueue(LiveRequestQueue):
    def send_content(self, content: types.Content) -> None:
        # ADK may enqueue a transfer_to_agent function response for the next
        # live session. Drop that orphaned response; the transfer already
        # happened and the new agent did not request this function response.
        if content and content.parts:
            for part in content.parts:
                fr = getattr(part, "function_response", None)
                if fr and getattr(fr, "name", None) == "transfer_to_agent":
                    return
        super().send_content(content)

    def drop_realtime_backlog(self) -> int:
        dropped = 0
        kept: list[LiveRequest] = []
        while True:
            try:
                req = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            if req.close or req.content is not None:
                kept.append(req)
                continue
            if req.activity_start or req.activity_end or req.blob is not None:
                dropped += 1
                continue
            kept.append(req)

        for req in kept:
            self._queue.put_nowait(req)
        return dropped
