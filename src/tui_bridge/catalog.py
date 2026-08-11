"""Lightweight, read-only workspace projections for the TUI.

This module deliberately depends only on files and ``yaml.safe_load``.  Merely
opening the workspace catalog must never construct a model, import the Agent
runtime, or create runtime storage.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from src.tui_bridge.definition import AgentDefinitionCache

AGENT_YAML_MAX_BYTES = 1024 * 1024
SKILL_MANIFEST_MAX_BYTES = 128 * 1024
SCHEDULE_DOCUMENT_MAX_BYTES = 8 * 1024 * 1024
MAX_WORKER_DEPTH = 16


def project_catalog(
    project_root: str | Path,
    systems: Iterable[Mapping[str, Any]],
    runs: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    definition_cache: AgentDefinitionCache | None = None,
) -> dict[str, Any]:
    """Project Applications, Agent trees, Skills, and durable schedules.

    ``systems`` and ``runs`` are the bridge's already-computed summaries.  The
    catalog enriches them from local configuration without importing the
    execution stack.  Invalid, external, or symlinked paths are ignored.
    """

    root = Path(project_root).expanduser().resolve()
    system_rows = [dict(item) for item in systems if isinstance(item, Mapping)]
    run_rows = [dict(item) for item in runs if isinstance(item, Mapping)]

    agents: list[dict[str, Any]] = []
    for system in system_rows:
        tree = _agent_tree(root, system, definition_cache=definition_cache)
        if tree is not None:
            agents.append(tree)
    agents.sort(key=lambda item: (item["application_id"], item["name"], item["path"]))

    application_ids = _discover_application_ids(root)
    application_ids.update(agent["application_id"] for agent in agents)
    skills = _global_skills(root) + _application_skills(root, application_ids)

    skills_by_application: dict[str, list[dict[str, Any]]] = {}
    for skill in skills:
        skills_by_application.setdefault(skill["application_id"], []).append(skill)
    agents_by_application: dict[str, list[dict[str, Any]]] = {}
    for agent in agents:
        agents_by_application.setdefault(agent["application_id"], []).append(agent)
    runs_by_application: dict[str, list[dict[str, Any]]] = {}
    for run in run_rows:
        application_id = _application_id_value(run.get("application_id"))
        if application_id is not None:
            runs_by_application.setdefault(application_id, []).append(run)

    applications: list[dict[str, Any]] = []
    for application_id in sorted(application_ids):
        application_agents = agents_by_application.get(application_id, [])
        application_runs = runs_by_application.get(application_id, [])
        worker_paths: set[str] = set()
        for agent in application_agents:
            _collect_worker_paths(agent.get("workers"), worker_paths)
        applications.append(
            {
                "id": application_id,
                "name": application_id.rsplit("/", 1)[-1],
                "path": f"applications/{application_id}",
                "system_count": len(application_agents),
                "worker_count": len(worker_paths),
                "skill_count": len(skills_by_application.get(application_id, [])),
                "run_count": len(application_runs),
                "active_run_count": sum(
                    1 for run in application_runs if str(run.get("status") or "").lower() == "running"
                ),
            }
        )

    return {
        "applications": applications,
        "agents": agents,
        "skills": skills,
        "schedules": schedule_catalog(root, now=now),
    }


def _agent_tree(
    root: Path,
    summary: Mapping[str, Any],
    *,
    definition_cache: AgentDefinitionCache | None,
) -> dict[str, Any] | None:
    raw_path = summary.get("path") or summary.get("id")
    path = _safe_project_file(root, raw_path, suffixes={".yaml", ".yml"})
    if path is None:
        return None
    relative = path.relative_to(root)
    if not relative.parts or relative.parts[0] != "applications" or "workflows" not in relative.parts:
        return None
    if "worker_agents" in relative.parts:
        return None
    application_id = _application_id_for_agent_path(relative)
    if application_id is None:
        return None
    definition = _read_agent_definition_object(root, path, cache=definition_cache)
    return _agent_node(
        root,
        path,
        application_id=application_id,
        role="supervisor",
        definition=definition,
        fallback=summary,
        ancestry=frozenset(),
        depth=0,
        definition_cache=definition_cache,
    )


def _agent_node(
    root: Path,
    path: Path,
    *,
    application_id: str,
    role: str,
    definition: Mapping[str, Any],
    fallback: Mapping[str, Any] | None,
    ancestry: frozenset[Path],
    depth: int,
    definition_cache: AgentDefinitionCache | None,
) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    name = _display_text(definition.get("name"))
    if not name and fallback is not None:
        name = _display_text(fallback.get("name"))
    description = _display_text(definition.get("description"))
    if not description and fallback is not None:
        description = _display_text(fallback.get("description"))

    workers: list[dict[str, Any]] = []
    if depth < MAX_WORKER_DEPTH:
        next_ancestry = ancestry | {path}
        raw_workers = definition.get("worker_agents")
        if isinstance(raw_workers, list):
            for raw_worker in raw_workers:
                worker_path = _worker_path(root, path, application_id, raw_worker)
                if worker_path is None or worker_path in next_ancestry:
                    continue
                worker_definition = _read_agent_definition_object(
                    root,
                    worker_path,
                    cache=definition_cache,
                )
                workers.append(
                    _agent_node(
                        root,
                        worker_path,
                        application_id=application_id,
                        role="worker",
                        definition=worker_definition,
                        fallback=None,
                        ancestry=next_ancestry,
                        depth=depth + 1,
                        definition_cache=definition_cache,
                    )
                )
    workers.sort(key=lambda item: (item["name"], item["path"]))
    return {
        "id": relative,
        "application_id": application_id,
        "name": name or path.stem,
        "description": description,
        "path": relative,
        "role": role,
        "skills": _configured_skills(definition.get("skills")),
        "workers": workers,
    }


def _worker_path(
    root: Path,
    supervisor_path: Path,
    application_id: str,
    raw_worker: Any,
) -> Path | None:
    if not isinstance(raw_worker, Mapping):
        return None
    raw_path = raw_worker.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip() or "\\" in raw_path:
        return None
    configured = Path(raw_path.strip())
    if configured.is_absolute():
        return None
    if "/" in raw_path:
        candidate = root / configured
    else:
        candidate = supervisor_path.parent / "worker_agents" / configured
    path = _safe_project_file(root, candidate, suffixes={".yaml", ".yml"})
    if path is None:
        return None
    application_root = root / "applications" / Path(*application_id.split("/"))
    try:
        path.relative_to(application_root)
    except ValueError:
        return None
    return path


def _configured_skills(raw: Any) -> dict[str, Any]:
    paths: Any = raw.get("paths") if isinstance(raw, Mapping) else []
    if not isinstance(paths, list):
        paths = []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in paths:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or len(value) > 1024 or "\x00" in value or "\n" in value or "\r" in value:
            continue
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            continue
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return {"paths": normalized}


def _collect_worker_paths(raw_workers: Any, paths: set[str]) -> None:
    if not isinstance(raw_workers, list):
        return
    for worker in raw_workers:
        if not isinstance(worker, Mapping):
            continue
        path = worker.get("path")
        if isinstance(path, str):
            paths.add(path)
        _collect_worker_paths(worker.get("workers"), paths)


def _discover_application_ids(root: Path) -> set[str]:
    applications_root = root / "applications"
    if not _safe_project_directory(root, applications_root):
        return set()
    discovered: set[str] = set()

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            child = Path(entry.path)
            if entry.name == "workflows":
                try:
                    application_id = child.parent.relative_to(applications_root).as_posix()
                except ValueError:
                    continue
                normalized = _application_id_value(application_id)
                if normalized is not None and _contains_supervisor_yaml(root, child):
                    discovered.add(normalized)
                continue
            if entry.name == "skills":
                # A directory named ``skills`` can also be test/data input.
                # Only a sibling Application workflow establishes ownership.
                continue
            visit(child)

    visit(applications_root)
    return discovered


def _contains_supervisor_yaml(root: Path, workflows_root: Path) -> bool:
    try:
        entries = sorted(os.scandir(workflows_root), key=lambda entry: entry.name)
    except OSError:
        return False
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_file(follow_symlinks=False):
                path = Path(entry.path)
                if path.suffix.lower() in {".yaml", ".yml"} and _safe_project_file(root, path) is not None:
                    return True
                continue
            if entry.is_dir(follow_symlinks=False) and entry.name != "worker_agents":
                if _contains_supervisor_yaml(root, Path(entry.path)):
                    return True
        except OSError:
            continue
    return False


def _application_skills(root: Path, application_ids: Iterable[str]) -> list[dict[str, Any]]:
    skills: list[dict[str, Any]] = []
    ids: set[str] = set()
    for application_id in sorted(set(application_ids)):
        skills_root = root / "applications" / Path(*application_id.split("/")) / "skills"
        if not _safe_project_directory(root, skills_root):
            continue
        manifests = _skill_manifests(root, skills_root)
        for manifest in manifests:
            metadata = _skill_metadata(root, manifest)
            name = _display_text(metadata.get("name")) or manifest.parent.name
            description = _display_text(metadata.get("description"))
            base_id = f"{application_id}:{name}"
            skill_id = base_id
            if skill_id in ids:
                relative_skill = manifest.parent.relative_to(skills_root).as_posix()
                skill_id = f"{base_id}:{relative_skill}"
            ids.add(skill_id)
            skills.append(
                {
                    "id": skill_id,
                    "application_id": application_id,
                    "name": name,
                    "description": description,
                    "origin": "application",
                    "path": manifest.relative_to(root).as_posix(),
                }
            )
    skills.sort(key=lambda item: (item["application_id"], item["name"], item["path"]))
    return skills


def _global_skills(root: Path) -> list[dict[str, Any]]:
    """Project-global Skills available to AgentLoom runtime Agents.

    This intentionally follows AgentLoom's global discovery boundary only:
    explicit ``config/system.yaml`` entries plus the root ``skills/`` default
    directory.  Framework/Codex Skills and Application packages are outside
    this count.
    """

    configured: Any = None
    config_path = root / "config" / "system.yaml"
    text = _read_text_bounded(root, config_path, max_bytes=1024 * 1024)
    if text is not None:
        try:
            document = yaml.safe_load(text) or {}
        except (TypeError, ValueError, yaml.YAMLError):
            document = {}
        if isinstance(document, Mapping):
            configured = document.get("skills")

    sources: list[Path] = []
    if configured != []:
        sources.append(root / "skills")
    sources.extend(_configured_global_skill_sources(root, configured))

    manifests: dict[Path, Path] = {}
    for source in sources:
        safe_file = _safe_project_file(root, source, suffixes={".md"})
        if safe_file is not None and safe_file.name.lower() == "skill.md":
            manifests[safe_file.resolve()] = safe_file
            continue
        if not _safe_project_directory(root, source):
            continue
        for manifest in _skill_manifests(root, source):
            manifests[manifest.resolve()] = manifest

    skills: list[dict[str, Any]] = []
    ids: set[str] = set()
    for manifest in sorted(manifests.values(), key=lambda path: path.relative_to(root).as_posix()):
        metadata = _skill_metadata(root, manifest)
        name = _display_text(metadata.get("name")) or manifest.parent.name
        skill_id = f"global:{name}"
        if skill_id in ids:
            skill_id = f"{skill_id}:{manifest.parent.relative_to(root).as_posix()}"
        ids.add(skill_id)
        skills.append(
            {
                "id": skill_id,
                "application_id": None,
                "name": name,
                "description": _display_text(metadata.get("description")),
                "origin": "global",
                "path": manifest.relative_to(root).as_posix(),
            }
        )
    return skills


def _configured_global_skill_sources(root: Path, configured: Any) -> list[Path]:
    items = configured.get("items") if isinstance(configured, Mapping) else configured
    if isinstance(items, (str, Mapping)):
        items = [items]
    if not isinstance(items, list):
        return []
    sources: list[Path] = []
    for item in items:
        raw_path = item.get("path") if isinstance(item, Mapping) else item
        if not isinstance(raw_path, str) or not raw_path.strip() or "\\" in raw_path:
            continue
        path = Path(raw_path.strip())
        if path.is_absolute() or ".." in path.parts:
            continue
        sources.append(root / path)
    return sources


def _skill_manifests(root: Path, skills_root: Path) -> list[Path]:
    by_directory: dict[Path, Path] = {}

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    visit(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False) or entry.name not in {"SKILL.md", "skill.md"}:
                    continue
            except OSError:
                continue
            path = _safe_project_file(root, Path(entry.path), suffixes={".md"})
            if path is None:
                continue
            previous = by_directory.get(path.parent)
            if previous is None or (path.name == "SKILL.md" and previous.name != "SKILL.md"):
                by_directory[path.parent] = path

    visit(skills_root)
    return sorted(by_directory.values(), key=lambda path: path.relative_to(skills_root).as_posix())


def _skill_metadata(root: Path, manifest: Path) -> dict[str, Any]:
    text = _read_text_bounded(root, manifest, max_bytes=SKILL_MANIFEST_MAX_BYTES)
    if text is None:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    closing_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing_index = index
            break
    if closing_index is None:
        return {}
    try:
        metadata = yaml.safe_load("\n".join(lines[1:closing_index])) or {}
    except (TypeError, ValueError, yaml.YAMLError):
        return {}
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def schedule_catalog(
    project_root: str | Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return only durable schedule and service-heartbeat projections."""

    root = Path(project_root).expanduser().resolve()
    checked_at = _as_utc(now)
    schedules_dir = root / ".agentloom" / "schedules"
    document, document_error = _read_json_object(
        root,
        schedules_dir / "jobs.json",
        max_bytes=SCHEDULE_DOCUMENT_MAX_BYTES,
    )
    heartbeat, _ = _read_json_object(
        root,
        schedules_dir / "serve-status.json",
        max_bytes=128 * 1024,
    )
    if document is not None and (
        document.get("version") != 1
        or not isinstance(document.get("jobs"), list)
        or not isinstance(document.get("executions"), list)
    ):
        document = None
        document_error = "Schedule storage is unreadable."
    if document is None:
        document = {"jobs": [], "executions": []}
    jobs = document.get("jobs") if isinstance(document.get("jobs"), list) else []
    executions = document.get("executions") if isinstance(document.get("executions"), list) else []
    jobs = [item for item in jobs if isinstance(item, Mapping)]
    executions = [item for item in executions if isinstance(item, Mapping)]

    executions_by_job: dict[str, list[Mapping[str, Any]]] = {}
    for execution in executions:
        job_id = execution.get("job_id")
        if isinstance(job_id, str) and job_id:
            executions_by_job.setdefault(job_id, []).append(execution)

    items: list[dict[str, Any]] = []
    for job in jobs:
        job_id = _display_text(job.get("id"))
        if not job_id:
            continue
        history = executions_by_job.get(job_id, [])
        latest = max(history, key=_execution_sort_key) if history else None
        state = _display_text(job.get("state")) or "unknown"
        items.append(
            {
                "id": job_id,
                "name": _display_text(job.get("name")) or job_id,
                "enabled": state == "scheduled",
                "state": state,
                "yaml_path": _safe_stored_path(job.get("yaml_path")),
                "trigger": _trigger_summary(job.get("schedule")),
                "next_run_at": _optional_text(job.get("next_run_at")),
                "last_run_at": _optional_text(job.get("last_run_at")),
                "last_status": _optional_text(job.get("last_status")),
                "run_count": _nonnegative_int(job.get("run_count")),
                "last_execution": _execution_summary(latest) if latest is not None else None,
            }
        )
    items.sort(key=lambda item: (item["next_run_at"] or "~", item["name"], item["id"]))

    return {
        "items": items,
        "service": _schedule_service_summary(
            jobs,
            executions,
            heartbeat if isinstance(heartbeat, Mapping) else {},
            checked_at=checked_at,
            document_error=document_error,
        ),
    }


def _schedule_service_summary(
    jobs: list[Mapping[str, Any]],
    executions: list[Mapping[str, Any]],
    heartbeat: Mapping[str, Any],
    *,
    checked_at: datetime,
    document_error: str | None,
) -> dict[str, Any]:
    pid = heartbeat.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        pid = None
    last_tick_at = _optional_text(heartbeat.get("last_tick_at"))
    recent = False
    parsed_tick = _parse_iso(last_tick_at)
    if parsed_tick is not None:
        tick_seconds = heartbeat.get("tick_seconds")
        if isinstance(tick_seconds, bool) or not isinstance(tick_seconds, (int, float)):
            tick_seconds = 1.0
        age = (checked_at - parsed_tick).total_seconds()
        recent = age <= max(float(tick_seconds) * 3, 5.0)

    if document_error is not None:
        state = "error"
    elif heartbeat.get("stopped_at"):
        state = "stopped"
    elif pid is not None and _pid_is_alive(pid) and recent:
        state = "running"
    elif heartbeat:
        state = "stale"
    else:
        state = "stopped"

    due_count = 0
    claimed_count = 0
    for job in jobs:
        claim = job.get("claim")
        claim_live = False
        if isinstance(claim, Mapping):
            expires_at = _parse_iso(_optional_text(claim.get("expires_at")))
            claim_live = expires_at is not None and expires_at > checked_at
        if claim_live:
            claimed_count += 1
        next_run_at = _parse_iso(_optional_text(job.get("next_run_at")))
        if not claim_live and job.get("state") == "scheduled" and next_run_at is not None and next_run_at <= checked_at:
            due_count += 1

    heartbeat_error = _optional_text(heartbeat.get("last_error"))
    return {
        "state": state,
        "pid": pid,
        "started_at": _optional_text(heartbeat.get("started_at")),
        "last_tick_at": last_tick_at,
        "last_success_at": _optional_text(heartbeat.get("last_success_at")),
        "last_error": document_error or heartbeat_error,
        "job_count": len(jobs),
        "due_count": due_count,
        "claimed_count": claimed_count,
        "execution_count": len(executions),
    }


def _trigger_summary(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in ("kind", "at", "seconds", "expression", "timezone"):
        value = raw.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            result[key] = value
    return result


def _execution_summary(execution: Mapping[str, Any]) -> dict[str, Any]:
    exit_code = execution.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        exit_code = None
    return {
        "id": _display_text(execution.get("id")),
        "job_id": _display_text(execution.get("job_id")),
        "status": _display_text(execution.get("status")) or "unknown",
        "trigger": _display_text(execution.get("trigger")) or "unknown",
        "claimed_at": _optional_text(execution.get("claimed_at")),
        "started_at": _optional_text(execution.get("started_at")),
        "finished_at": _optional_text(execution.get("finished_at")),
        "exit_code": exit_code,
        "error": _optional_text(execution.get("error")),
    }


def _execution_sort_key(execution: Mapping[str, Any]) -> tuple[datetime, str]:
    for key in ("finished_at", "started_at", "claimed_at"):
        parsed = _parse_iso(_optional_text(execution.get(key)))
        if parsed is not None:
            return parsed, _display_text(execution.get("id"))
    return datetime.min.replace(tzinfo=UTC), _display_text(execution.get("id"))


def _read_yaml_object(root: Path, path: Path) -> dict[str, Any]:
    text = _read_text_bounded(root, path, max_bytes=AGENT_YAML_MAX_BYTES)
    if text is None:
        return {}
    try:
        value = yaml.safe_load(text) or {}
    except (TypeError, ValueError, yaml.YAMLError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _read_agent_definition_object(
    root: Path,
    path: Path,
    *,
    cache: AgentDefinitionCache | None,
) -> dict[str, Any]:
    """Reuse a bridge parse while preserving the catalog's file safety limits."""

    safe = _safe_project_file(root, path, suffixes={".yaml", ".yml"})
    if safe is None:
        return {}
    try:
        if safe.stat().st_size > AGENT_YAML_MAX_BYTES:
            return {}
    except OSError:
        return {}
    if cache is None:
        return _read_yaml_object(root, safe)

    from src.tui_bridge.definition import read_agent_definition

    result = read_agent_definition(safe, cache=cache)
    if result.definition is None:
        return {}
    return result.definition


def _read_json_object(
    root: Path,
    path: Path,
    *,
    max_bytes: int,
) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, None
    text = _read_text_bounded(root, path, max_bytes=max_bytes)
    if text is None:
        return None, "Schedule storage is unreadable."
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "Schedule storage is unreadable."
    if not isinstance(value, dict):
        return None, "Schedule storage is unreadable."
    return value, None


def _read_text_bounded(root: Path, path: Path, *, max_bytes: int) -> str | None:
    safe = _safe_project_file(root, path)
    if safe is None:
        return None
    try:
        size = safe.stat().st_size
        if size > max_bytes:
            return None
        with safe.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
    except OSError:
        return None
    if len(payload) > max_bytes:
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeError:
        return None


def _safe_project_file(
    root: Path,
    raw_path: Any,
    *,
    suffixes: set[str] | None = None,
) -> Path | None:
    if isinstance(raw_path, Path):
        candidate = raw_path
    elif isinstance(raw_path, str) and raw_path.strip():
        candidate = Path(raw_path.strip())
    else:
        return None
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    if not relative.parts or not _path_without_symlinks(root, relative):
        return None
    try:
        mode = candidate.stat(follow_symlinks=False).st_mode
    except OSError:
        return None
    if not stat.S_ISREG(mode):
        return None
    if suffixes is not None and candidate.suffix.lower() not in suffixes:
        return None
    return candidate


def _safe_project_directory(root: Path, raw_path: Path) -> bool:
    candidate = Path(os.path.abspath(raw_path))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return False
    if not relative.parts or not _path_without_symlinks(root, relative):
        return False
    try:
        return stat.S_ISDIR(candidate.stat(follow_symlinks=False).st_mode)
    except OSError:
        return False


def _path_without_symlinks(root: Path, relative: Path) -> bool:
    current = root
    for part in relative.parts:
        if part in {"", ".", ".."}:
            return False
        current /= part
        try:
            mode = current.stat(follow_symlinks=False).st_mode
        except OSError:
            return False
        if stat.S_ISLNK(mode):
            return False
    return True


def _application_id_for_agent_path(relative: Path) -> str | None:
    try:
        workflows_index = relative.parts.index("workflows")
    except ValueError:
        return None
    if workflows_index <= 1:
        return None
    return _application_id_value("/".join(relative.parts[1:workflows_index]))


def _application_id_value(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip() or "\\" in raw:
        return None
    path = Path(raw.strip())
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _safe_stored_path(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        return ""
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        return ""
    return candidate.as_posix()


def _display_text(raw: Any) -> str:
    if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
        return ""
    return str(raw).strip().replace("\x00", "")[:4096]


def _optional_text(raw: Any) -> str | None:
    value = _display_text(raw)
    return value or None


def _nonnegative_int(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return max(raw, 0)


def _as_utc(value: datetime | None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False
