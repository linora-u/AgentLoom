"""Layered immutable config merge builder."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class OverlaySpec:
    """A named config overlay layer."""

    name: str
    data: Mapping[str, Any]


class LayeredConfigBuilder:
    """Build merged config from immutable layers with per-layer validation."""

    def __init__(
        self,
        *,
        validate_hook: Callable[[dict[str, Any], OverlaySpec], None] | None = None,
    ) -> None:
        self._current: dict[str, Any] = {}
        self._validate_hook = validate_hook
        self._applied: list[OverlaySpec] = []

    @staticmethod
    def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> None:
        for key, value in overlay.items():
            if isinstance(value, Mapping) and isinstance(base.get(key), dict):
                LayeredConfigBuilder._deep_merge(base[key], value)
                continue
            # Lists and scalars are replaced as a whole.
            base[key] = deepcopy(value)

    def apply_overlay(self, overlay: OverlaySpec) -> "LayeredConfigBuilder":
        overlay_data = dict(overlay.data)
        self._deep_merge(self._current, overlay_data)
        if self._validate_hook is not None:
            self._validate_hook(deepcopy(self._current), overlay)
        self._applied.append(overlay)
        return self

    def apply_mapping(self, name: str, data: Mapping[str, Any] | None) -> "LayeredConfigBuilder":
        if not data:
            return self
        return self.apply_overlay(OverlaySpec(name=name, data=data))

    def build(self) -> dict[str, Any]:
        return deepcopy(self._current)

    @property
    def applied_layers(self) -> tuple[OverlaySpec, ...]:
        return tuple(self._applied)
