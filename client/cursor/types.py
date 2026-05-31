"""client.cursor.types

Small value types shared across the cursor pipeline (lifted from Wand).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

CursorSource = Literal["hand", "mouse"]


@dataclass(frozen=True)
class NormalizedSample:
    """Index-fingertip position in normalized camera coords (0..1), y-down."""

    x: float
    y: float
    ts: float
    confidence: Optional[float] = None


@dataclass(frozen=True)
class HandObservation:
    """MediaPipe Hand Landmarker output for one detected hand.

    The task emits normalized image landmarks, optional world landmarks, and
    handedness categories. Gesture code can use image landmarks for cursor
    placement and world landmarks for more stable shape/distance checks.
    """

    image_landmarks: list[Any]
    world_landmarks: list[Any] | None
    handedness: str | None
    handedness_score: Optional[float]
    ts: float


@dataclass(frozen=True)
class CursorSample:
    x: int
    y: int
    ts: float
    source: CursorSource
    confidence: Optional[float] = None


@dataclass(frozen=True)
class TrackerHealth:
    running: bool
    last_error: Optional[str]
    last_seen_ts: Optional[float]
    frames_seen: int
