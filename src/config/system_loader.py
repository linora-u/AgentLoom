"""Strict, credential-free loading for project ``config/system.yaml``."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config_validation import validate_system_snapshot
from .layered_builder import LayeredConfigBuilder
from .yaml_loader import load_unique_yaml

SYSTEM_CONFIG_NAME = "system.yaml"
_LLM_ONLY_TOP_LEVEL_KEYS = {"model", "llm", "langfuse"}

logger = logging.getLogger(__name__)


def load_config_mapping(
    path: Path,
    *,
    require_exists: bool = False,
) -> dict[str, Any]:
    """Load one YAML mapping, rejecting duplicate keys and non-file nodes."""

    if path.is_symlink():
        raise ValueError(f"Configuration path must not be a symlink: {path}")
    if not path.exists():
        if require_exists:
            raise FileNotFoundError(f"Configuration file does not exist: {path}")
        return {}
    if not path.is_file():
        raise ValueError(f"Configuration path must be a regular file: {path}")
    with path.open("r", encoding="utf-8") as stream:
        loaded = load_unique_yaml(stream) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Configuration file must contain a mapping: {path}")
    return loaded


def filter_llm_only_top_level_keys(
    config_map: dict[str, Any] | None,
    *,
    source_name: str,
) -> dict[str, Any]:
    """Keep model credentials out of system configuration layers."""

    if not config_map:
        return {}
    filtered: dict[str, Any] = {}
    for key, value in config_map.items():
        if key in _LLM_ONLY_TOP_LEVEL_KEYS:
            logger.warning(
                "Ignoring top-level key '%s' in %s; LLM settings must come from config/llm.yaml only.",
                key,
                source_name,
            )
            continue
        filtered[key] = value
    return filtered


def load_project_system_config(
    project_root: Path | str,
    *,
    require_exists: bool = True,
) -> dict[str, Any]:
    """Load and validate a project's system config without reading LLM secrets."""

    agent_root = Path(project_root).expanduser().resolve()
    system_path = agent_root / "config" / SYSTEM_CONFIG_NAME
    system_yaml = filter_llm_only_top_level_keys(
        load_config_mapping(system_path, require_exists=require_exists),
        source_name="config/system.yaml",
    )
    layered_builder = LayeredConfigBuilder(
        validate_hook=lambda snapshot, overlay: validate_system_snapshot(
            snapshot,
            overlay.name,
        )
    )
    layered_builder.apply_mapping("config/system.yaml", system_yaml)
    return layered_builder.build()
