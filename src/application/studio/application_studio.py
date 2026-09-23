"""Versioned, read-only presentation of the shared Application definition."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from agentloom.application.definition import (
    definition_error,
    inspect_application_definition,
    load_agent_definition,
    model_catalog,
    model_types,
    resolve_worker_path,
    selected_model_type,
)
from agentloom.application.presentation import configuration_projection, display_path, public_value
from agentloom.application.revision import application_revision
from agentloom.configuration.config import load_project_config
from agentloom.configuration.yaml_loader import load_unique_yaml
from agentloom.execution.context import resolve_runtime_home

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
        inspection = None
        try:
            definition = load_agent_definition(path)
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            definition = {"name": path.stem}
            errors = [f"{path}: {definition_error(exc)}"]
        else:
            inspection = inspect_application_definition(root, str(path), definition, base_config=base, catalog=catalog)
            errors = list(inspection.errors)
        agents.append(
            _agent_detail(
                root,
                path,
                definition,
                role="supervisor",
                inspection=inspection,
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


def _agent_detail(root, path, definition, *, role, inspection, catalog, errors, ancestry):
    projection = {"values": {}, "sources": {}}
    snapshot = inspection.snapshots.get(path.resolve()) if inspection is not None else None
    if snapshot is not None:
        try:
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
            child = inspection.definitions.get(child_path) if inspection is not None else None
            if child is None:
                continue
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            continue
        child_errors = list(inspection.errors_by_path.get(child_path, ()))
        workers.append(
            _agent_detail(
                root,
                child_path,
                child,
                role="worker",
                inspection=inspection,
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
    skill_catalog = snapshot.values.get("_skill_catalog_snapshot") if snapshot is not None else None
    if skill_catalog is not None:
        skills = [
            {
                "name": item.name,
                "description": item.description,
                "source": "global" if item.scope == "project" else item.scope,
                "path": display_path(item.location, root),
            }
            for item in skill_catalog.summaries()
        ]
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
    text = " ".join(value.split()) if isinstance(value, str) else ""
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
    return resolve_runtime_home(system, agent_root=root).root_dir


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
