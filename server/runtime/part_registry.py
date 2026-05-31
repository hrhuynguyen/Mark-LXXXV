"""Session-scoped part registry for Forge builds.

The registry stores only JSON-serializable metadata keyed by Blender object name.
It deliberately never stores live ``bpy`` objects or other process-local handles.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable, Mapping, Optional
from uuid import uuid4

from server.live.trace import PROTOCOL_VERSION


PART_KINDS = frozenset({"procedural", "generated", "library_asset"})


class PartRegistryError(ValueError):
    """Raised when registry data violates the Forge registry contract."""


class PartNotFoundError(KeyError):
    """Raised when a named part is not present in a registry."""


def _json_copy(value: Any, *, field_name: str) -> Any:
    """Return a JSON-only copy, raising a targeted error for non-serializable values."""
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise PartRegistryError(f"{field_name} must be JSON-serializable") from exc


def _validate_name(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PartRegistryError(f"{field_name} must be a non-empty string")
    return value.strip()


def _validate_optional_name(value: Any, *, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return _validate_name(value, field_name=field_name)


def _validate_bbox(value: Any) -> Optional[list[list[float]]]:
    if value is None:
        return None
    copied = _json_copy(value, field_name="bbox")
    if (
        not isinstance(copied, list)
        or len(copied) != 2
        or any(not isinstance(point, list) or len(point) != 3 for point in copied)
    ):
        raise PartRegistryError("bbox must be [[min_x,min_y,min_z],[max_x,max_y,max_z]]")
    return [[float(coord) for coord in point] for point in copied]


@dataclass(frozen=True)
class PartEntry:
    """JSON-serializable metadata for one Blender object."""

    name: str
    kind: str
    params: dict[str, Any]
    generator: Optional[str] = None
    parent: Optional[str] = None
    bbox: Optional[list[list[float]]] = None

    def __post_init__(self) -> None:
        name = _validate_name(self.name, field_name="name")
        kind = _validate_name(self.kind, field_name="kind")
        if kind not in PART_KINDS:
            raise PartRegistryError(f"kind must be one of {sorted(PART_KINDS)}")
        if not isinstance(self.params, Mapping):
            raise PartRegistryError("params must be a JSON object")

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "params", _json_copy(dict(self.params), field_name="params"))
        object.__setattr__(
            self,
            "generator",
            _validate_optional_name(self.generator, field_name="generator"),
        )
        object.__setattr__(self, "parent", _validate_optional_name(self.parent, field_name="parent"))
        object.__setattr__(self, "bbox", _validate_bbox(self.bbox))

    @classmethod
    def from_json(cls, payload: Mapping[str, Any], *, name: Optional[str] = None) -> "PartEntry":
        data = dict(payload)
        if name is not None and "name" not in data:
            data["name"] = name
        return cls(
            name=data.get("name"),
            kind=data.get("kind"),
            params=data.get("params", {}),
            generator=data.get("generator"),
            parent=data.get("parent"),
            bbox=data.get("bbox"),
        )

    @classmethod
    def from_build_spec_part(
        cls,
        part: Mapping[str, Any],
        *,
        kind: str = "procedural",
        bbox: Any = None,
    ) -> "PartEntry":
        return cls(
            name=part.get("name"),
            kind=kind,
            params=dict(part.get("params") or {}),
            generator=part.get("generator"),
            parent=part.get("parent"),
            bbox=bbox,
        )

    def to_json(self) -> dict[str, Any]:
        return _json_copy(asdict(self), field_name=f"part {self.name}")

    def with_updates(self, **changes: Any) -> "PartEntry":
        return replace(self, **changes)


class PartRegistry:
    """Mutable registry for one build in one live session."""

    def __init__(
        self,
        *,
        build_id: Optional[str] = None,
        object_name: Optional[str] = None,
        protocol_version: str = PROTOCOL_VERSION,
        parts: Optional[Iterable[PartEntry]] = None,
    ) -> None:
        self.build_id = _validate_name(build_id or str(uuid4()), field_name="build_id")
        self.object_name = _validate_optional_name(object_name, field_name="object")
        self.protocol_version = _validate_name(protocol_version, field_name="protocol_version")
        self.parts: dict[str, PartEntry] = {}
        if parts is not None:
            for entry in parts:
                self.add(entry)

    def __len__(self) -> int:
        return len(self.parts)

    def __contains__(self, name: str) -> bool:
        return name in self.parts

    def add(self, entry: PartEntry, *, replace_existing: bool = True) -> PartEntry:
        if not replace_existing and entry.name in self.parts:
            raise PartRegistryError(f"part already registered: {entry.name}")
        self.parts[entry.name] = entry
        return entry

    def add_from_build_spec_part(
        self,
        part: Mapping[str, Any],
        *,
        kind: str = "procedural",
        bbox: Any = None,
        replace_existing: bool = True,
    ) -> PartEntry:
        return self.add(
            PartEntry.from_build_spec_part(part, kind=kind, bbox=bbox),
            replace_existing=replace_existing,
        )

    def register_tool_result(
        self,
        *,
        part: Mapping[str, Any],
        result: Mapping[str, Any],
        kind: str = "procedural",
    ) -> PartEntry:
        """Register a build/regenerate result against the Build-Spec part that produced it."""
        name = result.get("name") or part.get("name")
        bbox = result.get("world_bounding_box") or result.get("bbox")
        spec = dict(part)
        spec["name"] = name
        return self.add_from_build_spec_part(spec, kind=kind, bbox=bbox)

    def get(self, name: str) -> Optional[PartEntry]:
        return self.parts.get(name)

    def require(self, name: str) -> PartEntry:
        entry = self.get(name)
        if entry is None:
            raise PartNotFoundError(name)
        return entry

    def update(self, name: str, **changes: Any) -> PartEntry:
        entry = self.require(name)
        updated = entry.with_updates(**changes)
        self.parts[name] = updated
        return updated

    def update_params(self, name: str, params: Mapping[str, Any], *, merge: bool = True) -> PartEntry:
        entry = self.require(name)
        next_params = dict(entry.params) if merge else {}
        next_params.update(dict(params))
        return self.update(name, params=next_params)

    def update_bbox(self, name: str, bbox: Any) -> PartEntry:
        return self.update(name, bbox=bbox)

    def remove(self, name: str) -> Optional[PartEntry]:
        return self.parts.pop(name, None)

    def children(self, parent: str) -> list[PartEntry]:
        return [entry for entry in self.parts.values() if entry.parent == parent]

    def names(self) -> list[str]:
        return list(self.parts.keys())

    def enrich_pick_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        """Attach registry metadata to a successful pick result when available."""
        enriched = dict(result)
        if not enriched.get("hit"):
            return enriched
        name = enriched.get("name")
        if not isinstance(name, str):
            return enriched
        entry = self.get(name)
        enriched["registry"] = entry.to_json() if entry is not None else None
        return enriched

    def scene_name_report(self, scene_object_names: Iterable[str]) -> dict[str, list[str]]:
        scene_names = {str(name) for name in scene_object_names}
        registry_names = set(self.parts)
        return {
            "registered_missing_in_scene": sorted(registry_names - scene_names),
            "scene_unregistered": sorted(scene_names - registry_names),
        }

    def parts_to_json(self) -> dict[str, dict[str, Any]]:
        return {name: entry.to_json() for name, entry in self.parts.items()}

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "build_id": self.build_id,
            "protocol_version": self.protocol_version,
            "parts": self.parts_to_json(),
        }
        if self.object_name is not None:
            payload["object"] = self.object_name
        return _json_copy(payload, field_name="registry")

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "PartRegistry":
        parts_payload = payload.get("parts", {})
        if not isinstance(parts_payload, Mapping):
            raise PartRegistryError("registry parts must be a JSON object")
        registry = cls(
            build_id=payload.get("build_id"),
            object_name=payload.get("object"),
            protocol_version=payload.get("protocol_version", PROTOCOL_VERSION),
        )
        for name, part_payload in parts_payload.items():
            if not isinstance(part_payload, Mapping):
                raise PartRegistryError(f"part payload for {name} must be a JSON object")
            registry.add(PartEntry.from_json(part_payload, name=str(name)))
        return registry


_registry_lock = threading.RLock()
_registries: dict[tuple[str, str], PartRegistry] = {}


def get_part_registry(user_id: str, session_id: str, *, create: bool = True) -> Optional[PartRegistry]:
    key = (str(user_id), str(session_id))
    with _registry_lock:
        registry = _registries.get(key)
        if registry is None and create:
            registry = PartRegistry()
            _registries[key] = registry
        return registry


def replace_part_registry(user_id: str, session_id: str, registry: PartRegistry) -> PartRegistry:
    key = (str(user_id), str(session_id))
    with _registry_lock:
        _registries[key] = registry
        return registry


def clear_part_registry(user_id: str, session_id: str) -> Optional[PartRegistry]:
    key = (str(user_id), str(session_id))
    with _registry_lock:
        return _registries.pop(key, None)
