"""Path helpers for the self-learning extension."""

from __future__ import annotations

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


def _runtime_config_section() -> dict[str, Any]:
    try:
        from src.lib.config import C

        section = C.get("runtime", {})
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def self_learning_root(root: str | Path | None = None) -> Path:
    """Return the durable state root, defaulting to ``.agentloom``."""
    if root is not None:
        return Path(root).expanduser().resolve()

    # A running Application has one canonical runtime home.  Do not let a
    # legacy standalone/test override split sessions or memory from that run's
    # logs and checkpoints.
    try:
        from src.lib.runtime import get_current_run_context

        runtime_context = get_current_run_context()
        if runtime_context is not None:
            return runtime_context.root_dir
    except Exception:
        pass

    from src.lib.runtime import resolve_runtime_home

    return resolve_runtime_home(
        {"runtime": _runtime_config_section()},
        agent_root=project_root(),
    ).root_dir


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


def _strict_bool(value: Any, *, default: bool = False) -> bool:
    """Parse a configuration boolean without treating ``"false"`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
        return default
    if value is None:
        return default
    return bool(value)


def self_learning_config(agent_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the effective top-level self-learning section.

    A running Application must use its already-layered agent configuration.
    Falling back to process-global ``C`` is reserved for CLI/non-runtime use.
    """
    if isinstance(agent_config, dict):
        section = agent_config.get("self_learning", {})
        return section if isinstance(section, dict) else {}
    return _config_section()


def self_learning_enabled(agent_config: dict[str, Any] | None = None) -> bool:
    return _strict_bool(self_learning_config(agent_config).get("enabled", True), default=True)


_MEMORY_CONFIG_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "prompt_max_chars": 12000,
    "max_item_chars": 4000,
    "scope_budgets": {"project": 8000, "application": 6000},
    # Completed-run review is opt-in. An absent/empty model means the completed
    # run performs no extra LLM call; foreground memory writes remain usable.
    "review_model": "",
    "write_approval": False,
}


def memory_review_model(agent_config: dict[str, Any] | None = None) -> str:
    """Read only the opt-in review switch without parsing unrelated fields."""
    section = self_learning_config(agent_config).get("memory", {})
    if not isinstance(section, dict):
        return ""
    return str(section.get("review_model") or "").strip()


def memory_config(agent_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return effective ``self_learning.memory`` settings.

    ``agent_config`` is the root owner's fully layered Application config.
    Runtime paths must pass it explicitly instead of falling back to the
    process-global config, otherwise concurrent Applications can inherit one
    another's review/approval policy.
    """
    source = self_learning_config(agent_config)
    section = source.get("memory", {})
    if not isinstance(section, dict):
        section = {}
    merged = dict(_MEMORY_CONFIG_DEFAULTS)
    merged.update({k: v for k, v in section.items() if v is not None})
    budgets = dict(_MEMORY_CONFIG_DEFAULTS["scope_budgets"])
    if isinstance(section.get("scope_budgets"), dict):
        budgets.update({k: int(v) for k, v in section["scope_budgets"].items() if v is not None})
    merged["scope_budgets"] = budgets
    merged["enabled"] = _strict_bool(merged.get("enabled", True), default=True)
    merged["write_approval"] = _strict_bool(merged.get("write_approval", False), default=False)
    merged["review_model"] = memory_review_model(agent_config)
    return merged


def sessions_dir(root: str | Path | None = None) -> Path:
    return self_learning_root(root) / "sessions"


def session_events_dir(root: str | Path | None = None) -> Path:
    return sessions_dir(root) / "events"


def session_index_db(root: str | Path | None = None) -> Path:
    return self_learning_db(root)


def self_learning_db(root: str | Path | None = None) -> Path:
    return self_learning_root(root) / "self_learning.db"


def memory_db(root: str | Path | None = None) -> Path:
    return self_learning_db(root)


def skill_proposals_dir(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root).expanduser().resolve()
    return (project_root() / "skills" / "generated" / "proposals").resolve()


def active_skills_dir() -> Path:
    return (project_root() / "skills").resolve()
