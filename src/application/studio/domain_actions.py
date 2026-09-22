"""Application actions consumed by Studio presentation adapters."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

from agentloom.application.studio.errors import StudioServiceError
from agentloom.application.studio.query_service import StudioQueryService

_DEFAULT_AGENT_PAGE_SIZE = 10
_MAX_AGENT_PAGE_SIZE = 10
_MAX_SUMMARY_TEXT = 180
_MAX_CAPABILITY_ITEMS = 12
_MAX_SOURCE_PROFILES = 6


def execute_domain_action(
    project_root: Path,
    action: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    queries = StudioQueryService(project_root)
    if action == "catalog":
        if params:
            raise StudioServiceError("invalid_params", "catalog params must be empty")
        return queries.bootstrap()
    if action == "application.detail":
        return _model_application_detail(queries, params)
    if action == "application.validate":
        detail = queries.application_detail(
            _application_id_param(params, action=action)
        )
        agents = _flatten_agents(detail.get("agents"))
        errors = [
            str(error)
            for agent in agents
            for error in (agent.get("validation") or {}).get("errors", [])
        ]
        return {
            "application_id": detail["application"]["id"],
            "working_revision": detail["working_revision"],
            "valid": not errors and detail["application"]["health"] == "healthy",
            "errors": errors,
        }
    if action == "application.impact":
        return _application_impact(queries, params)
    if action in {"run.start", "run.resume", "run.restart"}:
        return _run_application(queries, project_root, action, params)
    if action == "run.stop":
        return _stop_run(queries, params)
    if action == "run.detail":
        return _run_detail(queries, params)
    raise StudioServiceError("method_not_found", f"unknown domain action: {action}")


def _model_application_detail(
    queries: StudioQueryService,
    params: dict[str, Any],
) -> dict[str, Any]:
    allowed = {"application_id", "offset", "limit"}
    if set(params) - allowed or not isinstance(params.get("application_id"), str):
        raise StudioServiceError(
            "invalid_params",
            "application.detail requires application_id and accepts integer offset/limit",
        )
    offset = params.get("offset", 0)
    limit = params.get("limit", _DEFAULT_AGENT_PAGE_SIZE)
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or limit > _MAX_AGENT_PAGE_SIZE
    ):
        raise StudioServiceError(
            "invalid_params",
            f"application.detail offset must be >= 0 and limit must be 1..{_MAX_AGENT_PAGE_SIZE}",
        )

    detail = queries.application_detail(str(params["application_id"]))
    agents = _flatten_agents(detail.get("agents"))
    selected = agents[offset : offset + limit]
    returned = len(selected)
    next_offset = offset + returned if offset + returned < len(agents) else None
    supervisors = sum(agent.get("role") == "supervisor" for agent in agents)
    workers = sum(agent.get("role") == "worker" for agent in agents)
    invalid = sum(not bool((agent.get("validation") or {}).get("valid")) for agent in agents)
    return {
        "schema_version": 2,
        "application": detail["application"],
        "working_revision": detail["working_revision"],
        "running_revision": detail["running_revision"],
        "overview": {
            "supervisor_count": supervisors,
            "worker_count": workers,
            "invalid_agent_count": invalid,
            "model_types": sorted({
                str((agent.get("model") or {}).get("type"))
                for agent in agents
                if (agent.get("model") or {}).get("type")
            }),
        },
        "effective_capabilities": _model_capabilities(agents),
        "agents": [_model_agent(agent) for agent in selected],
        "page": {
            "offset": offset,
            "limit": limit,
            "returned": returned,
            "total": len(agents),
            "next_offset": next_offset,
        },
    }


def _model_agent(agent: dict[str, Any]) -> dict[str, Any]:
    validation = agent.get("validation") or {}
    workers = agent.get("workers") if isinstance(agent.get("workers"), list) else []
    return {
        "id": str(agent.get("id") or ""),
        "name": str(agent.get("name") or ""),
        "description": _bounded_text(agent.get("description")),
        "role": str(agent.get("role") or ""),
        "workflow_summary": _bounded_text(agent.get("workflow")),
        "model": agent.get("model") or {},
        "tool_names": [
            str(tool.get("name"))
            for tool in (agent.get("tools") or [])[:_MAX_CAPABILITY_ITEMS]
            if isinstance(tool, dict) and tool.get("name")
        ],
        "skill_names": [
            str(skill.get("name"))
            for skill in (agent.get("skills") or [])[:_MAX_CAPABILITY_ITEMS]
            if isinstance(skill, dict) and skill.get("name")
        ],
        "permission_source": _source_summary(agent.get("permissions")),
        "hook_source": _source_summary(agent.get("hooks")),
        "mcp_source": _source_summary(agent.get("mcp")),
        "worker_ids": [
            str(worker.get("id"))
            for worker in workers[:_MAX_CAPABILITY_ITEMS]
            if isinstance(worker, dict) and worker.get("id")
        ],
        "source_path": str(agent.get("source_path") or ""),
        "validation": {
            "valid": bool(validation.get("valid")),
            "errors": [
                _bounded_text(error)
                for error in (validation.get("errors") or [])[:5]
            ],
        },
    }


def _model_capabilities(agents: list[dict[str, Any]]) -> dict[str, Any]:
    tools = sorted({
        str(tool.get("name"))
        for agent in agents
        for tool in (agent.get("tools") or [])
        if isinstance(tool, dict) and tool.get("name")
    })
    skills_by_path: dict[str, dict[str, Any]] = {}
    for agent in agents:
        for skill in agent.get("skills") or []:
            if not isinstance(skill, dict) or not skill.get("path"):
                continue
            path = str(skill["path"])
            skills_by_path[path] = {
                "name": str(skill.get("name") or ""),
                "description": _bounded_text(skill.get("description"), limit=180),
                "source": str(skill.get("source") or ""),
                "path": path,
            }
    return {
        "tools": tools[:_MAX_CAPABILITY_ITEMS],
        "tool_count": len(tools),
        "skills": list(skills_by_path.values())[:_MAX_CAPABILITY_ITEMS],
        "skill_count": len(skills_by_path),
        "permission_profiles": _unique_source_profiles(agents, "permissions"),
        "hook_profiles": _unique_source_profiles(agents, "hooks"),
        "mcp_profiles": _unique_source_profiles(agents, "mcp"),
    }


def _unique_source_profiles(agents: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for agent in agents:
        raw = agent.get(key)
        if not isinstance(raw, dict):
            continue
        profile = {
            "source": str(raw.get("source") or "none"),
            "source_path": raw.get("source_path"),
            "summary": _bounded_text(
                json.dumps(raw.get("value"), ensure_ascii=False, sort_keys=True),
                limit=160,
            ),
        }
        identity = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        profiles[identity] = profile
    return list(profiles.values())[:_MAX_SOURCE_PROFILES]


def _source_summary(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"source": "none", "source_path": None}
    return {
        "source": str(raw.get("source") or "none"),
        "source_path": raw.get("source_path"),
    }


def _bounded_text(raw: Any, *, limit: int = _MAX_SUMMARY_TEXT) -> str:
    text = str(raw or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _application_impact(queries: StudioQueryService, params: dict[str, Any]) -> dict[str, Any]:
    if set(params) != {"paths"} or not isinstance(params.get("paths"), list):
        raise StudioServiceError("invalid_params", "application.impact requires paths[]")
    applications = [str(item["id"]) for item in queries.bootstrap().get("applications", [])]
    affected: set[str] = set()
    global_change = False
    normalized: list[str] = []
    for raw in params["paths"]:
        if not isinstance(raw, str) or not raw.strip():
            raise StudioServiceError("invalid_params", "impact paths must be non-empty strings")
        path = Path(raw.strip())
        if path.is_absolute() or ".." in path.parts:
            raise StudioServiceError("invalid_params", f"impact path must stay inside the project: {raw}")
        display = path.as_posix()
        normalized.append(display)
        if len(path.parts) >= 2 and path.parts[0] == "applications":
            candidate = path.parts[1]
            if candidate in applications:
                affected.add(candidate)
        else:
            global_change = True
    if global_change:
        affected.update(applications)
    return {
        "paths": normalized,
        "scope": "global" if global_change else "application",
        "affected_applications": sorted(affected),
        "count": len(affected),
    }


def _run_application(
    queries: StudioQueryService,
    project_root: Path,
    action: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    allowed = {"application_id", "workflow_path", "task", "task_id"}
    if set(params) - allowed or not isinstance(params.get("application_id"), str):
        raise StudioServiceError("invalid_params", f"{action} requires application_id")
    application_id = str(params["application_id"])
    detail = queries.application_detail(application_id)
    workflows = [
        str(agent.get("source_path"))
        for agent in detail.get("agents", [])
        if isinstance(agent, dict) and isinstance(agent.get("source_path"), str)
    ]
    requested = params.get("workflow_path")
    if requested is None:
        if len(workflows) != 1:
            raise StudioServiceError(
                "invalid_params",
                f"{action} requires workflow_path when Application has {len(workflows)} supervisors",
            )
        workflow = workflows[0]
    elif isinstance(requested, str) and requested in workflows:
        workflow = requested
    else:
        raise StudioServiceError("invalid_params", "workflow_path is not a supervisor in this Application")

    task = params.get("task")
    if task is not None and (not isinstance(task, str) or len(task.encode("utf-8")) > 64 * 1024):
        raise StudioServiceError("invalid_params", "task must be a string no larger than 64 KiB")
    resume_task_id = params.get("task_id") if action == "run.resume" else None
    if action == "run.resume" and not isinstance(resume_task_id, str):
        raise StudioServiceError("invalid_params", "run.resume requires task_id")

    from agentloom.application.run import (
        ApplicationRunError,
        ApplicationRunInterrupted,
    )
    from agentloom.application.runner import execute_app

    events: list[dict[str, Any]] = []

    def record(event: Any) -> None:
        run = getattr(event, "run", None)
        events.append({
            "event": str(getattr(event, "event", "unknown")),
            "application_id": getattr(run, "application_id", application_id),
            "task_id": getattr(run, "task_id", None),
            "run_id": getattr(run, "run_id", None),
            "error": getattr(event, "error", None),
        })

    try:
        completed = execute_app(
            str(project_root / workflow),
            resume_task_id=resume_task_id,
            task_override=task,
            event_sink=record,
        )
    except ApplicationRunInterrupted as error:
        return {
            "status": "interrupted",
            "application_id": error.run.application_id,
            "task_id": error.run.task_id,
            "run_id": error.run.run_id,
            "resumable": error.resumable,
            "events": events[-8:],
        }
    except ApplicationRunError as error:
        return {
            "status": "failed",
            "application_id": error.run.application_id,
            "task_id": error.run.task_id,
            "run_id": error.run.run_id,
            "error": str(error),
            "events": events[-8:],
        }
    return {
        "status": "completed",
        "application_id": completed.run.application_id,
        "task_id": completed.run.task_id,
        "run_id": completed.run.run_id,
        "output": completed.output[-16_000:],
        "output_truncated": len(completed.output) > 16_000,
        "events": events[-8:],
    }


def _stop_run(queries: StudioQueryService, params: dict[str, Any]) -> dict[str, Any]:
    required = {"application_id", "run_id"}
    if set(params) != required or not all(isinstance(params.get(key), str) for key in required):
        raise StudioServiceError("invalid_params", "run.stop requires application_id and run_id")
    detail = _run_detail(queries, params)
    if (detail.get("summary") or {}).get("status") != "running":
        raise StudioServiceError("invalid_state", "only a running Run can be stopped")
    task_id = str(detail["summary"]["task_id"])
    runtime_root = queries.runtime_root()
    heartbeat_path = runtime_root / "checkpoints" / Path(*str(params["application_id"]).split("/")) / task_id / "heartbeat.json"
    try:
        heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StudioServiceError("invalid_state", "running Run has no readable supervisor heartbeat") from error
    if heartbeat.get("run_id") != params["run_id"] or heartbeat.get("status") != "running":
        raise StudioServiceError("invalid_state", "supervisor heartbeat does not match the requested Run")
    pid = heartbeat.get("pid")
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
        raise StudioServiceError("invalid_state", "supervisor heartbeat has no safe process identity")
    try:
        os.kill(pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError, OSError) as error:
        raise StudioServiceError("invalid_state", "could not interrupt the Run process") from error
    return {
        "status": "stop_requested",
        "application_id": params["application_id"],
        "task_id": task_id,
        "run_id": params["run_id"],
    }


def _flatten_agents(raw: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    stack = list(reversed(raw)) if isinstance(raw, list) else []
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        result.append(item)
        workers = item.get("workers")
        if isinstance(workers, list):
            stack.extend(reversed(workers))
    return result


def _application_id_param(
    params: dict[str, Any],
    *,
    action: str,
) -> str:
    if set(params) != {"application_id"} or not isinstance(
        params.get("application_id"),
        str,
    ):
        raise StudioServiceError(
            "invalid_params",
            f"{action} requires application_id",
        )
    return str(params["application_id"])


def _run_detail(
    queries: StudioQueryService,
    params: dict[str, Any],
) -> dict[str, Any]:
    expected = {"application_id", "run_id"}
    if "system_id" in params:
        expected.add("system_id")
    if set(params) != expected:
        raise StudioServiceError(
            "invalid_params",
            "run.detail requires application_id and run_id",
        )
    application_id = params.get("application_id")
    run_id = params.get("run_id")
    system_id = params.get("system_id")
    if (
        not isinstance(application_id, str)
        or not application_id.strip()
        or not isinstance(run_id, str)
        or not run_id.strip()
    ):
        raise StudioServiceError(
            "invalid_params",
            "run.detail requires application_id and run_id",
        )
    if system_id is not None and (
        not isinstance(system_id, str) or not system_id.strip()
    ):
        raise StudioServiceError(
            "invalid_params",
            "system_id must be a non-empty string",
        )
    return queries.run_detail(
        run_id,
        application_id=application_id,
        system_id=system_id,
    )
