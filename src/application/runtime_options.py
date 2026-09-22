"""Project layered runtime options without interpreting backend execution policy."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agentloom.configuration.config import EffectiveAgentConfigSnapshot
from agentloom.configuration.runtime_options import (
    runtime_config_layers as _runtime_config_layers,
)


def normalize_runtime_options(
    config: dict, *, snapshot: EffectiveAgentConfigSnapshot | None = None,
    agent_root: Path | str,
) -> tuple[dict[str, Any], dict[str, str]]:
    runtime_id = config.get("agent_runtime")
    if runtime_id == "smolagents":
        from agentloom.runtimes.smolagents.options import normalize_runtime_options as normalize_smol

        return normalize_smol(config, snapshot=snapshot, agent_root=agent_root)
    _, layers = _runtime_config_layers(config, snapshot)
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
