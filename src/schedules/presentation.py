"""Read-only schedule projections for AgentLoom presentation surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from agentloom.configuration.yaml_loader import load_unique_yaml
from agentloom.execution.context import RuntimeHome, resolve_runtime_home
from agentloom.execution.storage import SecureDirectory
from agentloom.schedules.schedule import validate_schedule
from agentloom.schedules.schema import (
    SCHEDULE_DOCUMENT_MAX_BYTES,
    SCHEDULE_HEARTBEAT_MAX_BYTES,
    decode_json_object,
    execution_rank,
    heartbeat_state,
    safe_nonnegative_int,
    validate_heartbeat,
    validate_schedule_document,
)

SYSTEM_CONFIG_MAX_BYTES = 1024 * 1024
SYSTEM_CONFIG_MAX_EVENTS = 4096
SYSTEM_CONFIG_MAX_DEPTH = 32
SYSTEM_CONFIG_MAX_SCALAR_CHARS = 4096
RUNTIME_ROOT_MAX_CHARS = 4096


def schedule_catalog(
    project_root: str | Path,
    *,
    now: datetime | None = None,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    """Project durable jobs and service health without creating storage."""

    root = Path(project_root).expanduser().resolve()
    checked_at = _as_utc(now)
    try:
        if runtime_root is None:
            resolved_runtime_root = resolve_schedule_runtime_root(root)
        else:
            explicit_root = Path(runtime_root).expanduser()
            if not explicit_root.is_absolute():
                explicit_root = root / explicit_root
            runtime_home = RuntimeHome(explicit_root)
            resolved_runtime_root = runtime_home.validate_root()
    except (OSError, RuntimeError, TypeError, ValueError):
        return _unreadable_catalog(checked_at)

    document, document_error = _read_json_object(
        resolved_runtime_root,
        Path("schedules/jobs.json"),
        max_bytes=SCHEDULE_DOCUMENT_MAX_BYTES,
    )
    heartbeat, heartbeat_error = _read_json_object(
        resolved_runtime_root,
        Path("schedules/serve-status.json"),
        max_bytes=SCHEDULE_HEARTBEAT_MAX_BYTES,
    )
    if document is None:
        jobs: list[Mapping[str, Any]] = []
        executions: list[Mapping[str, Any]] = []
    else:
        try:
            jobs, executions = validate_schedule_document(
                document,
                schedule_validator=_validate_schedule_record,
            )
        except ValueError:
            return _unreadable_catalog(checked_at)

    if heartbeat is None:
        heartbeat_record: Mapping[str, Any] = {}
    else:
        try:
            heartbeat_record = validate_heartbeat(heartbeat)
        except ValueError:
            heartbeat_record = {}
            heartbeat_error = "Schedule storage is unreadable."

    latest_execution_by_job: dict[str, Mapping[str, Any]] = {}
    for execution in executions:
        job_id = str(execution["job_id"])
        current = latest_execution_by_job.get(job_id)
        if current is None or execution_rank(execution) > execution_rank(current):
            latest_execution_by_job[job_id] = execution

    items: list[dict[str, Any]] = []
    for job in jobs:
        job_id = _display_text(job.get("id"))
        if not job_id:
            continue
        latest = latest_execution_by_job.get(job_id)
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
                "last_execution": (_execution_summary(latest) if latest is not None else None),
            }
        )
    items.sort(
        key=lambda item: (
            item["next_run_at"] or "~",
            item["name"],
            item["id"],
        )
    )
    return {
        "items": items,
        "service": _schedule_service_summary(
            jobs,
            executions,
            heartbeat_record,
            checked_at=checked_at,
            document_error=document_error or heartbeat_error,
        ),
    }


def resolve_schedule_runtime_root(project_root: str | Path) -> Path:
    """Resolve the configured Schedule storage root without creating it."""

    root = Path(project_root).expanduser().resolve()
    system_config, system_config_error = _read_system_config(root)
    if system_config_error is not None:
        raise ValueError(system_config_error)
    return resolve_runtime_home(
        system_config,
        agent_root=root,
    ).validate_root()


def _unreadable_catalog(checked_at: datetime) -> dict[str, Any]:
    return {
        "items": [],
        "service": _schedule_service_summary(
            [],
            [],
            {},
            checked_at=checked_at,
            document_error="Schedule storage is unreadable.",
        ),
    }


def _read_system_config(
    root: Path,
) -> tuple[dict[str, Any], str | None]:
    try:
        with SecureDirectory(root, create=False) as storage:
            payload, truncated = storage.read_bytes_up_to(
                Path("config/system.yaml"),
                SYSTEM_CONFIG_MAX_BYTES,
            )
        if truncated:
            return {}, "System configuration is unreadable."
        text = payload.decode("utf-8")
        _validate_system_config_yaml(text)
        value = load_unique_yaml(text) or {}
    except FileNotFoundError:
        return {}, None
    except (
        OSError,
        RuntimeError,
        UnicodeError,
        TypeError,
        ValueError,
        yaml.YAMLError,
    ):
        return {}, "System configuration is unreadable."
    if not isinstance(value, dict):
        return {}, "System configuration is unreadable."
    runtime = value.get("runtime")
    if runtime is None:
        return {}, None
    if not isinstance(runtime, dict):
        return {}, "System configuration is unreadable."
    if "root_dir" not in runtime:
        return {}, None
    root_dir = runtime["root_dir"]
    if (
        not isinstance(root_dir, str)
        or not root_dir
        or root_dir != root_dir.strip()
        or len(root_dir) > RUNTIME_ROOT_MAX_CHARS
        or any(character in root_dir for character in ("\x00", "\n", "\r"))
    ):
        return {}, "System configuration is unreadable."
    return {"runtime": {"root_dir": root_dir}}, None


def _validate_system_config_yaml(text: str) -> None:
    event_count = 0
    depth = 0
    start_events = (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)
    end_events = (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)
    for event in yaml.parse(text, Loader=yaml.SafeLoader):
        event_count += 1
        if event_count > SYSTEM_CONFIG_MAX_EVENTS:
            raise ValueError("system config has too many YAML events")
        if isinstance(event, yaml.events.AliasEvent):
            raise ValueError("system config YAML aliases are unsupported")
        if isinstance(event, yaml.events.ScalarEvent) and len(event.value) > SYSTEM_CONFIG_MAX_SCALAR_CHARS:
            raise ValueError("system config scalar is too large")
        if isinstance(event, start_events):
            depth += 1
            if depth > SYSTEM_CONFIG_MAX_DEPTH:
                raise ValueError("system config is nested too deeply")
        elif isinstance(event, end_events):
            depth -= 1


def _read_json_object(
    root: Path,
    relative: Path,
    *,
    max_bytes: int,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with SecureDirectory(root, create=False) as storage:
            with storage.open_binary_reader(relative) as stream:
                payload = stream.read(max_bytes + 1)
    except FileNotFoundError:
        return None, None
    except (OSError, RuntimeError, ValueError):
        return None, "Schedule storage is unreadable."
    if len(payload) > max_bytes:
        return None, "Schedule storage is unreadable."
    try:
        value = decode_json_object(payload)
    except ValueError:
        return None, "Schedule storage is unreadable."
    if not isinstance(value, dict):
        return None, "Schedule storage is unreadable."
    return value, None


def _validate_schedule_record(schedule: Mapping[str, Any]) -> None:
    try:
        normalized = validate_schedule(dict(schedule))
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError("invalid job.schedule") from error
    if normalized != dict(schedule):
        raise ValueError("schedule is not in canonical persisted form")


def _schedule_service_summary(
    jobs: list[Mapping[str, Any]],
    executions: list[Mapping[str, Any]],
    heartbeat: Mapping[str, Any],
    *,
    checked_at: datetime,
    document_error: str | None,
) -> dict[str, Any]:
    pid: int | None = None
    if document_error is not None:
        state = "error"
    elif heartbeat:
        try:
            state, pid = heartbeat_state(heartbeat, checked_at=checked_at)
        except ValueError:
            state = "error"
            pid = None
    else:
        state = "stopped"
    last_tick_at = _optional_text(heartbeat.get("last_tick_at"))

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

    return {
        "state": state,
        "pid": pid,
        "started_at": _optional_text(heartbeat.get("started_at")),
        "last_tick_at": last_tick_at,
        "last_success_at": _optional_text(heartbeat.get("last_success_at")),
        "last_error": document_error or _optional_text(heartbeat.get("last_error")),
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
    return str(raw).strip().replace("\x00", "").replace("\n", " ").replace("\r", " ")[:4096]


def _optional_text(raw: Any) -> str | None:
    return _display_text(raw) or None


def _nonnegative_int(raw: Any) -> int:
    return safe_nonnegative_int(raw, field="projected integer")


def _as_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        return None
