"""Application identity helpers for self-learning state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .redaction import safe_storage_identity


@dataclass(frozen=True)
class ApplicationScope:
    """Stable identity for an AgentLoom application."""

    application_id: str = ""
    application_name: str = ""
    application_path: str = ""
    workflow_path: str = ""


def safe_application_id(value: str) -> str:
    raw = safe_storage_identity(
        value,
        namespace="application",
        allow_empty=True,
    ).replace("\\", "/")
    parts = []
    for part in raw.split("/"):
        if part in {"", ".", ".."}:
            continue
        parts.append(
            "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in part)
        )
    cleaned = "/".join(parts)
    return cleaned[:240]


def _agent_root() -> Path:
    try:
        from src.lib.config import C

        return Path(C.agent_root).resolve()
    except Exception:
        current = Path.cwd().resolve()
        for parent in (current, *current.parents):
            if (parent / "applications").is_dir() and (parent / "config" / "system.yaml").exists():
                return parent
        return current


def _workflow_path_from_config(agent_config: dict[str, Any] | None, explicit: str | Path | None = None) -> Path | None:
    raw = explicit
    if raw is None and isinstance(agent_config, dict):
        raw = (
            agent_config.get("_yaml_file_path")
            or agent_config.get("workflow_path")
            or agent_config.get("yaml_path")
            or agent_config.get("_yaml_path")
        )
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = _agent_root() / path
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _app_root_from_workflow(workflow_path: Path, agent_root: Path) -> Path | None:
    current = workflow_path.parent
    while current != current.parent:
        if current.name == "workflows":
            app_root = current.parent
            try:
                app_root.relative_to(agent_root / "applications")
            except ValueError:
                return None
            return app_root
        if current == agent_root:
            break
        current = current.parent
    return None


def resolve_application_scope(
    agent_config: dict[str, Any] | None = None,
    *,
    workflow_path: str | Path | None = None,
) -> ApplicationScope:
    """Resolve the current AgentLoom application from config or YAML path.

    Agent names are not application identities.  The authoritative boundary is
    the nearest ``workflows/`` ancestor under ``<agent_root>/applications``.
    """

    root = _agent_root()
    yaml_path = _workflow_path_from_config(agent_config, workflow_path)
    if yaml_path is not None:
        app_root = _app_root_from_workflow(yaml_path, root)
        if app_root is not None:
            try:
                rel = app_root.relative_to(root / "applications").as_posix()
            except ValueError:
                rel = app_root.name
            return ApplicationScope(
                application_id=safe_application_id(rel),
                application_name=app_root.name,
                application_path=str(app_root),
                workflow_path=str(yaml_path),
            )

    if isinstance(agent_config, dict):
        app_value = (
            agent_config.get("application_id")
            or agent_config.get("app")
            or agent_config.get("application")
            or ""
        )
        if app_value:
            app_id = safe_application_id(str(app_value))
            return ApplicationScope(
                application_id=app_id,
                application_name=Path(app_id).name or app_id,
                workflow_path=str(yaml_path or ""),
            )

    return ApplicationScope(workflow_path=str(yaml_path or ""))


def current_application_scope() -> ApplicationScope:
    try:
        from src.trace import get_current_agent_config

        return resolve_application_scope(get_current_agent_config())
    except Exception:
        return ApplicationScope()
