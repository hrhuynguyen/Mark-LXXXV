"""ADK tools that forward Blender work to the local companion client."""

from __future__ import annotations

from time import time
from typing import Any, Dict
from uuid import uuid4

from google.adk.tools.tool_context import ToolContext

from server.runtime.session_bridge import call_local_tool, emit_server_trace


AGENT_NAME = "blender_agent"


def _get_uid_sid(tc: ToolContext) -> tuple[str, str]:
    user_id = str(getattr(tc, "user_id", "") or "")
    session = getattr(tc, "session", None)
    session_id = str(getattr(session, "id", "") or "")
    if user_id and session_id:
        return user_id, session_id

    inv = getattr(tc, "_invocation_context", None)
    user_id = str(getattr(inv, "user_id", "") or user_id or "unknown_user")
    sess = getattr(inv, "session", None)
    session_id = str(getattr(sess, "id", "") or session_id or "unknown_session")
    return user_id, session_id


async def _call(tool_context: ToolContext, tool_name: str, args: Dict[str, Any]) -> dict:
    user_id, session_id = _get_uid_sid(tool_context)
    request_id = str(uuid4())
    t0 = time()
    await emit_server_trace(
        user_id=user_id,
        session_id=session_id,
        request_id=request_id,
        event="agent_started",
        status="started",
        summary=f"{AGENT_NAME} handling {tool_name}",
        agent_name=AGENT_NAME,
        tool_name=tool_name,
    )
    try:
        result = await call_local_tool(
            user_id=user_id,
            session_id=session_id,
            tool=tool_name,
            args=args,
            request_id=request_id,
            agent_name=AGENT_NAME,
        )
    except Exception as exc:
        await emit_server_trace(
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            event="agent_finished",
            status="error",
            summary=str(exc),
            agent_name=AGENT_NAME,
            tool_name=tool_name,
            duration_ms=int((time() - t0) * 1000),
        )
        raise
    await emit_server_trace(
        user_id=user_id,
        session_id=session_id,
        request_id=request_id,
        event="agent_finished",
        status="ok",
        summary=f"{AGENT_NAME} completed {tool_name}",
        agent_name=AGENT_NAME,
        tool_name=tool_name,
        duration_ms=int((time() - t0) * 1000),
    )
    return result


async def execute_blender_code(tool_context: ToolContext, code: str) -> dict:
    """Execute Python code inside Blender."""
    return await _call(tool_context, "execute_blender_code", {"code": code})


async def pick_object_at(tool_context: ToolContext) -> dict:
    """Pick the object under the user's current calibrated cursor."""
    return await _call(tool_context, "pick_object_at", {})


async def get_object_info(tool_context: ToolContext, name: str) -> dict:
    return await _call(tool_context, "get_object_info", {"name": name})


async def get_viewport_screenshot(tool_context: ToolContext, max_size: int = 800) -> dict:
    return await _call(tool_context, "get_viewport_screenshot", {"max_size": int(max_size)})


async def frame_object(tool_context: ToolContext, name: str) -> dict:
    return await _call(tool_context, "frame_object", {"name": name})
