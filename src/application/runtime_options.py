"""Project layered runtime options without interpreting backend execution policy."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from agentloom.configuration.config import EffectiveAgentConfigSnapshot

def runtime_config_layers(config: dict, snapshot: EffectiveAgentConfigSnapshot | None):
    source = str(config.get("_yaml_file_path") or config.get("name", "agent"))
    layers = [(source, config, False)] if snapshot is None else [
        (str(layer.source_path), layer.data, layer.name == "global_system")
        for layer in snapshot.layers
    ]
    # Fields outside the system overlay (for example max_steps) still belong
    # to the Agent YAML and must not lose their explicitness.
    if snapshot is not None:
        layers.append((source, config, False))
    return source, layers


def normalize_runtime_options(
    config: dict, *, snapshot: EffectiveAgentConfigSnapshot | None = None,
    agent_root: Path | str,
) -> tuple[dict[str, Any], dict[str, str]]:
    runtime_id = config.get("agent_runtime")
    if runtime_id == "smolagents":
        from agentloom.adapters.smolagents.options import normalize_runtime_options as normalize_smol

        return normalize_smol(config, snapshot=snapshot, agent_root=agent_root)
    _, layers = runtime_config_layers(config, snapshot)
    options: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for layer_source, data, _ in layers:
        raw = data.get("runtime_options", {})
        if not isinstance(raw, dict):
            raise ValueError(f"{layer_source}:runtime_options must be a mapping")
        for key, value in raw.items():
            if not isinstance(key, str):
                raise ValueError(f"{layer_source}:runtime_options keys must be strings")
            options[key] = value
            sources[key] = f"{layer_source}:runtime_options.{key}"
    if runtime_id == "pi":
        from agentloom.adapters.pi.metadata import validate_options

        validate_options(options)
    return options, sources
