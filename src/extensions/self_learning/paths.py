"""Path helpers for the self-learning extension."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def project_root() -> Path:
    """Return the AgentLoom project root without importing heavy runtime code."""
    try:
        from src.lib.config import C

        return Path(C.agent_root).resolve()
    except Exception:
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / "config" / "system.yaml").exists():
                return parent
        return Path.cwd().resolve()


def _config_section() -> dict[str, Any]:
    try:
        from src.lib.config import C

        section = C.get("self_learning", {})
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def self_learning_root(root: str | Path | None = None) -> Path:
    """Return the durable state root, defaulting to ``.agentloom``."""
    if root is not None:
        return Path(root).expanduser().resolve()

    env_root = os.environ.get("AGENTLOOM_SELF_LEARNING_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()

    configured = _config_section().get("root_dir", ".agentloom")
    path = Path(str(configured)).expanduser()
    if not path.is_absolute():
        path = project_root() / path
    return path.resolve()


def config_bool(name: str, default: bool = True) -> bool:
    value = _config_section().get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def sessions_dir(root: str | Path | None = None) -> Path:
    return self_learning_root(root) / "sessions"


def session_events_dir(root: str | Path | None = None) -> Path:
    return sessions_dir(root) / "events"


def session_index_db(root: str | Path | None = None) -> Path:
    return self_learning_db(root)


def self_learning_db(root: str | Path | None = None) -> Path:
    return self_learning_root(root) / "self_learning.db"


def memory_dir(root: str | Path | None = None) -> Path:
    return self_learning_root(root) / "memory"


def memory_db(root: str | Path | None = None) -> Path:
    return self_learning_db(root)


def learning_runs_dir(root: str | Path | None = None) -> Path:
    return self_learning_root(root) / "learning" / "runs"


def application_learning_runs_dir(application_id: str, root: str | Path | None = None) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(application_id or "default"))
    return self_learning_root(root) / "learning" / "applications" / safe / "runs"


def skill_proposals_dir(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root).expanduser().resolve()
    return (project_root() / "skills" / "generated" / "proposals").resolve()


def active_skills_dir() -> Path:
    return (project_root() / "skills").resolve()
