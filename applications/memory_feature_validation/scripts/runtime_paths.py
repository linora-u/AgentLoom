"""Canonical runtime paths for memory validation entry points."""

from __future__ import annotations

from pathlib import Path

import yaml
from agentloom.runtime import resolve_runtime_home


def canonical_runtime_root(repo_root: Path) -> Path:
    """Resolve the validation root through AgentLoom's single path authority."""

    system_path = repo_root / "config" / "system.yaml"
    try:
        system = yaml.safe_load(system_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        system = {}
    return resolve_runtime_home(system, agent_root=repo_root).root_dir
