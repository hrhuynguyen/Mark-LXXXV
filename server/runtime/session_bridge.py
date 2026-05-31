"""server.runtime.session_bridge

Server<->client tool_call/result bridge over WebSocket (reused from Wand). Step 5.
"""

from __future__ import annotations

from typing import Any, Protocol


class SessionBridge(Protocol):
    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Call a local client-owned tool and return its JSON result."""
