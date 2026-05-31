"""server.runtime.part_registry

PartEntry + PartRegistry + Build Spec. JSON-serializable: store names+params only, never live bpy refs. Step 6.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PartEntry:
    name: str
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    generator: str | None = None
    parent: str | None = None
    bbox: Any | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PartEntry":
        return cls(
            name=str(data["name"]),
            kind=str(data.get("kind") or "procedural"),
            params=dict(data.get("params") or {}),
            generator=data.get("generator"),
            parent=data.get("parent"),
            bbox=data.get("bbox"),
        )

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PartRegistry:
    object: str | None = None
    parts: dict[str, PartEntry] = field(default_factory=dict)

    def add(self, part: PartEntry) -> None:
        self.parts[part.name] = part

    def get(self, name: str) -> PartEntry | None:
        return self.parts.get(name)

    def update(self, name: str, **changes: Any) -> PartEntry:
        part = self.parts[name]
        for key, value in changes.items():
            if not hasattr(part, key):
                raise AttributeError(f"unknown PartEntry field: {key}")
            setattr(part, key, value)
        return part

    def children(self, parent: str) -> list[PartEntry]:
        return [part for part in self.parts.values() if part.parent == parent]

    def to_json(self) -> dict[str, Any]:
        return {
            "object": self.object,
            "parts": [part.to_json() for part in self.parts.values()],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PartRegistry":
        registry = cls(object=data.get("object"))
        for raw in data.get("parts") or []:
            part = PartEntry.from_json(raw)
            registry.add(part)
        return registry
