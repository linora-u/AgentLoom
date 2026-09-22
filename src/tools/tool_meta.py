"""Apply runtime configuration overlays to canonical tool metadata."""

from __future__ import annotations

from dataclasses import fields, replace
from typing import Any

from agentloom.configuration import C
from agentloom.execution.logging import get_logger

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


def tool_is_concurrency_safe(tool_name: str, agent_config: dict[str, Any] | None = None) -> bool:
    """Apply catalog and effective Agent metadata to registered or dynamic tools."""
    from .catalog import list_tool_specs
    specs = {spec.name: spec for spec in list_tool_specs()}
    safe = specs[tool_name].is_concurrency_safe if tool_name in specs else True
    metadata = (agent_config or {}).get("tool_metadata", _load_tool_metadata_from_config())
    if not isinstance(metadata, dict):
        raise ValueError("tool_metadata must be a mapping")
    for key in ("default", tool_name):
        value = metadata.get(key, {})
        if isinstance(value, dict) and value.get("is_concurrency_safe") is not None:
            safe = value["is_concurrency_safe"]
    if not isinstance(safe, bool):
        raise ValueError(f"tool_metadata.{tool_name}.is_concurrency_safe must be a boolean")
    return safe
