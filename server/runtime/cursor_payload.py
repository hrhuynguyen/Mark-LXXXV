from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple


def parse_cursor_payload(payload: Mapping[str, Any]) -> Optional[Tuple[int, int]]:
    if payload.get("type") != "cursor":
        return None
    x = payload.get("x")
    y = payload.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    return int(x), int(y)
