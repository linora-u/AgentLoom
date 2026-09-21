"""Project layered runtime options without interpreting backend execution policy."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from agentloom.configuration.config import EffectiveAgentConfigSnapshot

def runtime_config_layers(config: dict, snapshot: EffectiveAgentConfigSnapshot | None):
    source = str(config.get("_yaml_file_path") or config.get("name", "agent"))
    layers = [(source, config)] if snapshot is None else [
        (str(layer.source_path), layer.data)
        for layer in snapshot.layers
    ]
    # The effective snapshot contains project/application overlays. Append the
    # Agent definition so its runtime_options retain highest precedence.
    if snapshot is not None:
        layers.append((source, config))
    return source, layers


def normalize_runtime_options(
    config: dict, *, snapshot: EffectiveAgentConfigSnapshot | None = None,
    agent_root: Path | str,
) -> tuple[dict[str, Any], dict[str, str]]:
    runtime_id = config.get("agent_runtime")
    if runtime_id == "smolagents":
        from agentloom.runtimes.smolagents.options import normalize_runtime_options as normalize_smol

        return normalize_smol(config, snapshot=snapshot, agent_root=agent_root)
    _, layers = runtime_config_layers(config, snapshot)
    options: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for layer_source, data in layers:
        raw = data.get("runtime_options", {})
        if not isinstance(raw, dict):
            raise ValueError(f"{layer_source}:runtime_options must be a mapping")
        for key, value in raw.items():
            if not isinstance(key, str):
                raise ValueError(f"{layer_source}:runtime_options keys must be strings")
            options[key] = value
            sources[key] = f"{layer_source}:runtime_options.{key}"
    if runtime_id == "pi":
        from agentloom.runtimes.pi.metadata import validate_options

        validate_options(options)
    return options, sources
