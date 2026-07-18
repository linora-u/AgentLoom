"""Application identity helpers for self-learning state."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from src.lib.runtime.context import get_current_run_context, safe_application_id


@dataclass(frozen=True)
class ApplicationScope:
    """Stable identity for an AgentLoom application."""

    application_id: str = ""
    application_name: str = ""
    application_path: str = ""
    workflow_path: str = ""


@dataclass(frozen=True)
class LegacyApplicationResolution:
    """Conservative result for one pre-canonical Application identity.

    ``canonical_id`` is populated only when one lossless identity or one
    authoritative Application path identifies the target.  Unresolved data is
    assigned a deterministic, non-runtime quarantine namespace so migration can
    retain an audit without guessing which live Application owns it.
    """

    canonical_id: str = ""
    quarantine_id: str = ""
    reason: str = ""
    candidates: tuple[str, ...] = ()


def _legacy_application_id_from_path(value: str, *, workflow: bool) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    parts = PurePosixPath(raw).parts
    application_indexes = [index for index, part in enumerate(parts) if part == "applications"]
    if not application_indexes:
        return ""
    tail = list(parts[application_indexes[-1] + 1 :])
    if "workflows" in tail:
        tail = tail[: tail.index("workflows")]
    elif workflow:
        # A workflow/yaml path without the canonical ``workflows`` boundary is
        # not enough evidence to decide where the Application directory ends.
        return ""
    if not tail:
        return ""
    try:
        return safe_application_id("/".join(tail))
    except ValueError:
        return ""


def resolve_legacy_application_id(
    value: str,
    *,
    application_paths: tuple[str, ...] = (),
    workflow_paths: tuple[str, ...] = (),
) -> LegacyApplicationResolution:
    """Resolve legacy Application state without name-based guessing.

    Runtime-owned paths are stronger than the old identifier because older
    schemas sometimes stored an Agent name in ``application_id``. Multiple
    distinct path targets are ambiguous and therefore quarantined. With no path
    evidence, only an identifier already in canonical form is accepted.
    """

    raw = str(value or "").strip().replace("\\", "/")
    path_candidates = {
        candidate
        for candidate in (
            *(_legacy_application_id_from_path(path, workflow=False) for path in application_paths),
            *(_legacy_application_id_from_path(path, workflow=True) for path in workflow_paths),
        )
        if candidate
    }
    if len(path_candidates) == 1:
        canonical = next(iter(path_candidates))
        return LegacyApplicationResolution(
            canonical_id=canonical,
            reason="workflow_path",
            candidates=(canonical,),
        )
    if len(path_candidates) > 1:
        reason = "conflicting_application_paths"
    else:
        try:
            canonical = safe_application_id(raw)
        except ValueError:
            canonical = ""
        if canonical and canonical == raw:
            return LegacyApplicationResolution(
                canonical_id=canonical,
                reason="canonical_identity",
                candidates=(canonical,),
            )
        reason = "noncanonical_application_identity"

    digest_input = "\n".join(
        (
            raw,
            *sorted(str(path or "") for path in application_paths),
            *sorted(str(path or "") for path in workflow_paths),
        )
    )
    digest = hashlib.sha256(digest_input.encode("utf-8", errors="surrogatepass")).hexdigest()[:24]
    return LegacyApplicationResolution(
        quarantine_id=f"migration-unresolved/{digest}",
        reason=reason,
        candidates=tuple(sorted(path_candidates)),
    )


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

    runtime_context = get_current_run_context()
    bound_application_id = runtime_context.application_id if runtime_context is not None else ""
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
                application_id=bound_application_id or safe_application_id(rel),
                application_name=app_root.name,
                application_path=str(app_root),
                workflow_path=str(yaml_path),
            )

    if isinstance(agent_config, dict):
        app_value = (
            agent_config.get("application_id") or agent_config.get("app") or agent_config.get("application") or ""
        )
        if app_value:
            app_id = bound_application_id or safe_application_id(str(app_value))
            return ApplicationScope(
                application_id=app_id,
                application_name=Path(app_id).name or app_id,
                workflow_path=str(yaml_path or ""),
            )

    if bound_application_id:
        return ApplicationScope(
            application_id=bound_application_id,
            application_name=Path(bound_application_id).name,
            workflow_path=str(yaml_path or ""),
        )
    return ApplicationScope(workflow_path=str(yaml_path or ""))


def current_application_scope() -> ApplicationScope:
    try:
        from src.trace import get_current_agent_config

        return resolve_application_scope(get_current_agent_config())
    except Exception:
        return ApplicationScope()
