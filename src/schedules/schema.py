"""Shared durable Schedule record semantics."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_SAFE_INTEGER = (1 << 53) - 1
MAX_PID = (1 << 31) - 1
JOB_NAME_MAX_BYTES = 512
MAX_HEARTBEAT_TICK_SECONDS = 24 * 60 * 60
MAX_INTERVAL_SECONDS = 999_999_999 * 24 * 60 * 60
SCHEDULE_DOCUMENT_MAX_BYTES = 8 * 1024 * 1024
SCHEDULE_HEARTBEAT_MAX_BYTES = 128 * 1024
MAX_TEXT_CHARS = 4096
EXECUTION_COMMAND_MAX_ITEMS = 32
EXECUTION_COMMAND_MAX_BYTES = 4 * 1024
EXECUTION_COMMAND_ITEM_MAX_BYTES = 1024
EXECUTION_ERROR_MAX_BYTES = 4 * 1024
_JOB_STATES = frozenset({"scheduled", "paused", "completed"})
_EXECUTION_STATES = frozenset({"claimed", "running", "succeeded", "failed", "abandoned"})
_EXECUTION_TRIGGERS = frozenset({"manual", "scheduled"})


def decode_json_object(payload: bytes) -> dict[str, Any]:
    """Decode strict JSON without duplicate keys or unsafe numeric values."""

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_parse_finite_json_float,
            parse_int=_parse_safe_json_int,
        )
    except (RecursionError, UnicodeError, ValueError) as exc:
        raise ValueError("invalid Schedule JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Schedule JSON must contain an object")
    return value


def validate_schedule_document(
    document: Mapping[str, Any],
    *,
    schedule_validator: Callable[[Mapping[str, Any]], None] | None = None,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Validate one complete version-1 jobs and executions document."""

    version = document.get("version")
    jobs = document.get("jobs")
    executions = document.get("executions")
    if type(version) is not int or version != 1 or not isinstance(jobs, list) or not isinstance(executions, list):
        raise ValueError("invalid schedule document")

    job_ids: set[str] = set()
    validated_jobs: list[Mapping[str, Any]] = []
    for raw_job in jobs:
        if not isinstance(raw_job, Mapping):
            raise ValueError("invalid schedule job")
        _validate_job(raw_job, schedule_validator=schedule_validator)
        job_id = _canonical_identity(raw_job["id"], field="job.id")
        if job_id in job_ids:
            raise ValueError("duplicate schedule job id")
        job_ids.add(job_id)
        validated_jobs.append(raw_job)

    execution_ids: set[str] = set()
    execution_sequences: set[int] = set()
    validated_executions: list[Mapping[str, Any]] = []
    for raw_execution in executions:
        if not isinstance(raw_execution, Mapping):
            raise ValueError("invalid schedule execution")
        _validate_execution(raw_execution)
        execution_id = _canonical_identity(
            raw_execution["id"],
            field="execution.id",
        )
        if execution_id in execution_ids:
            raise ValueError("duplicate schedule execution id")
        execution_ids.add(execution_id)
        if "sequence" in raw_execution:
            sequence = safe_nonnegative_int(
                raw_execution["sequence"],
                field="execution.sequence",
            )
            if sequence in execution_sequences:
                raise ValueError("duplicate schedule execution sequence")
            execution_sequences.add(sequence)
        validated_executions.append(raw_execution)
    _validate_document_relationships(validated_jobs, validated_executions)
    return validated_jobs, validated_executions


def execution_sequence(execution: Mapping[str, Any]) -> int:
    """Return a durable sequence, with ``-1`` for pre-sequence records."""

    sequence = execution.get("sequence")
    if isinstance(sequence, int) and not isinstance(sequence, bool) and 0 <= sequence <= MAX_SAFE_INTEGER:
        return sequence
    return -1


def execution_rank(execution: Mapping[str, Any]) -> tuple[int, str, str]:
    """Order executions exactly as the persistence layer does."""

    observed_at = execution.get("finished_at") or execution.get("started_at") or execution.get("claimed_at") or ""
    return (
        execution_sequence(execution),
        _normalized_instant(observed_at),
        str(execution.get("id") or ""),
    )


def heartbeat_state(
    heartbeat: Mapping[str, Any],
    *,
    checked_at: datetime,
) -> tuple[str, int | None]:
    """Validate one persisted heartbeat and return its trustworthy state."""

    validated = validate_heartbeat(heartbeat)
    checked_at = _as_utc(checked_at)
    pid = validated["pid"]
    stopped_at = validated["stopped_at"]
    started_at = validated["started_at"]
    last_tick_at = _required_instant(validated["last_tick_at"])
    tick_seconds = _positive_finite_seconds(validated["tick_seconds"])
    if stopped_at is not None:
        return "stopped", None
    assert isinstance(pid, int)
    if started_at is None:
        return "stale", pid
    age = (checked_at - last_tick_at).total_seconds()
    recent = 0 <= age <= max(tick_seconds * 3, 5.0)
    if _pid_is_alive(pid) and recent:
        return "running", pid
    return "stale", pid


def validate_heartbeat(heartbeat: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate the complete persisted heartbeat structure."""

    required = {
        "pid",
        "started_at",
        "last_tick_at",
        "last_success_at",
        "last_error",
        "tick_seconds",
        "stopped_at",
    }
    if not required.issubset(heartbeat):
        raise ValueError("incomplete schedule heartbeat")
    pid = heartbeat["pid"]
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= MAX_PID):
        raise ValueError("invalid heartbeat pid")
    started_at = _optional_instant(heartbeat["started_at"])
    last_tick_at = _required_instant(heartbeat["last_tick_at"])
    last_success_at = _optional_instant(heartbeat["last_success_at"])
    stopped_at = _optional_instant(heartbeat["stopped_at"])
    last_error = heartbeat["last_error"]
    if last_error is not None:
        _bounded_free_text(
            last_error,
            field="heartbeat.last_error",
            max_bytes=SCHEDULE_HEARTBEAT_MAX_BYTES,
        )
    tick_seconds = _positive_finite_seconds(heartbeat["tick_seconds"])
    _ = tick_seconds
    if stopped_at is not None:
        if pid is not None:
            raise ValueError("stopped heartbeat must not have a pid")
        if started_at is None:
            raise ValueError("stopped heartbeat requires a start time")
    elif pid is None:
        raise ValueError("active heartbeat requires a pid")
    elif started_at is None:
        return heartbeat
    assert started_at is not None
    if started_at > last_tick_at:
        raise ValueError("heartbeat tick precedes service start")
    if last_success_at is not None and not started_at <= last_success_at <= last_tick_at:
        raise ValueError("heartbeat success is outside the service lifetime")
    if stopped_at is not None and stopped_at < last_tick_at:
        raise ValueError("heartbeat stop precedes its last tick")
    return heartbeat


def safe_integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
        raise ValueError(f"invalid {field}")
    return value


def safe_nonnegative_int(value: Any, *, field: str) -> int:
    result = safe_integer(value, field=field)
    if result < 0:
        raise ValueError(f"invalid {field}")
    return result


def _validate_job(
    job: Mapping[str, Any],
    *,
    schedule_validator: Callable[[Mapping[str, Any]], None] | None,
) -> None:
    required = {
        "id",
        "name",
        "yaml_path",
        "schedule",
        "state",
        "created_at",
        "updated_at",
        "next_run_at",
        "last_run_at",
        "last_status",
        "run_count",
        "claim",
    }
    if not required.issubset(job):
        raise ValueError("incomplete schedule job")
    _canonical_identity(job["id"], field="job.id")
    _legacy_job_name(job["name"])
    _validate_yaml_path(job["yaml_path"])
    state = _canonical_enum(job["state"], field="job.state", values=_JOB_STATES)
    schedule = job["schedule"]
    if not isinstance(schedule, Mapping):
        raise ValueError("invalid job.schedule")
    if schedule.get("kind") == "interval" and type(schedule.get("seconds")) is not int:
        raise ValueError("interval schedule seconds must be an integer")
    if schedule_validator is not None:
        schedule_validator(schedule)
    _required_instant(job["created_at"])
    _required_instant(job["updated_at"])
    next_run_at = _optional_instant(job["next_run_at"])
    if state == "completed" and next_run_at is not None:
        raise ValueError("completed job cannot have a next run")
    last_run_at = _optional_instant(job["last_run_at"])
    last_status = job["last_status"]
    if last_status is not None:
        last_status = _canonical_enum(
            last_status,
            field="job.last_status",
            values=frozenset({"succeeded", "failed"}),
        )
    run_count = safe_nonnegative_int(job["run_count"], field="job.run_count")
    has_last_run = last_run_at is not None
    has_last_status = last_status is not None
    if run_count == 0 and (has_last_run or has_last_status):
        raise ValueError("unrun job cannot have a last run result")
    if run_count > 0 and not (has_last_run and has_last_status):
        raise ValueError("run job requires a complete last run result")
    claim = job["claim"]
    if claim is not None:
        if not isinstance(claim, Mapping):
            raise ValueError("invalid job.claim")
        required_claim = {"execution_id", "owner", "claimed_at", "expires_at"}
        if not required_claim.issubset(claim):
            raise ValueError("incomplete job.claim")
        _canonical_identity(
            claim["execution_id"],
            field="job.claim.execution_id",
        )
        _legacy_claim_owner(claim["owner"])
        claimed_at = _required_instant(claim["claimed_at"])
        expires_at = _required_instant(claim["expires_at"])
        if expires_at <= claimed_at:
            raise ValueError("job claim must expire after it was claimed")


def _validate_execution(execution: Mapping[str, Any]) -> None:
    required = {
        "id",
        "job_id",
        "job_name",
        "trigger",
        "scheduled_for",
        "status",
        "claimed_at",
        "started_at",
        "finished_at",
        "command",
        "pid",
        "exit_code",
        "stdout_path",
        "stderr_path",
        "error",
    }
    if not required.issubset(execution):
        raise ValueError("incomplete schedule execution")
    _canonical_identity(execution["id"], field="execution.id")
    _canonical_identity(execution["job_id"], field="execution.job_id")
    _legacy_execution_job_name(execution["job_name"])
    trigger = _canonical_enum(
        execution["trigger"],
        field="execution.trigger",
        values=_EXECUTION_TRIGGERS,
    )
    status = _canonical_enum(
        execution["status"],
        field="execution.status",
        values=_EXECUTION_STATES,
    )
    if "sequence" in execution:
        safe_nonnegative_int(execution["sequence"], field="execution.sequence")
    scheduled_for = _optional_instant(execution["scheduled_for"])
    if (trigger == "scheduled") != (scheduled_for is not None):
        raise ValueError("execution.scheduled_for does not match trigger")
    claimed_at = _required_instant(execution["claimed_at"])
    started_at = _optional_instant(execution["started_at"])
    finished_at = _optional_instant(execution["finished_at"])
    if started_at is not None and started_at < claimed_at:
        raise ValueError("execution.started_at precedes execution.claimed_at")
    if finished_at is not None and finished_at < (started_at or claimed_at):
        raise ValueError("execution.finished_at precedes execution start")
    command = execution["command"]
    if command is not None:
        if (
            not isinstance(command, list)
            or len(command) > EXECUTION_COMMAND_MAX_ITEMS
            or any(not isinstance(item, str) for item in command)
            or any(len(item.encode("utf-8")) > EXECUTION_COMMAND_ITEM_MAX_BYTES for item in command)
            or sum(len(item.encode("utf-8")) for item in command) > EXECUTION_COMMAND_MAX_BYTES
        ):
            raise ValueError("invalid execution.command")
    pid = execution["pid"]
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= MAX_PID):
        raise ValueError("invalid execution.pid")
    exit_code = execution["exit_code"]
    if exit_code is not None:
        safe_integer(exit_code, field="execution.exit_code")
    for field in ("stdout_path", "stderr_path"):
        value = execution[field]
        if value is not None:
            _required_text(value, field=f"execution.{field}")
    error = execution["error"]
    if error is not None:
        _bounded_free_text(
            error,
            field="execution.error",
            max_bytes=EXECUTION_ERROR_MAX_BYTES,
        )
    _validate_execution_lifecycle(
        execution,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
    )


def _validate_execution_lifecycle(
    execution: Mapping[str, Any],
    *,
    status: str,
    started_at: datetime | None,
    finished_at: datetime | None,
) -> None:
    command = execution["command"]
    pid = execution["pid"]
    exit_code = execution["exit_code"]
    stdout_path = execution["stdout_path"]
    stderr_path = execution["stderr_path"]
    error = execution["error"]

    if status == "claimed":
        if any(
            value is not None
            for value in (
                started_at,
                finished_at,
                command,
                pid,
                exit_code,
                stdout_path,
                stderr_path,
                error,
            )
        ):
            raise ValueError("claimed execution contains running or terminal fields")
        return

    if status == "running":
        if (
            started_at is None
            or not isinstance(command, list)
            or pid is None
            or stdout_path is None
            or stderr_path is None
            or any(value is not None for value in (finished_at, exit_code, error))
        ):
            raise ValueError("running execution fields are inconsistent")
        return

    if finished_at is None:
        raise ValueError("terminal execution requires execution.finished_at")
    if status == "succeeded":
        if exit_code != 0 or error is not None:
            raise ValueError("succeeded execution fields are inconsistent")
    elif status == "abandoned":
        if exit_code is not None or error is None:
            raise ValueError("abandoned execution fields are inconsistent")
    _validate_terminal_start_evidence(
        started_at=started_at,
        command=command,
        pid=pid,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


def _validate_terminal_start_evidence(
    *,
    started_at: datetime | None,
    command: Any,
    pid: Any,
    stdout_path: Any,
    stderr_path: Any,
) -> None:
    started = started_at is not None
    has_command = isinstance(command, list)
    has_pid = pid is not None
    if not any((started, has_command, has_pid)):
        # Version-1 writers allow terminal ledger rows without start evidence.
        # Keep those records readable until a versioned lifecycle migration
        # changes the durable contract.
        if command is not None:
            raise ValueError("terminal execution has invalid start command")
        return
    if any((started, has_command, has_pid)) and not all((started, has_command, has_pid)):
        raise ValueError("terminal execution has partial start evidence")
    if stdout_path is None or stderr_path is None:
        raise ValueError("started terminal execution requires log paths")


def _validate_document_relationships(
    jobs: list[Mapping[str, Any]],
    executions: list[Mapping[str, Any]],
) -> None:
    jobs_by_id = {str(job["id"]): job for job in jobs}
    executions_by_id = {str(execution["id"]): execution for execution in executions}
    claimed_execution_ids: set[str] = set()

    for job_id, job in jobs_by_id.items():
        claim = job["claim"]
        if claim is None:
            continue
        execution_id = str(claim["execution_id"])
        execution = executions_by_id.get(execution_id)
        if (
            execution is None
            or execution["job_id"] != job_id
            or execution["status"] not in {"claimed", "running"}
            or _required_instant(execution["claimed_at"]) != _required_instant(claim["claimed_at"])
            or execution_id in claimed_execution_ids
        ):
            raise ValueError("job claim does not own one active execution")
        if execution["trigger"] == "scheduled" and (
            job["state"] not in {"scheduled", "paused"} or execution["scheduled_for"] != job["next_run_at"]
        ):
            raise ValueError("scheduled execution does not match its job")
        claimed_execution_ids.add(execution_id)

    for execution_id, execution in executions_by_id.items():
        if execution["status"] in {"claimed", "running"}:
            job = jobs_by_id.get(str(execution["job_id"]))
            if (
                job is None
                or not isinstance(job["claim"], Mapping)
                or job["claim"]["execution_id"] != execution_id
                or execution_id not in claimed_execution_ids
            ):
                raise ValueError("active execution does not own a job claim")


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {field}")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > MAX_TEXT_CHARS
        or any(character in normalized for character in ("\x00", "\n", "\r"))
    ):
        raise ValueError(f"invalid {field}")
    return normalized


def _canonical_enum(
    value: Any,
    *,
    field: str,
    values: frozenset[str],
) -> str:
    normalized = _required_text(value, field=field)
    if value != normalized or normalized not in values:
        raise ValueError(f"invalid {field}")
    return normalized


def _canonical_identity(value: Any, *, field: str) -> str:
    normalized = _required_text(value, field=field)
    if value != normalized:
        raise ValueError(f"invalid {field}")
    return normalized


def _legacy_job_name(value: Any) -> str:
    """Accept every non-empty name the original version-1 writer persisted."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid job.name")
    return value


def _legacy_claim_owner(value: Any) -> str:
    """Accept every owner string the original version-1 writer persisted."""

    if not isinstance(value, str):
        raise ValueError("invalid job.claim.owner")
    return value


def _legacy_execution_job_name(value: Any) -> str:
    """Accept the bounded control characters written by version-1 stores."""

    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > JOB_NAME_MAX_BYTES:
        raise ValueError("invalid execution.job_name")
    return value


def _bounded_text(value: Any, *, field: str, max_bytes: int) -> str:
    normalized = _required_text(value, field=field)
    if len(normalized.encode("utf-8")) > max_bytes:
        raise ValueError(f"invalid {field}")
    return normalized


def _bounded_free_text(value: Any, *, field: str, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {field}")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"invalid {field}")
    return value


def _validate_yaml_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid job.yaml_path")
    normalized = value
    path = Path(normalized)
    if not path.is_absolute() and any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("invalid job.yaml_path")
    return normalized


def _unique_json_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number: {value}")
    return parsed


def _parse_safe_json_int(value: str) -> int:
    return safe_integer(int(value), field="JSON integer")


def _normalized_instant(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        return _required_instant(value).isoformat()
    except ValueError:
        return ""


def _required_instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid schedule timestamp")
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError("invalid schedule timestamp") from exc


def _optional_instant(value: Any) -> datetime | None:
    if value is None:
        return None
    return _required_instant(value)


def _positive_finite_seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid heartbeat interval")
    try:
        normalized = float(value)
    except OverflowError as exc:
        raise ValueError("invalid heartbeat interval") from exc
    if not math.isfinite(normalized) or normalized <= 0 or normalized > MAX_HEARTBEAT_TICK_SECONDS:
        raise ValueError("invalid heartbeat interval")
    return normalized


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, OverflowError, ValueError):
        return False
