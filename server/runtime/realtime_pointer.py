"""Per-session cursor cache for the server UI/trace path."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CursorPosition:
    x: int
    y: int
    ts: float


_lock = asyncio.Lock()
_cursors: dict[tuple[str, str], CursorPosition] = {}


async def set_cursor(user_id: str, session_id: str, *, x: int, y: int) -> CursorPosition:
    pos = CursorPosition(x=int(x), y=int(y), ts=time.time())
    async with _lock:
        _cursors[(str(user_id), str(session_id))] = pos
    return pos


async def get_cursor(user_id: str, session_id: str) -> Optional[CursorPosition]:
    async with _lock:
        return _cursors.get((str(user_id), str(session_id)))


async def clear_cursor(user_id: str, session_id: str) -> None:
    async with _lock:
        _cursors.pop((str(user_id), str(session_id)), None)
