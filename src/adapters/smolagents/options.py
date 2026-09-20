"""Smol-owned configuration, defaults and legacy YAML compatibility.

This module is metadata-only: validation never imports the native SDK or tools.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from agentloom.configuration.config import EffectiveAgentConfigSnapshot
from agentloom.configuration.config_validation import TODO_MODES, normalize_todo_mode_value

if TYPE_CHECKING:
    from agentloom.runtime.agent_runtime import RuntimeDefinition

@dataclass(frozen=True)
class NormalizedExecutionConfig:
    """Supported execution settings shared by preflight and construction."""

    prompt_template_path: str | None
    planning_interval: int | None = None


def _resolve_agent_root(agent_root: Path | str) -> Path:
    return Path(agent_root).expanduser().resolve()


def resolve_execution_prompt_template_path(
    raw_path: str,
    source: str,
    *,
    agent_root: Path | str,
) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"{source} must be a non-empty string path")
    path_obj = Path(raw_path.strip()).expanduser()
    if not path_obj.is_absolute():
        path_obj = (_resolve_agent_root(agent_root) / path_obj).resolve()
    else:
        path_obj = path_obj.resolve()
    return path_obj


def normalize_execution_prompt_template_path_value(
    raw_prompt: Any,
    source: str,
    *,
    agent_root: Path | str,
) -> str | None:
    if raw_prompt is None:
        return None

    raw_path: Any
    if isinstance(raw_prompt, str):
        raw_path = raw_prompt
    elif isinstance(raw_prompt, dict):
        if "path" not in raw_prompt:
            raise ValueError(f"{source} must include 'path' when prompt is a mapping")
        raw_path = raw_prompt.get("path")
    else:
        raise ValueError(f"{source} must be a string or mapping with 'path'")

    resolved = resolve_execution_prompt_template_path(
        raw_path,
        f"{source} path",
        agent_root=agent_root,
    )
    return str(resolved)


def normalize_execution_prompt_template_path(
    config: dict,
    source: str,
    *,
    agent_root: Path | str,
) -> str | None:
    return normalize_execution_prompt_template_path_value(
        config.get("prompt"),
        source,
        agent_root=agent_root,
    )


def normalize_execution_planning_interval_value(raw_value: Any) -> int | None:
    return normalize_positive_int_value(raw_value)


def validate_todo_config(config: dict, *, source: str) -> str:
    """Validate and return the effective current-task Todo mode."""

    raw_todo = config.get("todo", {})
    if not isinstance(raw_todo, dict):
        raise ValueError(f"{source}.todo must be a mapping")
    unexpected = sorted(set(raw_todo) - {"mode"})
    if unexpected:
        raise ValueError(
            f"{source}.todo has unsupported field(s): {', '.join(unexpected)}"
        )
    raw_mode = normalize_todo_mode_value(raw_todo.get("mode", "auto"))
    if not isinstance(raw_mode, str) or raw_mode not in TODO_MODES:
        allowed = ", ".join(sorted(TODO_MODES))
        raise ValueError(f"{source}.todo.mode must be one of: {allowed}")
    return raw_mode


def normalize_positive_int_value(raw_value: Any) -> int | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, int):
        return raw_value if raw_value > 0 else None
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return None
        try:
            parsed = int(text)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def build_normalized_execution_config(
    config: dict,
    *,
    source_name: str,
    agent_root: Path | str,
) -> NormalizedExecutionConfig:
    name = str(config.get("name", source_name))
    prompt_template_path = normalize_execution_prompt_template_path(
        config,
        source=f"{name}.prompt",
        agent_root=agent_root,
    )
    planning_interval = normalize_execution_planning_interval_value(config.get("planning_interval"))

    return NormalizedExecutionConfig(
        prompt_template_path=prompt_template_path,
        planning_interval=planning_interval,
    )


def validate_execution_config_payload(normalized: Any) -> NormalizedExecutionConfig:
    if not isinstance(normalized, NormalizedExecutionConfig):
        raise ValueError("execution normalized config must be NormalizedExecutionConfig")

    prompt_template_path = normalized.prompt_template_path
    if prompt_template_path is not None:
        if not isinstance(prompt_template_path, str) or not prompt_template_path.strip():
            raise ValueError("execution normalized prompt_template_path must be a non-empty string when provided")
        prompt_template_path = prompt_template_path.strip()

    planning_interval = normalized.planning_interval
    if planning_interval is not None:
        if isinstance(planning_interval, bool) or not isinstance(planning_interval, int) or planning_interval <= 0:
            raise ValueError("execution normalized planning_interval must be a positive integer when provided")

    return NormalizedExecutionConfig(
        prompt_template_path=prompt_template_path,
        planning_interval=planning_interval,
    )


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
    from agentloom.application.runtime_options import runtime_config_layers

    source, layers = runtime_config_layers(config, snapshot)
    legacy: dict[str, Any] = {}
    legacy_sources: dict[str, str] = {}
    options: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for layer_source, data, _ in layers:
        for key, name in LEGACY_OPTIONS.items():
            if key not in data:
                continue
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
            if key == "prompt_template_path":
                value = normalize_execution_prompt_template_path_value(
                    value, f"{layer_source}:runtime_options.{key}", agent_root=agent_root,
                )
            options[key] = value
            sources[key] = f"{layer_source}:runtime_options.{key}"
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
    return options, sources


def options_from_definition(definition: RuntimeDefinition) -> dict[str, Any]:
    """Accept 02's Python compatibility fields without projecting them back to core."""
    options = dict(definition.runtime_options)
    for name in SMOL_DEFAULTS:
        legacy = getattr(definition, name)
        if legacy is not None:
            if name in options and options[name] != legacy:
                raise ValueError(f"Conflicting smolagents runtime option: {name}")
            options[name] = legacy
    normalized, _ = normalize_runtime_options(
        {"name": definition.name, "agent_runtime": "smolagents", "runtime_options": options},
        agent_root=definition.project_root or Path.cwd(),
    )
    return normalized
