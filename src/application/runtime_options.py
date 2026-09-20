"""Normalize backend-owned options while retaining legacy configuration sources."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agentloom.application.validation import (
    normalize_execution_prompt_template_path_value,
    normalize_execution_planning_interval_value,
    validate_todo_config,
)
from agentloom.configuration.config import EffectiveAgentConfigSnapshot

SMOL_DEFAULTS = {
    "max_steps": 80,
    "planning_interval": None,
    "smart_summary": True,
    "todo_mode": "auto",
    "prompt_template_path": None,
    "max_consecutive_model_errors": 5,
}
LEGACY_OPTIONS = {
    "max_steps": "max_steps",
    "planning_interval": "planning_interval",
    "smart_summary": "smart_summary",
    "todo": "todo_mode",
    "prompt": "prompt_template_path",
    "max_consecutive_parse_errors": "max_consecutive_model_errors",
}


def normalize_runtime_options(
    config: dict, *, snapshot: EffectiveAgentConfigSnapshot | None = None,
    agent_root: Path | str,
) -> tuple[dict[str, Any], dict[str, str]]:
    runtime_id = config.get("agent_runtime")
    source = str(config.get("_yaml_file_path") or config.get("name", "agent"))
    layers = [(source, config, False)] if snapshot is None else [
        (str(layer.source_path), layer.data, layer.name == "global_system")
        for layer in snapshot.layers
    ]
    # Fields outside the system overlay (for example max_steps) still belong
    # to the Agent YAML and must not lose their explicitness.
    if snapshot is not None:
        layers.append((source, config, False))
    legacy: dict[str, Any] = {}
    legacy_sources: dict[str, str] = {}
    options: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for layer_source, data, historical_global in layers:
        for key, name in LEGACY_OPTIONS.items():
            if key not in data:
                continue
            if runtime_id != "smolagents":
                if historical_global:
                    continue
                raise ValueError(
                    f"{layer_source}:{key} is a smolagents-only option; "
                    f"agent_runtime={runtime_id!r} must use its own runtime_options"
                )
            value = data[key]
            if key == "todo":
                value = validate_todo_config(data, source=layer_source)
            elif key == "prompt":
                value = normalize_execution_prompt_template_path_value(
                    value, f"{layer_source}:prompt", agent_root=agent_root,
                )
            elif key == "planning_interval":
                value = normalize_execution_planning_interval_value(value)
            elif key == "smart_summary" and isinstance(value, str):
                if value.lower() in {"true", "false"}:
                    value = value.lower() == "true"
            legacy[name] = value
            legacy_sources[name] = f"{layer_source}:{key}"
        raw = data.get("runtime_options", {})
        if not isinstance(raw, dict):
            raise ValueError(f"{layer_source}:runtime_options must be a mapping")
        for key, value in raw.items():
            if not isinstance(key, str):
                raise ValueError(f"{layer_source}:runtime_options keys must be strings")
            if runtime_id == "smolagents" and key == "prompt_template_path":
                value = normalize_execution_prompt_template_path_value(
                    value, f"{layer_source}:runtime_options.{key}", agent_root=agent_root,
                )
            options[key] = value
            sources[key] = f"{layer_source}:runtime_options.{key}"
    if runtime_id == "smolagents":
        unknown = sorted(set(options) - set(SMOL_DEFAULTS))
        if unknown:
            raise ValueError(f"{source}: unsupported smolagents runtime_options: {', '.join(unknown)}")
        for name in legacy.keys() & options.keys():
            if legacy[name] != options[name]:
                raise ValueError(
                    f"Conflicting runtime option {name!r}: {legacy_sources[name]} "
                    f"conflicts with {sources[name]}"
                )
        options = {**SMOL_DEFAULTS, **legacy, **options}
        sources = {**{name: "default:smolagents" for name in SMOL_DEFAULTS}, **legacy_sources, **sources}
        for name in ("max_steps", "max_consecutive_model_errors", "planning_interval"):
            value = options[name]
            if name == "planning_interval" and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{sources[name]} must be a positive integer")
        if not isinstance(options["smart_summary"], bool):
            raise ValueError(f"{sources['smart_summary']} must be a boolean")
        if options["todo_mode"] not in {"auto", "on", "off"}:
            raise ValueError(f"{sources['todo_mode']} must be auto, on or off")
        prompt = options["prompt_template_path"]
        if prompt is not None:
            options["prompt_template_path"] = normalize_execution_prompt_template_path_value(
                prompt, sources["prompt_template_path"], agent_root=agent_root,
            )
    if runtime_id == "pi":
        from agentloom.adapters.pi.metadata import validate_options

        validate_options(options)
    return options, sources
