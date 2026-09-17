"""Versioned, read-only presentation of the shared Application definition."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from src.application.definition import (
    definition_error,
    load_agent_definition,
    model_catalog,
    model_types,
    resolve_worker_path,
    selected_model_type,
    skill_sources,
    validate_agent_definition,
)
from src.application.presentation import configuration_projection, display_path, public_value
from src.application.revision import application_revision
from src.configuration.config import build_effective_agent_config_snapshot, load_project_config
from src.configuration.yaml_loader import load_unique_yaml
from src.runtime.skills.catalog import SkillCatalog

_MAX_REVISION_FILES = 4096
_MAX_REVISION_BYTES = 64 * 1024 * 1024


def application_detail(project_root: Path, application_id: str, *, systems: list[dict[str, Any]]) -> dict[str, Any]:
    root = project_root.resolve()
    application_root = root / "applications" / Path(*application_id.split("/"))
    if (
        application_root.is_symlink()
        or not application_root.is_dir()
        or root / "applications" not in application_root.resolve().parents
    ):
        raise FileNotFoundError(application_id)
    supervisor_systems = [row for row in systems if row.get("application_id") == application_id]
    agents = []
    base = None
    config_errors = []
    try:
        base = load_project_config(root)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        config_errors.append(f"{root}/config: {definition_error(exc)}")
    catalog = model_catalog(base) if base is not None else model_types(root)
    for system in supervisor_systems:
        path = root / str(system["path"])
        try:
            definition = load_agent_definition(path)
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            definition = {"name": path.stem}
            errors = [f"{path}: {definition_error(exc)}"]
        else:
            errors = validate_agent_definition(root, str(path), definition, base_config=base, catalog=catalog)
        agents.append(
            _agent_detail(
                root,
                path,
                definition,
                role="supervisor",
                base=base,
                catalog=catalog,
                errors=list(dict.fromkeys(config_errors + errors)),
                ancestry=frozenset(),
            )
        )
    valid = bool(agents) and all(agent["validation"]["valid"] for agent in agents)
    return {
        "schema_version": 1,
        "application": {
            "id": application_id,
            "name": application_id.rsplit("/", 1)[-1],
            "path": application_root.relative_to(root).as_posix(),
            "health": "healthy" if valid else "invalid",
            "updated_at": _application_updated_at(application_root),
        },
        "working_revision": _working_revision(application_root),
        "running_revision": _running_revision(root, application_id, supervisor_systems),
        "agents": agents,
    }


def _agent_detail(root, path, definition, *, role, base, catalog, errors, ancestry):
    projection = {"values": {}, "sources": {}}
    snapshot = None
    if base is not None:
        try:
            snapshot = build_effective_agent_config_snapshot(definition, source_name=str(path), base_config=base)
            projection = configuration_projection(snapshot, root)
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{path}: {definition_error(exc)}")
    values, sources = projection["values"], projection["sources"]
    workers = []
    ancestry = ancestry | {path.resolve()}
    raw_workers = definition.get("worker_agents") or []
    for raw in raw_workers if isinstance(raw_workers, list) else []:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("path"), str):
            continue
        try:
            child_path = resolve_worker_path(root, path, raw["path"])
            if child_path in ancestry:
                continue
            child = load_agent_definition(child_path)
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            continue
        child_errors = validate_agent_definition(root, str(child_path), child, base_config=base, catalog=catalog)
        workers.append(
            _agent_detail(
                root,
                child_path,
                child,
                role="worker",
                base=base,
                catalog=catalog,
                errors=child_errors,
                ancestry=ancestry,
            )
        )
    try:
        model = selected_model_type(definition, catalog)
    except (TypeError, ValueError):
        model = str(definition.get("model_type") or catalog[0])
    skills = []
    if snapshot is not None:
        try:
            skills = [
                {
                    "name": item.name,
                    "description": item.description,
                    "source": "global" if item.scope == "project" else item.scope,
                    "path": display_path(item.location, root),
                }
                for item in SkillCatalog.discover(skill_sources(snapshot)).summaries()
            ]
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"{path}: {definition_error(exc)}")
    return {
        "id": display_path(path, root),
        "name": str(definition.get("name") or path.stem),
        "description": str(definition.get("description") or ""),
        "role": role,
        "workflow": _workflow_summary(definition.get("workflow")),
        "model": {"type": model, "source": "agent" if str(definition.get("model_type") or "").strip() else "global"},
        "tools": [
            {"name": str(tool["name"]), "source": sources.get("tools", {}).get("source", "agent")}
            for tool in values.get("tools", definition.get("tools")) or []
            if isinstance(tool, Mapping) and tool.get("name")
        ],
        "skills": sorted(skills, key=lambda item: (item["source"] != "global", item["name"], item["path"])),
        "permissions": _sourced(values.get("tool_access_control"), sources.get("tool_access_control")),
        "hooks": _sourced(values.get("hooks"), sources.get("hooks")),
        "mcp": _sourced(values.get("mcp_servers"), sources.get("mcp_servers")),
        "effective_config": projection,
        "source_path": display_path(path, root),
        "validation": {"valid": not errors, "errors": list(dict.fromkeys(errors))},
        "workers": workers,
    }


def _sourced(value, source):
    return {
        "value": public_value(value),
        "source": source["source"] if source else "none",
        "source_path": source["source_path"] if source else None,
    }


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
        if isinstance(latest, Mapping) and latest.get("status") == "running" and isinstance(latest.get("run_id"), str)
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
        raw = load_unique_yaml(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, TypeError, ValueError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _application_updated_at(application_root: Path) -> str | None:
    files = _application_files(application_root)
    if not files:
        return None
    timestamp = max(path.stat().st_mtime for path in files)
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()
