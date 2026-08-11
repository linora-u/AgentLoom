"""Versioned, read-only AgentLoom Application Studio domain projection."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from src.application_revision import application_revision
from src.lib.config.layered_builder import LayeredConfigBuilder
from src.tui_bridge.catalog import _skill_manifests, _skill_metadata
from src.tui_bridge.definition import load_agent_definition

_MAX_REVISION_FILES = 4096
_MAX_REVISION_BYTES = 64 * 1024 * 1024


def application_detail(
    project_root: Path,
    application_id: str,
    *,
    systems: list[dict[str, Any]],
) -> dict[str, Any]:
    root = project_root.resolve()
    application_root = root / "applications" / Path(*application_id.split("/"))
    if (
        application_root.is_symlink()
        or not application_root.is_dir()
        or root / "applications" not in application_root.resolve().parents
    ):
        raise FileNotFoundError(application_id)

    global_path = root / "config" / "system.yaml"
    application_path = application_root / "config" / "system.yaml"
    global_config = _safe_config(global_path)
    application_config = _safe_config(application_path)
    supervisor_systems = [row for row in systems if row.get("application_id") == application_id]
    agents: list[dict[str, Any]] = []
    for system in supervisor_systems:
        source_path = root / str(system["path"])
        definition = load_agent_definition(source_path)
        agents.append(
            _agent_detail(
                root,
                application_root,
                source_path,
                definition,
                role="supervisor",
                global_config=global_config,
                application_config=application_config,
                validation=deepcopy(system.get("validation") or {"valid": False, "errors": []}),
                ancestry=frozenset(),
            )
        )

    updated_at = _application_updated_at(application_root)
    valid = bool(supervisor_systems) and all(
        bool((row.get("validation") or {}).get("valid")) for row in supervisor_systems
    )
    return {
        "schema_version": 1,
        "application": {
            "id": application_id,
            "name": application_id.rsplit("/", 1)[-1],
            "path": application_root.relative_to(root).as_posix(),
            "health": "healthy" if valid else "invalid",
            "updated_at": updated_at,
        },
        "working_revision": _working_revision(application_root),
        "running_revision": _running_revision(root, application_id, supervisor_systems),
        "agents": agents,
    }


def _agent_detail(
    root: Path,
    application_root: Path,
    source_path: Path,
    definition: dict[str, Any],
    *,
    role: str,
    global_config: dict[str, Any],
    application_config: dict[str, Any],
    validation: dict[str, Any],
    ancestry: frozenset[Path],
) -> dict[str, Any]:
    agent_overlay = _agent_overlay(definition)
    builder = LayeredConfigBuilder()
    layers = (
        ("global", global_config, root / "config" / "system.yaml"),
        ("application", application_config, application_root / "config" / "system.yaml"),
        ("agent", agent_overlay, source_path),
    )
    sources: dict[str, tuple[str, str]] = {}
    for source, values, path in layers:
        if not values:
            continue
        builder.apply_mapping(source, values)
        for key in values:
            sources[key] = (source, path.relative_to(root).as_posix())
    effective = builder.build()

    workers: list[dict[str, Any]] = []
    next_ancestry = ancestry | {source_path.resolve()}
    for raw_worker in definition.get("worker_agents") or []:
        worker_path = _worker_path(root, application_root, source_path, raw_worker)
        if worker_path is None or worker_path.resolve() in next_ancestry:
            continue
        try:
            worker_definition = load_agent_definition(worker_path)
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            continue
        workers.append(
            _agent_detail(
                root,
                application_root,
                worker_path,
                worker_definition,
                role="worker",
                global_config=global_config,
                application_config=application_config,
                validation={"valid": True, "errors": []},
                ancestry=next_ancestry,
            )
        )

    model_type = str(definition.get("model_type") or _default_model_type(root) or "")
    model_source = "agent" if definition.get("model_type") else "global"
    return {
        "id": source_path.relative_to(root).as_posix(),
        "name": str(definition.get("name") or source_path.stem),
        "description": str(definition.get("description") or ""),
        "role": role,
        "workflow": _workflow_summary(definition.get("workflow")),
        "model": {"type": model_type, "source": model_source},
        "tools": _tool_summaries(definition.get("tools")),
        "skills": _effective_skills(
            root,
            application_root,
            global_config.get("skills"),
            application_config.get("skills"),
            agent_overlay.get("skills"),
        ),
        "permissions": _sourced(effective.get("tool_access_control"), sources.get("tool_access_control")),
        "hooks": _sourced(effective.get("hooks"), sources.get("hooks")),
        "mcp": _sourced(effective.get("mcp_servers"), sources.get("mcp_servers")),
        "source_path": source_path.relative_to(root).as_posix(),
        "validation": validation,
        "workers": workers,
    }


def _safe_config(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            return {}
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, TypeError, ValueError, yaml.YAMLError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: deepcopy(value)
        for key, value in raw.items()
        if key not in {"model", "llm", "langfuse", "runtime", "logging"}
    }


def _agent_overlay(definition: Mapping[str, Any]) -> dict[str, Any]:
    # Keep this list aligned with the Python runtime's documented Agent
    # overlay surface; identity/prompt workflow fields are presented separately.
    allowed = {
        "system", "model_request_headers", "smart_summary", "context_engine",
        "tool_access_control", "execution_env", "code_agent", "tools",
        "shell_settings", "default_toolsets", "toolsets",
        "prompt", "mcp_servers", "self_learning", "hooks", "skills",
    }
    return {key: deepcopy(value) for key, value in definition.items() if key in allowed}


def _effective_skills(
    root: Path,
    application_root: Path,
    global_configured: Any,
    application_configured: Any,
    agent_configured: Any,
) -> list[dict[str, Any]]:
    resolved: dict[Path, tuple[Path, str]] = {}
    layers = (
        (root, global_configured, "global"),
        (application_root, application_configured, "application"),
        (application_root, agent_configured, "agent"),
    )
    _add_skill_source(resolved, root, root / "skills", "global")
    _add_skill_source(resolved, root, application_root / "skills", "application")
    for layer_root, configured, source in layers:
        paths = configured.get("paths") if isinstance(configured, Mapping) else None
        for raw_path in paths if isinstance(paths, list) else []:
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            relative = Path(raw_path.strip())
            if relative.is_absolute() or ".." in relative.parts:
                continue
            _add_skill_source(resolved, root, layer_root / relative, source)

    skills_by_name: dict[str, dict[str, Any]] = {}
    for manifest, (display_path, skill_source) in sorted(
        resolved.items(), key=lambda item: (item[1][1], item[1][0].as_posix())
    ):
        metadata = _skill_metadata(root, manifest)
        name = str(metadata.get("name") or manifest.parent.name)
        relative_path = display_path.relative_to(root).as_posix()
        entry = {
            "name": name,
            "description": str(metadata.get("description") or ""),
            "source": skill_source,
            "path": relative_path,
        }
        existing = skills_by_name.get(name)
        priority = {"global": 0, "application": 1, "agent": 2}
        if existing is None or priority[skill_source] > priority[str(existing["source"])]:
            skills_by_name[name] = entry
        elif priority[skill_source] == priority[str(existing["source"])] and relative_path < str(existing["path"]):
            skills_by_name[name] = entry
    skills = list(skills_by_name.values())
    return sorted(skills, key=lambda item: (item["source"] != "global", item["name"], item["path"]))


def _add_skill_source(
    resolved: dict[Path, tuple[Path, str]],
    root: Path,
    source_path: Path,
    source: str,
) -> None:
    try:
        candidate = source_path.resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError):
        return
    if source_path.is_symlink():
        return
    if candidate.is_file():
        manifests = [candidate] if candidate.name.lower() == "skill.md" else []
    elif candidate.is_dir():
        manifests = _skill_manifests(root, candidate)
    else:
        manifests = []
    for manifest in manifests:
        resolved[manifest.resolve()] = (manifest, source)


def _tool_summaries(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    tools: list[dict[str, str]] = []
    for item in raw:
        name = item.get("name") if isinstance(item, Mapping) else item
        if isinstance(name, str) and name.strip():
            tools.append({"name": name.strip(), "source": "agent"})
    return tools


def _sourced(value: Any, source: tuple[str, str] | None) -> dict[str, Any]:
    return {
        "value": deepcopy(value),
        "source": source[0] if source else "none",
        "source_path": source[1] if source else None,
    }


def _worker_path(
    root: Path,
    application_root: Path,
    supervisor_path: Path,
    raw_worker: Any,
) -> Path | None:
    raw_path = raw_worker.get("path") if isinstance(raw_worker, Mapping) else None
    if not isinstance(raw_path, str) or not raw_path.strip() or "\\" in raw_path:
        return None
    configured = Path(raw_path.strip())
    if configured.is_absolute():
        return None
    candidate = root / configured if "/" in raw_path else supervisor_path.parent / "worker_agents" / configured
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(application_root.resolve())
    except (OSError, ValueError):
        return None
    if candidate.is_symlink() or not resolved.is_file() or resolved.suffix.lower() not in {".yaml", ".yml", ".md"}:
        return None
    return resolved


def _default_model_type(root: Path) -> str:
    try:
        raw = yaml.safe_load((root / "config" / "llm.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, TypeError, ValueError, yaml.YAMLError):
        return ""
    model = raw.get("model") if isinstance(raw, Mapping) else None
    return str(model.get("default_model_type") or "") if isinstance(model, Mapping) else ""


def _workflow_summary(value: Any) -> str:
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    text = " ".join(str(value or "").split())
    return text[:500]


def _application_files(application_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(application_root.rglob("*"), key=lambda item: item.as_posix()):
        if len(files) >= _MAX_REVISION_FILES:
            raise ValueError("Application contains too many files for a bounded revision")
        if path.is_symlink() or not path.is_file():
            continue
        files.append(path)
    return files


def _working_revision(application_root: Path) -> str:
    return application_revision(application_root)


def _running_revision(
    root: Path,
    application_id: str,
    systems: list[dict[str, Any]],
) -> str | None:
    active_run_ids = {
        str(latest["run_id"])
        for system in systems
        for latest in [system.get("latest_run")]
        if isinstance(latest, Mapping)
        and latest.get("status") == "running"
        and isinstance(latest.get("run_id"), str)
    }
    if not active_run_ids:
        return None
    runtime_root = _runtime_root(root)
    runs_root = runtime_root / "runs" / Path(*application_id.split("/"))
    for run_id in sorted(active_run_ids):
        manifest_path = runs_root / run_id / "manifest.json"
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        revision = raw.get("application_revision") if isinstance(raw, Mapping) else None
        if (
            raw.get("application_id") == application_id
            and raw.get("run_id") == run_id
            and raw.get("status") == "running"
            and isinstance(revision, str)
            and revision.startswith("sha256:")
            and len(revision) == 71
        ):
            return revision
    return None


def _runtime_root(root: Path) -> Path:
    system = _safe_runtime_config(root / "config" / "system.yaml")
    runtime = system.get("runtime") if isinstance(system, Mapping) else None
    configured = os.environ.get("AGENTLOOM_RUNTIME_ROOT", "").strip()
    if not configured and isinstance(runtime, Mapping):
        configured = str(runtime.get("root_dir") or "")
    path = Path(configured or ".agentloom").expanduser()
    return path.absolute() if path.is_absolute() else (root / path).absolute()


def _safe_runtime_config(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, TypeError, ValueError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _application_updated_at(application_root: Path) -> str | None:
    files = _application_files(application_root)
    if not files:
        return None
    timestamp = max(path.stat().st_mtime for path in files)
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()
