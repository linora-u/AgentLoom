"""Layered runtime option sources without backend-specific interpretation."""

from __future__ import annotations

from agentloom.configuration.config import EffectiveAgentConfigSnapshot


def runtime_config_layers(
    config: dict,
    snapshot: EffectiveAgentConfigSnapshot | None,
) -> tuple[str, list[tuple[str, dict]]]:
    source = str(config.get("_yaml_file_path") or config.get("name", "agent"))
    layers = (
        [(source, config)]
        if snapshot is None
        else [
            (str(layer.source_path), layer.data)
            for layer in snapshot.layers
        ]
    )
    if snapshot is not None:
        layers.append((source, config))
    return source, layers
