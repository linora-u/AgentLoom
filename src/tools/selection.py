"""Resolve selected builtin names before importing any implementation."""

from __future__ import annotations

from typing import Any

from .catalog import get_tool_spec, resolve_toolsets


def resolve_runtime_toolsets(
    config: dict[str, Any],
    effective_config: dict[str, Any] | None = None,
) -> list[str]:
    """Share runtime-aware selection between preflight and actual assembly.

    Only historical global defaults may omit another runtime's basics.
    Explicit application/Agent selections require an implemented mapping.
    No native mappings are registered by this compatibility baseline.
    """
    values = {**config, **(effective_config or {})}
    runtime_id = config.get("agent_runtime", "smolagents")
    key = "toolsets" if "toolsets" in values else "default_toolsets"
    raw = values.get(key)
    if effective_config is None and key not in values and runtime_id != "smolagents":
        # Definition-only inspection has not resolved lower configuration layers.
        raw = []
    if key in values and not isinstance(raw, list):
        raise ValueError(f"{key} must be a list of toolset names when provided")
    inherited = (
        key == "default_toolsets"
        and key not in config
        and (values.get("_default_toolsets_source", "global_system") == "global_system")
    )
    names = []

    def validate_selection(spec):
        if runtime_id == "pi" and spec.operation in {"write", "shell"} and spec.provider != "pi":
            raise ValueError(f"Pi does not yet support write/Shell tool '{spec.name}'")

    for name in resolve_toolsets(raw):
        spec = get_tool_spec(name)
        if spec.owner == "runtime" and spec.provider != runtime_id:
            if inherited:
                continue
            raise ValueError(f"Tool '{name}' has no compatible mapping for runtime '{runtime_id}'")
        validate_selection(spec)
        names.append(name)
    for item in values.get("tools") or []:
        if not isinstance(item, dict) or "name" not in item:
            continue  # Structural validation owns the diagnostic.
        if "module" in item or "function" in item:
            if item["name"] in names:
                raise ValueError(f"Duplicate tool name: {item['name']}")
            continue
        try:
            spec = get_tool_spec(item["name"])
        except ValueError:
            # The implementation resolver owns unknown-name diagnostics. This
            # early pass validates ownership of catalogued native selections.
            continue
        if spec.owner == "runtime" and spec.provider != runtime_id:
            raise ValueError(f"Tool '{spec.name}' has no compatible mapping for runtime '{runtime_id}'")
        validate_selection(spec)
    return names
