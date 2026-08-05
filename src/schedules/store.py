"""Atomic JSON schedule store with cross-process claims."""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from src.lib.runtime import SecureDirectory

from .schedule import next_run, parse_datetime, validate_schedule

Document = dict[str, Any]

DEFAULT_EXECUTION_RETENTION_GLOBAL = 512
DEFAULT_EXECUTION_RETENTION_PER_JOB = 64
EXECUTION_COMMAND_MAX_ITEMS = 32
EXECUTION_COMMAND_MAX_BYTES = 4 * 1024
EXECUTION_COMMAND_ITEM_MAX_BYTES = 1024
EXECUTION_ERROR_MAX_BYTES = 4 * 1024
EXECUTION_JOB_NAME_MAX_BYTES = 512
EXECUTION_GOAL_TEXT_MAX_BYTES = 4 * 1024


class ScheduleStoreError(RuntimeError):
    """Base error for a durable schedule store operation."""


class JobNotFoundError(ScheduleStoreError):
    pass


class JobBusyError(ScheduleStoreError):
    pass


class ClaimLostError(ScheduleStoreError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime:
    value = value or _utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None = None) -> str:
    return _as_utc(value).isoformat()


def _parse(value: str) -> datetime:
    return parse_datetime(value, timezone="UTC")


class ScheduleStore:
    """Project-local jobs and executions guarded by an advisory ``flock``."""

    VERSION = 1
    EXECUTION_RETENTION_GLOBAL = DEFAULT_EXECUTION_RETENTION_GLOBAL
    EXECUTION_RETENTION_PER_JOB = DEFAULT_EXECUTION_RETENTION_PER_JOB
    EXECUTION_COMMAND_MAX_ITEMS = EXECUTION_COMMAND_MAX_ITEMS
    EXECUTION_COMMAND_MAX_BYTES = EXECUTION_COMMAND_MAX_BYTES
    EXECUTION_ERROR_MAX_BYTES = EXECUTION_ERROR_MAX_BYTES
    EXECUTION_JOB_NAME_MAX_BYTES = EXECUTION_JOB_NAME_MAX_BYTES

    def __init__(
        self,
        project_root: str | Path,
        *,
        claim_lease_seconds: float = 300.0,
        execution_retention_global: int = DEFAULT_EXECUTION_RETENTION_GLOBAL,
        execution_retention_per_job: int = DEFAULT_EXECUTION_RETENTION_PER_JOB,
    ):
        self.project_root = Path(project_root).expanduser().resolve()
        self.schedules_dir = self.project_root / ".agentloom" / "schedules"
        self.jobs_path = self.schedules_dir / "jobs.json"
        self.lock_path = self.schedules_dir / "jobs.lock"
        self.executions_dir = self.schedules_dir / "executions"
        self.claim_lease_seconds = max(float(claim_lease_seconds), 1.0)
        self.execution_retention_global = max(int(execution_retention_global), 1)
        self.execution_retention_per_job = max(int(execution_retention_per_job), 1)
        project_storage = SecureDirectory(self.project_root, create=True)
        try:
            self._storage = project_storage.child(".agentloom/schedules", create=True)
        finally:
            project_storage.close()
        try:
            # Keep both directory inodes open for the store lifetime. A later
            # rename/symlink swap of any pathname component cannot redirect
            # jobs, heartbeat, locks, or process output outside this anchor.
            self._executions_storage = self._storage.child("executions", create=True)
        except BaseException:
            self._storage.close()
            raise

    @staticmethod
    def _empty() -> Document:
        return {"version": ScheduleStore.VERSION, "jobs": [], "executions": []}

    def _ensure_dir(self) -> None:
        if self._storage.closed:
            raise RuntimeError(f"schedule storage is closed: {self.schedules_dir}")

    @contextmanager
    def file_lock(
        self,
        relative: str | Path,
        *,
        exclusive: bool,
        blocking: bool = True,
    ) -> Iterator[None]:
        """Lock one anchored regular file without following symlinks."""

        self._ensure_dir()
        with self._storage.advisory_file_lock(
            relative,
            create=True,
            exclusive=exclusive,
            blocking=blocking,
        ):
            yield

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        with self.file_lock("jobs.lock", exclusive=exclusive):
            yield

    def _read_unlocked(self) -> Document:
        try:
            payload = self._storage.read_json("jobs.json")
        except FileNotFoundError:
            return self._empty()
        except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScheduleStoreError(f"Cannot read schedule store {self.jobs_path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("version") != self.VERSION:
            raise ScheduleStoreError(f"Unsupported schedule store format: {self.jobs_path}")
        if not isinstance(payload.get("jobs"), list) or not isinstance(payload.get("executions"), list):
            raise ScheduleStoreError(f"Invalid schedule store document: {self.jobs_path}")
        return payload

    def _write_unlocked(self, payload: Document) -> None:
        self._ensure_dir()
        removed_executions = self._prune_executions(payload)
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        self._storage.atomic_write_text("jobs.json", serialized + "\n")
        self._cleanup_execution_logs(removed_executions)

    @staticmethod
    def _bounded_text(value: Any, max_bytes: int) -> str:
        encoded = str(value).encode("utf-8", errors="replace")
        if len(encoded) <= max_bytes:
            return str(value)
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    @classmethod
    def _bounded_command(cls, command: list[str]) -> list[str]:
        bounded: list[str] = []
        remaining = EXECUTION_COMMAND_MAX_BYTES
        for index, raw_item in enumerate(command):
            if index >= EXECUTION_COMMAND_MAX_ITEMS or remaining <= 0:
                break
            item = cls._bounded_text(
                raw_item,
                min(EXECUTION_COMMAND_ITEM_MAX_BYTES, remaining),
            )
            remaining -= len(item.encode("utf-8"))
            bounded.append(item)
        return bounded

    @staticmethod
    def _execution_sequence(execution: dict[str, Any]) -> int:
        sequence = execution.get("sequence")
        if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 0:
            return sequence
        return -1

    @classmethod
    def _execution_rank(cls, execution: dict[str, Any]) -> tuple[int, str, str]:
        observed_at = execution.get("finished_at") or execution.get("started_at") or execution.get("claimed_at") or ""
        return (
            cls._execution_sequence(execution),
            str(observed_at),
            str(execution.get("id") or ""),
        )

    def _next_execution_sequence(self, payload: Document) -> int:
        return (
            max(
                (
                    self._execution_sequence(execution)
                    for execution in payload["executions"]
                    if isinstance(execution, dict)
                ),
                default=0,
            )
            + 1
        )

    def _prune_executions(self, payload: Document) -> list[dict[str, Any]]:
        executions = payload["executions"]
        claim_execution_ids = {
            str(claim["execution_id"])
            for job in payload["jobs"]
            if isinstance(job, dict)
            for claim in [job.get("claim")]
            if isinstance(claim, dict) and claim.get("execution_id")
        }
        active_indexes: set[int] = set()
        terminal_by_job: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for index, execution in enumerate(executions):
            if not isinstance(execution, dict):
                continue
            execution_id = str(execution.get("id") or "")
            if execution_id in claim_execution_ids or execution.get("status") in {
                "claimed",
                "running",
            }:
                active_indexes.add(index)
                continue
            terminal_by_job.setdefault(str(execution.get("job_id") or ""), []).append((index, execution))

        per_job_indexes: set[int] = set()
        for candidates in terminal_by_job.values():
            newest = sorted(
                candidates,
                key=lambda item: self._execution_rank(item[1]),
                reverse=True,
            )[: self.execution_retention_per_job]
            per_job_indexes.update(index for index, _ in newest)

        retained_terminal_indexes = {
            index
            for index, _ in sorted(
                ((index, executions[index]) for index in per_job_indexes if isinstance(executions[index], dict)),
                key=lambda item: self._execution_rank(item[1]),
                reverse=True,
            )[: self.execution_retention_global]
        }
        retained_indexes = active_indexes | retained_terminal_indexes
        removed = [
            execution
            for index, execution in enumerate(executions)
            if index not in retained_indexes and isinstance(execution, dict)
        ]
        payload["executions"] = [execution for index, execution in enumerate(executions) if index in retained_indexes]
        return removed

    def _cleanup_execution_logs(self, executions: list[dict[str, Any]]) -> None:
        for execution in executions:
            execution_id = str(execution.get("id") or "")
            if not execution_id or Path(execution_id).name != execution_id or execution_id in {".", ".."}:
                continue
            for suffix in ("stdout.log", "stderr.log"):
                name = f"{execution_id}.{suffix}"
                try:
                    self._executions_storage.stat_file(name)
                    self._executions_storage.unlink(name)
                except FileNotFoundError:
                    pass
                except (OSError, RuntimeError, ValueError):
                    # Retention is already durable. Never follow or fail a
                    # schedule mutation because a log was swapped or malformed.
                    continue

    def read_state_json(self, relative: str | Path) -> Any:
        """Read schedule-owned state through the pinned directory inode."""

        self._ensure_dir()
        return self._storage.read_json(relative)

    def write_state_json(self, relative: str | Path, payload: Any) -> None:
        """Atomically write schedule-owned state without pathname traversal."""

        self._ensure_dir()
        serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        self._storage.atomic_write_text(relative, serialized + "\n")

    @staticmethod
    def _execution_log_paths(execution_id: str) -> tuple[str, str]:
        if not execution_id or Path(execution_id).name != execution_id or execution_id in {".", ".."}:
            raise ValueError(f"unsafe schedule execution id: {execution_id}")
        prefix = f".agentloom/schedules/executions/{execution_id}"
        return f"{prefix}.stdout.log", f"{prefix}.stderr.log"

    @contextmanager
    def open_execution_logs(self, execution_id: str) -> Iterator[tuple[BinaryIO, BinaryIO]]:
        """Create one execution's output files under the pinned log directory."""

        self._execution_log_paths(execution_id)
        stdout_name = f"{execution_id}.stdout.log"
        stderr_name = f"{execution_id}.stderr.log"
        with self._executions_storage.open_binary_writer(
            stdout_name,
            exclusive=True,
        ) as stdout_handle:
            with self._executions_storage.open_binary_writer(
                stderr_name,
                exclusive=True,
            ) as stderr_handle:
                yield stdout_handle, stderr_handle

    def read_execution_stdout(self, execution_id: str, *, max_bytes: int = 256 * 1024) -> str:
        """Read the bounded tail of one execution's stdout from anchored storage."""

        self._execution_log_paths(execution_id)
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        stdout_name = f"{execution_id}.stdout.log"
        with self._executions_storage.open_binary_reader(stdout_name) as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            payload = stream.read(max_bytes)
        return payload.decode("utf-8", errors="replace")

    def close(self) -> None:
        self._executions_storage.close()
        self._storage.close()

    def __enter__(self) -> ScheduleStore:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def _find_job(payload: Document, job_id: str) -> dict[str, Any]:
        for job in payload["jobs"]:
            if job.get("id") == job_id:
                return job
        raise JobNotFoundError(f"Unknown schedule job: {job_id}")

    @staticmethod
    def _find_execution(payload: Document, execution_id: str) -> dict[str, Any]:
        for execution in payload["executions"]:
            if execution.get("id") == execution_id:
                return execution
        raise ScheduleStoreError(f"Unknown schedule execution: {execution_id}")

    def snapshot(self) -> Document:
        with self._locked(exclusive=False):
            return copy.deepcopy(self._read_unlocked())

    def list_jobs(self) -> list[dict[str, Any]]:
        jobs = self.snapshot()["jobs"]
        return sorted(jobs, key=lambda job: (str(job.get("next_run_at") or "~"), str(job.get("name") or "")))

    def list_executions(self, *, job_id: str | None = None) -> list[dict[str, Any]]:
        executions = [item for item in self.snapshot()["executions"] if isinstance(item, dict)]
        if job_id is not None:
            executions = [item for item in executions if item.get("job_id") == job_id]
        return sorted(executions, key=self._execution_rank, reverse=True)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._locked(exclusive=False):
            return copy.deepcopy(self._find_job(self._read_unlocked(), job_id))

    def get_execution(self, execution_id: str) -> dict[str, Any]:
        with self._locked(exclusive=False):
            return copy.deepcopy(self._find_execution(self._read_unlocked(), execution_id))

    def _stored_yaml_path(self, yaml_path: str | Path) -> str:
        resolved = Path(yaml_path).expanduser()
        if not resolved.is_absolute():
            resolved = self.project_root / resolved
        resolved = resolved.resolve()
        if not resolved.is_file():
            raise ValueError(f"Agent YAML does not exist: {resolved}")
        try:
            return resolved.relative_to(self.project_root).as_posix()
        except ValueError:
            return str(resolved)

    def add_job(
        self,
        *,
        name: str,
        yaml_path: str | Path,
        schedule: dict[str, Any],
        now: datetime | None = None,
        validate_before_commit: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        normalized = validate_schedule(schedule)
        created = _as_utc(now)
        first = next_run(normalized, after=created)
        with self._locked(exclusive=True):
            job = {
                "id": f"job_{uuid.uuid4().hex[:12]}",
                "name": str(name).strip() or Path(yaml_path).stem,
                "yaml_path": self._stored_yaml_path(yaml_path),
                "schedule": normalized,
                "state": "scheduled",
                "created_at": created.isoformat(),
                "updated_at": created.isoformat(),
                "next_run_at": first.isoformat() if first else None,
                "last_run_at": None,
                "last_status": None,
                "run_count": 0,
                "claim": None,
            }
            if validate_before_commit is not None:
                # The callback runs after the target's canonical stored path is
                # known and before the job is visible. It must not call back
                # into this ScheduleStore instance because the lock is not
                # reentrant.
                validate_before_commit(copy.deepcopy(job))
            payload = self._read_unlocked()
            payload["jobs"].append(job)
            self._write_unlocked(payload)
        return copy.deepcopy(job)

    def pause(self, job_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        with self._locked(exclusive=True):
            payload = self._read_unlocked()
            job = self._find_job(payload, job_id)
            job["state"] = "paused"
            job["updated_at"] = _iso(now)
            self._write_unlocked(payload)
            return copy.deepcopy(job)

    def resume(self, job_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        resumed_at = _as_utc(now)
        with self._locked(exclusive=True):
            payload = self._read_unlocked()
            job = self._find_job(payload, job_id)
            self._expire_stale_claim(payload, job, resumed_at)
            if self._claim_is_live(job, resumed_at):
                raise JobBusyError(f"Schedule job is running: {job_id}")
            candidate = next_run(job["schedule"], after=resumed_at)
            job["state"] = "scheduled"
            job["next_run_at"] = candidate.isoformat() if candidate else None
            job["updated_at"] = resumed_at.isoformat()
            self._write_unlocked(payload)
            return copy.deepcopy(job)

    def remove(self, job_id: str) -> dict[str, Any]:
        with self._locked(exclusive=True):
            payload = self._read_unlocked()
            job = self._find_job(payload, job_id)
            now = _utc_now()
            self._expire_stale_claim(payload, job, now)
            if self._claim_is_live(job, now):
                raise JobBusyError(f"Schedule job is running: {job_id}")
            payload["jobs"] = [item for item in payload["jobs"] if item.get("id") != job_id]
            self._write_unlocked(payload)
            return copy.deepcopy(job)

    @staticmethod
    def _claim_is_live(job: dict[str, Any], now: datetime) -> bool:
        claim = job.get("claim")
        if not isinstance(claim, dict) or not claim.get("expires_at"):
            return False
        try:
            return _parse(str(claim["expires_at"])) > now
        except ValueError:
            return False

    def _expire_stale_claim(self, payload: Document, job: dict[str, Any], now: datetime) -> None:
        claim = job.get("claim")
        if not isinstance(claim, dict) or self._claim_is_live(job, now):
            return
        execution_id = claim.get("execution_id")
        if execution_id:
            try:
                execution = self._find_execution(payload, str(execution_id))
            except ScheduleStoreError:
                execution = None
            if execution is not None and execution.get("status") in {"claimed", "running"}:
                execution["status"] = "abandoned"
                execution["finished_at"] = now.isoformat()
                execution["error"] = "execution claim expired before completion"
        job["claim"] = None

    def _claim_job(
        self,
        payload: Document,
        job: dict[str, Any],
        *,
        now: datetime,
        owner: str,
        trigger: str,
    ) -> dict[str, Any]:
        self._expire_stale_claim(payload, job, now)
        if self._claim_is_live(job, now):
            raise JobBusyError(f"Schedule job is already running: {job['id']}")
        execution_id = f"exec_{uuid.uuid4().hex}"
        expires_at = now + timedelta(seconds=self.claim_lease_seconds)
        scheduled_for = job.get("next_run_at") if trigger == "scheduled" else None
        execution = {
            "id": execution_id,
            "sequence": self._next_execution_sequence(payload),
            "job_id": job["id"],
            "job_name": self._bounded_text(
                job["name"],
                EXECUTION_JOB_NAME_MAX_BYTES,
            ),
            "trigger": trigger,
            "scheduled_for": scheduled_for,
            "status": "claimed",
            "claimed_at": now.isoformat(),
            "started_at": None,
            "finished_at": None,
            "command": None,
            "pid": None,
            "exit_code": None,
            "stdout_path": None,
            "stderr_path": None,
            "error": None,
        }
        payload["executions"].append(execution)
        job["claim"] = {
            "execution_id": execution_id,
            "owner": owner,
            "claimed_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        job["updated_at"] = now.isoformat()
        return {"job": copy.deepcopy(job), "execution": copy.deepcopy(execution)}

    def claim_due(
        self,
        *,
        now: datetime | None = None,
        owner: str,
        limit: int | None = 1,
    ) -> list[dict[str, Any]]:
        if limit is not None and limit <= 0:
            return []
        claimed_at = _as_utc(now)
        claims: list[dict[str, Any]] = []
        with self._locked(exclusive=True):
            payload = self._read_unlocked()
            ordered = sorted(payload["jobs"], key=lambda job: str(job.get("next_run_at") or "~"))
            for job in ordered:
                self._expire_stale_claim(payload, job, claimed_at)
                if job.get("state") != "scheduled" or not job.get("next_run_at"):
                    continue
                if _parse(str(job["next_run_at"])) > claimed_at or self._claim_is_live(job, claimed_at):
                    continue
                claims.append(self._claim_job(payload, job, now=claimed_at, owner=owner, trigger="scheduled"))
                if limit is not None and len(claims) >= max(0, limit):
                    break
            if claims:
                self._write_unlocked(payload)
        return claims

    def claim_now(
        self,
        job_id: str,
        *,
        owner: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        claimed_at = _as_utc(now)
        with self._locked(exclusive=True):
            payload = self._read_unlocked()
            job = self._find_job(payload, job_id)
            claim = self._claim_job(payload, job, now=claimed_at, owner=owner, trigger="manual")
            self._write_unlocked(payload)
            return claim

    def mark_running(
        self,
        execution_id: str,
        *,
        command: list[str],
        pid: int,
        stdout_path: str,
        stderr_path: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        started_at = _as_utc(now)
        expected_stdout, expected_stderr = self._execution_log_paths(execution_id)
        if (stdout_path, stderr_path) != (expected_stdout, expected_stderr):
            raise ValueError("stdout_path and stderr_path must be canonical execution log paths")
        with self._locked(exclusive=True):
            payload = self._read_unlocked()
            execution = self._find_execution(payload, execution_id)
            job = self._find_job(payload, str(execution["job_id"]))
            if not isinstance(job.get("claim"), dict) or job["claim"].get("execution_id") != execution_id:
                raise ClaimLostError(f"Execution no longer owns its job claim: {execution_id}")
            execution.update(
                {
                    "status": "running",
                    "started_at": started_at.isoformat(),
                    "command": self._bounded_command(command),
                    "pid": int(pid),
                    "stdout_path": expected_stdout,
                    "stderr_path": expected_stderr,
                }
            )
            self._write_unlocked(payload)
            return copy.deepcopy(execution)

    def heartbeat_claim(self, execution_id: str, *, now: datetime | None = None) -> bool:
        heartbeat_at = _as_utc(now)
        with self._locked(exclusive=True):
            payload = self._read_unlocked()
            execution = self._find_execution(payload, execution_id)
            job = self._find_job(payload, str(execution["job_id"]))
            claim = job.get("claim")
            if not isinstance(claim, dict) or claim.get("execution_id") != execution_id:
                return False
            claim["expires_at"] = (heartbeat_at + timedelta(seconds=self.claim_lease_seconds)).isoformat()
            self._write_unlocked(payload)
            return True

    def finish_execution(
        self,
        execution_id: str,
        *,
        exit_code: int | None,
        stdout_path: str,
        stderr_path: str,
        error: str | None = None,
        terminal_status: str | None = None,
        goal: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        finished_at = _as_utc(now)
        expected_stdout, expected_stderr = self._execution_log_paths(execution_id)
        if (stdout_path, stderr_path) != (expected_stdout, expected_stderr):
            raise ValueError("stdout_path and stderr_path must be canonical execution log paths")
        if terminal_status not in {None, "budget_limited"}:
            raise ValueError(f"unsupported schedule terminal status: {terminal_status}")
        bounded_goal: dict[str, Any] | None = None
        if goal is not None:
            if terminal_status != "budget_limited" or goal.get("status") != "budget_limited":
                raise ValueError("schedule Goal diagnostics require budget_limited status")
            bounded_goal = {
                key: (
                    self._bounded_text(value, EXECUTION_GOAL_TEXT_MAX_BYTES)
                    if key in {"objective", "evidence"} and value is not None
                    else value
                )
                for key, value in goal.items()
                if key
                in {
                    "schema_version",
                    "goal_id",
                    "status",
                    "objective",
                    "token_budget",
                    "prompt_tokens",
                    "completion_tokens",
                    "used_tokens",
                    "remaining_tokens",
                    "evidence",
                    "created_at",
                    "updated_at",
                    "completed_at",
                }
            }
        with self._locked(exclusive=True):
            payload = self._read_unlocked()
            execution = self._find_execution(payload, execution_id)
            job = self._find_job(payload, str(execution["job_id"]))
            claim = job.get("claim")
            if not isinstance(claim, dict) or claim.get("execution_id") != execution_id:
                raise ClaimLostError(f"Execution no longer owns its job claim: {execution_id}")
            if exit_code not in (None, 0) and error is None:
                error = f"process exited with status {exit_code}"
            if error is not None:
                error = self._bounded_text(error, EXECUTION_ERROR_MAX_BYTES)
            success = exit_code == 0 and error is None
            status = terminal_status or ("succeeded" if success else "failed")
            execution.update(
                {
                    "status": status,
                    "finished_at": finished_at.isoformat(),
                    "exit_code": exit_code,
                    "stdout_path": expected_stdout,
                    "stderr_path": expected_stderr,
                    "error": error,
                }
            )
            if bounded_goal is not None:
                execution["goal"] = bounded_goal
            job["claim"] = None
            job["last_run_at"] = finished_at.isoformat()
            job["last_status"] = execution["status"]
            job["run_count"] = int(job.get("run_count") or 0) + 1
            job["updated_at"] = finished_at.isoformat()
            if execution.get("trigger") == "scheduled":
                previous = _parse(str(execution["scheduled_for"]))
                candidate = next_run(job["schedule"], after=finished_at, previous=previous)
                job["next_run_at"] = candidate.isoformat() if candidate else None
                if candidate is None:
                    job["state"] = "completed"
            self._write_unlocked(payload)
            return copy.deepcopy(execution)
