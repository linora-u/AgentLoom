"""Smol-owned execution options; only runtime_options is interpreted."""
from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from agentloom.configuration.config import EffectiveAgentConfigSnapshot

if TYPE_CHECKING:
    from agentloom.runtime.agent_runtime import RuntimeDefinition


SMOL_DEFAULTS = {
    "max_steps": 80,
    "planning_interval": None,
    "smart_summary": True,
    "todo_mode": "auto",
    "prompt_template_path": None,
    "max_consecutive_model_errors": 5,
}


def resolve_execution_prompt_template_path(raw_path: str, source: str, *, agent_root: Path | str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"{source} must be a non-empty string path")
    path = Path(raw_path.strip()).expanduser()
    return (path if path.is_absolute() else Path(agent_root).expanduser() / path).resolve()


def normalize_runtime_options(
    config: dict, *, snapshot: EffectiveAgentConfigSnapshot | None = None,
    agent_root: Path | str,
) -> tuple[dict[str, Any], dict[str, str]]:
    from agentloom.application.runtime_options import runtime_config_layers

    source, layers = runtime_config_layers(config, snapshot)
    options: dict[str, Any] = dict(SMOL_DEFAULTS)
    sources = {name: "default:smolagents" for name in options}
    for layer_source, data in layers:
        raw = data.get("runtime_options", {})
        if not isinstance(raw, dict):
            raise ValueError(f"{layer_source}:runtime_options must be a mapping")
        for key, value in raw.items():
            if not isinstance(key, str):
                raise ValueError(f"{layer_source}:runtime_options keys must be strings")
            options[key] = value
            sources[key] = f"{layer_source}:runtime_options.{key}"
    unknown = sorted(set(options) - set(SMOL_DEFAULTS))
    if unknown:
        raise ValueError(f"{source}: unsupported smolagents runtime_options: {', '.join(unknown)}")
    for name in ("max_steps", "max_consecutive_model_errors", "planning_interval"):
        value = options[name]
        if name == "planning_interval" and value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{sources[name]} must be a positive integer")
    if not isinstance(options["smart_summary"], bool):
        raise ValueError(f"{sources['smart_summary']} must be a boolean")
    if not isinstance(options["todo_mode"], str) or options["todo_mode"] not in {"auto", "on", "off"}:
        raise ValueError(f"{sources['todo_mode']} must be auto, on or off")
    prompt = options["prompt_template_path"]
    if prompt is not None:
        options["prompt_template_path"] = str(resolve_execution_prompt_template_path(
            prompt, sources["prompt_template_path"], agent_root=agent_root,
        ))
    return options, sources


def options_from_definition(definition: RuntimeDefinition) -> dict[str, Any]:
    """Interpret smol options entirely inside the adapter."""
    normalized, _ = normalize_runtime_options(
        {"name": definition.name, "runtime_options": dict(definition.runtime_options)},
        agent_root=definition.project_root or Path.cwd(),
    )
    return normalized
