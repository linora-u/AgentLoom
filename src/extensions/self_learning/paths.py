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


def config_int(name: str, default: int = 0) -> int:
    try:
        return int(_config_section().get(name, default) or 0)
    except (TypeError, ValueError):
        return default


_MEMORY_CONFIG_DEFAULTS: dict[str, Any] = {
    "prompt_max_chars": 12000,
    "max_item_chars": 4000,
    "scope_budgets": {"project": 8000, "application": 6000, "session": 4000},
    "session_ttl_days": 14,
    "distill_enabled": True,
    # Code defaults stay conservative (no LLM calls, no auto-apply) so tests
    # and minimal configs are inert; config/system.yaml ships the active values.
    "distill_model": "",
    "auto_apply": "off",
}


def memory_config() -> dict[str, Any]:
    """Return the ``self_learning.memory`` section merged over defaults."""
    section = _config_section().get("memory", {})
    if not isinstance(section, dict):
        section = {}
    merged = dict(_MEMORY_CONFIG_DEFAULTS)
    merged.update({k: v for k, v in section.items() if v is not None})
    budgets = dict(_MEMORY_CONFIG_DEFAULTS["scope_budgets"])
    if isinstance(section.get("scope_budgets"), dict):
        budgets.update({k: int(v) for k, v in section["scope_budgets"].items() if v is not None})
    merged["scope_budgets"] = budgets
    return merged


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
