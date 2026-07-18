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
}


_REVIEW_SCOPE_DEFAULTS: dict[str, dict[str, Any]] = {
    "application": {
        "review_model": "",
        "trigger": {"mode": "batch", "min_completed_runs": 5},
        "approval": {"fact": "auto", "experience": "manual"},
    },
    "project": {
        "review_model": "",
        "trigger": {"mode": "batch", "min_candidates": 5},
        "approval": {"fact": "manual", "experience": "manual"},
    },
}
_REVIEW_ARTIFACT_DEFAULTS: dict[str, bool] = {
    "markdown": True,
    "review_auto_applied": True,
}


def review_config(
    agent_config: dict[str, Any] | None = None,
    *,
    scope: str = "application",
) -> dict[str, Any]:
    """Return one scope's effective v6 review policy.

    Runtime callers pass the already-layered Application configuration. The
    configuration builder protects the project policy before it reaches this
    accessor; legacy ``self_learning.memory`` review keys are never read.
    """

    if scope not in _REVIEW_SCOPE_DEFAULTS:
        raise ValueError("review scope must be 'application' or 'project'")

    review = self_learning_config(agent_config).get("review", {})
    if not isinstance(review, dict):
        review = {}
    raw_scope = review.get(scope, {})
    if not isinstance(raw_scope, dict):
        raw_scope = {}

    defaults = _REVIEW_SCOPE_DEFAULTS[scope]
    trigger = dict(defaults["trigger"])
    if isinstance(raw_scope.get("trigger"), dict):
        trigger.update(raw_scope["trigger"])
    approval = dict(defaults["approval"])
    if isinstance(raw_scope.get("approval"), dict):
        approval.update(raw_scope["approval"])
    artifacts = dict(_REVIEW_ARTIFACT_DEFAULTS)
    if isinstance(review.get("artifacts"), dict):
        artifacts.update(review["artifacts"])

    return {
        "enabled": _strict_bool(review.get("enabled", False), default=False),
        "review_model": str(raw_scope.get("review_model") or "").strip(),
        "trigger": trigger,
        "approval": approval,
        "artifacts": artifacts,
    }


def review_model(
    agent_config: dict[str, Any] | None = None,
    *,
    scope: str = "application",
) -> str:
    policy = review_config(agent_config, scope=scope)
    return policy["review_model"] if policy["enabled"] else ""


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
