"""Apply runtime configuration overlays to canonical tool metadata."""

from __future__ import annotations

from dataclasses import fields, replace
from typing import Any

from src.lib.config import C
from src.lib.logging import get_logger

from .catalog import ToolSpec, get_tool_spec

logger = get_logger(__name__)

_OVERRIDABLE_SPEC_FIELDS = {
    "max_result_chars",
    "is_concurrency_safe",
    "category",
    "description",
    "output_kind",
}


def _load_tool_metadata_from_config() -> dict[str, dict[str, Any]]:
    raw = C.get("tool_metadata", {})
    if not isinstance(raw, dict):
        logger.warning("tool_metadata config is not a dict, ignoring: %s", type(raw))
        return {}
    return raw


def _filtered_overrides(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if key in _OVERRIDABLE_SPEC_FIELDS and value is not None
    }


def get_tool_meta(
    tool_name: str,
    agent_tool_overrides: dict[str, Any] | None = None,
) -> ToolSpec:
    """Return the registered ``ToolSpec`` with metadata overrides applied.

    Resolution is catalog-first. Unknown tool names fail instead of falling
    back to convention-based imports.
    """
    spec = get_tool_spec(tool_name)
    yaml_meta = _load_tool_metadata_from_config()

    merged: dict[str, Any] = {}
    merged.update(_filtered_overrides(yaml_meta.get("default")))
    merged.update(_filtered_overrides(yaml_meta.get(spec.name)))
    merged.update(_filtered_overrides(agent_tool_overrides))

    if not merged:
        return spec

    known_fields = {field.name for field in fields(ToolSpec)}
    filtered = {key: value for key, value in merged.items() if key in known_fields}
    return replace(spec, **filtered)


__all__ = ["get_tool_meta"]
