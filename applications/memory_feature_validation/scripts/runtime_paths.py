"""Canonical runtime paths for memory validation entry points."""

from __future__ import annotations

from pathlib import Path

from agentloom.configuration.system_loader import load_project_system_config
from agentloom.runtime import resolve_runtime_home


def canonical_runtime_root(repo_root: Path) -> Path:
    """Resolve the validation root through AgentLoom's single path authority."""

    system = load_project_system_config(repo_root)
    return resolve_runtime_home(system, agent_root=repo_root).root_dir
