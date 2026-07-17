"""
Checkpoint persistence manager.

Manages one canonical task checkpoint tree under::

    {runtime.root_dir}/checkpoints/{application_id}/{task_id}/
        task_events.jsonl
        task_tree.json
        heartbeat.json
        checkpoint.json                          # supervisor
        workers/{worker_name}/calls/{call_index}/checkpoint.json

The directory is task-scoped and deliberately independent from run logs.
Each resume attempt writes a new run directory while continuing to use this
same task directory.  Lookup is therefore a direct path operation: there is
no task index, log-directory scan, or legacy runtime fallback.

All writes are **atomic**: data is flushed to a ``.tmp`` file first and
then renamed, so a crash mid-write never corrupts the checkpoint.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.lib.checkpoint.serializer import CheckpointSerializer
from src.lib.heartbeat.status import (
    detect_crashed_status as _detect_crashed_status,
)
from src.lib.heartbeat.status import (
    detect_worker_call_crashed as _detect_worker_call_crashed,
)
from src.lib.logging import get_logger
from src.lib.runtime import SecureDirectory, portable_runtime_component

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WorkerCallPreparation:
    """Atomic decision for one worker invocation in the current attempt."""

    call_index: int
    should_execute: bool
    cached_result: Any = None


def _require_safe_path_component(value: Any, *, field: str) -> str:
    """Validate an identifier before it participates in a filesystem path."""
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError(f"{field} must be one safe path component")
    return value


def _require_call_index(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("call_index must be a non-negative integer")
    return value


class CheckpointTaskLease:
    """Exclusive process lease for one logical task checkpoint directory."""

    def __init__(self, task_dir: Path, *, require_exists: bool = False) -> None:
        self._task_dir = task_dir
        self._require_exists = require_exists
        self._fd: int | None = None

    def acquire(self) -> CheckpointTaskLease:
        if self._fd is not None:
            return self
        if self._task_dir.is_symlink():
            raise RuntimeError(f"checkpoint task directory is a symlink: {self._task_dir}")
        if self._require_exists:
            if not self._task_dir.is_dir():
                raise FileNotFoundError(f"checkpoint task directory does not exist: {self._task_dir}")
        else:
            self._task_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(
                self._task_dir,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            # A concurrent cleaner may win between the existence check and
            # open.  Resume must fail without recreating an empty checkpoint.
            raise
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            os.close(fd)
            raise RuntimeError(f"checkpoint task is already active: {self._task_dir.name}") from exc
        self._fd = fd
        return self

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> CheckpointTaskLease:
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


# =========================================================================
# Task-tree migration helpers
# =========================================================================


def _migrate_task_tree_workers(tree: dict) -> dict:
    """Auto-upgrade workers from v1 (single dict) to v2 (list of calls).

    v1 format::

        "workers": {"w1": {"status": "completed", ...}}

    v2 format::

        "workers": {"w1": [{"call_index": 0, "status": "completed", ...}]}
    """
    workers = tree.get("workers")
    if not isinstance(workers, dict):
        return tree
    for name, entry in list(workers.items()):
        if isinstance(entry, dict):
            # v1 → v2: wrap single dict in a list
            entry.setdefault("call_index", 0)
            entry.setdefault("input_hash", "")
            workers[name] = [entry]
    return tree


def _jsonable(data: Any) -> Any:
    """Return JSON-roundtrippable data using the same coercion as disk writes."""
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))


def _coerce_call_index(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _find_worker_call(calls: list[dict], call_index: int) -> dict | None:
    for call in calls:
        if _coerce_call_index(call.get("call_index")) == call_index:
            return call
    return None


def _sorted_worker_calls(calls: list[dict]) -> list[dict]:
    return sorted(calls, key=lambda c: _coerce_call_index(c.get("call_index")))


def _apply_task_event(tree: dict | None, event: dict, fallback_task_id: str = "") -> dict:
    """Apply one checkpoint event to a task-tree projection."""
    event_type = event.get("type")
    timestamp = event.get("timestamp", "")

    if event_type == "task_tree_replaced":
        replaced = event.get("tree") or {}
        if not isinstance(replaced, dict):
            replaced = {}
        if isinstance(tree, dict):
            for field in (
                "task_id",
                "yaml_path",
                "agent_name",
                "task_text",
                "created_at",
                "run_id",
                "last_run_at",
                "runs",
            ):
                if field not in replaced and field in tree:
                    replaced[field] = tree[field]
        return _migrate_task_tree_workers(_jsonable(replaced))

    if tree is None:
        tree = {
            "task_id": event.get("task_id", fallback_task_id),
            "agent_name": event.get("supervisor_name", ""),
            "status": "running",
            "created_at": timestamp,
            "workers": {},
        }

    workers = tree.setdefault("workers", {})

    if event_type == "task_created":
        tree.update(
            {
                "task_id": event.get("task_id", fallback_task_id),
                "yaml_path": event.get("yaml_path", tree.get("yaml_path", "")),
                "agent_name": event.get("agent_name", tree.get("agent_name", "")),
                "task_text": event.get("task_text", tree.get("task_text", "")),
                "status": event.get("status", tree.get("status", "running")),
                "created_at": event.get("created_at", timestamp),
            }
        )
        tree.setdefault("workers", workers)

    elif event_type == "task_status_changed":
        status = event.get("status")
        if status:
            tree["status"] = status
        if event.get("result") is not None:
            tree["result"] = event.get("result")
        if event.get("error") is not None:
            tree["error"] = event.get("error")
        if status == "interrupted":
            tree["interrupted_at"] = event.get("interrupted_at", timestamp)

    elif event_type in {"run_started", "run_resumed"}:
        run_id = event.get("run_id")
        if run_id:
            tree["run_id"] = run_id
            tree["last_run_at"] = timestamp
            attempts = tree.setdefault("runs", [])
            if not isinstance(attempts, list):
                attempts = []
                tree["runs"] = attempts
            attempts.append(
                {
                    "run_id": run_id,
                    "event": event_type,
                    "started_at": timestamp,
                }
            )
        tree["status"] = "running"

    elif event_type == "worker_call_started":
        worker_name = event.get("agent_name") or event.get("worker_name")
        if worker_name:
            calls = workers.setdefault(worker_name, [])
            if not isinstance(calls, list):
                calls = [calls]
                workers[worker_name] = calls
            call_index = _coerce_call_index(event.get("call_index"), len(calls))
            call = _find_worker_call(calls, call_index)
            if call is None:
                call = {"call_index": call_index}
                calls.append(call)
            call.update(
                {
                    "call_index": call_index,
                    "input_hash": event.get("input_hash", call.get("input_hash", "")),
                    "agent_name": worker_name,
                    "status": "running",
                    "task_input": event.get("task_input", call.get("task_input", "")),
                    "result": None,
                    "started_at": event.get("started_at", timestamp),
                }
            )
            if event.get("run_id"):
                call["attempt_run_id"] = event["run_id"]
            workers[worker_name] = _sorted_worker_calls(calls)

    elif event_type == "worker_call_resume_claimed":
        worker_name = event.get("agent_name") or event.get("worker_name")
        if worker_name:
            calls = workers.setdefault(worker_name, [])
            if not isinstance(calls, list):
                calls = [calls]
                workers[worker_name] = calls
            call_index = _coerce_call_index(event.get("call_index"), len(calls))
            call = _find_worker_call(calls, call_index)
            if call is None:
                call = {"call_index": call_index, "agent_name": worker_name}
                calls.append(call)
            call.update(
                {
                    "input_hash": event.get("input_hash", call.get("input_hash", "")),
                    "agent_name": worker_name,
                    "status": "running",
                    "task_input": event.get("task_input", call.get("task_input", "")),
                    "result": None,
                    "attempt_run_id": event.get("run_id", ""),
                    "resume_claimed_at": event.get("claimed_at", timestamp),
                }
            )
            workers[worker_name] = _sorted_worker_calls(calls)

    elif event_type == "worker_call_cached_result_claimed":
        worker_name = event.get("agent_name") or event.get("worker_name")
        if worker_name:
            calls = workers.setdefault(worker_name, [])
            if not isinstance(calls, list):
                calls = [calls]
                workers[worker_name] = calls
            call_index = _coerce_call_index(event.get("call_index"), len(calls))
            call = _find_worker_call(calls, call_index)
            if call is None:
                call = {"call_index": call_index, "agent_name": worker_name}
                calls.append(call)
            # Claiming a cached result is an execution-attempt cursor only.  It
            # must not change the original call's completed status or result.
            call["cached_claim_run_id"] = event.get("run_id", "")
            call["cached_claimed_at"] = event.get("claimed_at", timestamp)
            workers[worker_name] = _sorted_worker_calls(calls)

    elif event_type == "worker_call_finished":
        worker_name = event.get("agent_name") or event.get("worker_name")
        if worker_name:
            calls = workers.setdefault(worker_name, [])
            if not isinstance(calls, list):
                calls = [calls]
                workers[worker_name] = calls
            call_index = _coerce_call_index(event.get("call_index"), len(calls))
            call = _find_worker_call(calls, call_index)
            if call is None:
                call = {"call_index": call_index, "agent_name": worker_name}
                calls.append(call)
            call["status"] = event.get("status", call.get("status", "unknown"))
            call["finished_at"] = event.get("finished_at", timestamp)
            if event.get("input_hash") is not None:
                call["input_hash"] = event.get("input_hash")
            if event.get("task_input") is not None:
                call["task_input"] = event.get("task_input")
            if event.get("result") is not None:
                call["result"] = event.get("result")
            if event.get("error") is not None:
                call["error"] = event.get("error")
            workers[worker_name] = _sorted_worker_calls(calls)

    return _migrate_task_tree_workers(tree)


def _project_task_tree_from_events(events: list[dict], fallback_task_id: str = "") -> dict | None:
    if not events:
        return None
    tree: dict | None = None
    for event in events:
        tree = _apply_task_event(tree, event, fallback_task_id=fallback_task_id)
    if tree is None:
        return None
    tree.setdefault("task_id", fallback_task_id)
    tree.setdefault("workers", {})
    return _migrate_task_tree_workers(tree)


# =========================================================================
# CheckpointManager
# =========================================================================


class CheckpointManager:
    """Manage checkpoint files under a canonical runtime task path.

    Active execution should pass ``checkpoint_dir`` from
    :class:`src.lib.runtime.RuntimeContext`.  Management and tests may pass an
    application-level ``checkpoints_root`` and address its tasks by id.

    Checkpoint layout::

        {runtime.root_dir}/checkpoints/{application_id}/{task_id}/
            task_events.jsonl
            task_tree.json
            heartbeat.json
            checkpoint.json
            workers/{worker_name}/calls/{call_index}/checkpoint.json

    Args:
        supervisor_name: The YAML ``name`` field of the supervisor agent.
        checkpoint_dir: Canonical path for exactly one logical task.
        checkpoints_root: Canonical application checkpoint root.  This is
            intended for task enumeration and isolated tests.
        run_id: Current execution-attempt id, persisted in run events and
            exposed to worker heartbeat writers.
    """

    def __init__(
        self,
        supervisor_name: str,
        *,
        checkpoint_dir: Path | None = None,
        checkpoints_root: Path | None = None,
        run_id: str | None = None,
    ) -> None:
        if (checkpoint_dir is None) == (checkpoints_root is None):
            raise ValueError("pass exactly one of checkpoint_dir or checkpoints_root")
        self._supervisor_name = supervisor_name
        self._run_id = run_id
        if checkpoint_dir is not None:
            self._checkpoint_dir = Path(os.path.abspath(os.fspath(Path(checkpoint_dir).expanduser())))
            self._checkpoints_root = self._checkpoint_dir.parent
            self._bound_task_id: str | None = self._checkpoint_dir.name
        else:
            self._checkpoints_root = Path(os.path.abspath(os.fspath(Path(checkpoints_root).expanduser())))
            self._checkpoint_dir = None
            self._bound_task_id = None

        import threading

        self._tree_lock = threading.Lock()
        self._task_storages: dict[Path, SecureDirectory] = {}
        if self._checkpoint_dir is not None and self._checkpoint_dir.is_dir():
            self._task_storages[self._checkpoint_dir] = SecureDirectory(
                self._checkpoint_dir,
                create=False,
            )

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def checkpoint_dir(self) -> Path | None:
        return self._checkpoint_dir

    def task_lease(self, *, require_exists: bool = False) -> CheckpointTaskLease:
        """Return an exclusive lease for this manager's bound task."""
        if self._checkpoint_dir is None:
            raise RuntimeError("task_lease requires a manager bound to checkpoint_dir")
        return CheckpointTaskLease(
            self._checkpoint_dir,
            require_exists=require_exists,
        )

    # ── path helpers ─────────────────────────────────────────────────────

    def _task_dir(self, task_id: str) -> Path:
        """Return the direct canonical directory for ``task_id``."""
        task_id = _require_safe_path_component(task_id, field="task_id")
        if self._bound_task_id is not None:
            if task_id != self._bound_task_id:
                raise ValueError(f"manager is bound to task {self._bound_task_id}, got {task_id}")
            assert self._checkpoint_dir is not None
            return self._checkpoint_dir
        return self._checkpoints_root / task_id

    def _supervisor_ckpt(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "checkpoint.json"

    def worker_dir(self, task_id: str, worker_name: str) -> Path:
        """Return the canonical directory for one validated worker name."""
        worker_name = _require_safe_path_component(worker_name, field="worker_name")
        worker_component = portable_runtime_component(worker_name, fallback="worker")
        return self._task_dir(task_id) / "workers" / worker_component

    def _worker_call_ckpt(self, task_id: str, worker_name: str, call_index: int) -> Path:
        call_index = _require_call_index(call_index)
        return self.worker_dir(task_id, worker_name) / "calls" / str(call_index) / "checkpoint.json"

    def _task_tree_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "task_tree.json"

    def _task_events_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "task_events.jsonl"

    def _heartbeat_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "heartbeat.json"

    def context_store_dir(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "context_store"

    def file_history_dir(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "file-history"

    def task_storage(self, task_id: str) -> SecureDirectory:
        """Return an independently owned handle to the canonical task inode."""

        task_dir = self._task_dir(task_id)
        storage, _ = self._task_storage_for_path(
            task_dir / ".storage-anchor",
            create=True,
        )
        return storage.duplicate()

    def directory_storage(self, task_id: str, directory: Path) -> SecureDirectory:
        """Derive a stable child-directory handle from the task inode."""

        task_dir = self._task_dir(task_id)
        absolute = Path(os.path.abspath(os.fspath(directory)))
        try:
            relative = absolute.relative_to(task_dir)
        except ValueError as exc:
            raise RuntimeError(f"checkpoint directory escapes task: {absolute}") from exc
        if not relative.parts:
            return self.task_storage(task_id)
        task_storage = self.task_storage(task_id)
        try:
            return task_storage.child(relative, create=True)
        finally:
            task_storage.close()

    # ── atomic write ─────────────────────────────────────────────────────

    def _task_storage_for_path(
        self,
        path: Path,
        *,
        create: bool,
    ) -> tuple[SecureDirectory, Path]:
        absolute = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
        if self._checkpoint_dir is not None:
            task_root = self._checkpoint_dir
        else:
            try:
                relative = absolute.relative_to(self._checkpoints_root)
            except ValueError as exc:
                raise RuntimeError(f"checkpoint path escapes root: {absolute}") from exc
            if not relative.parts:
                raise RuntimeError(f"checkpoint path does not identify a task: {absolute}")
            task_root = self._checkpoints_root / relative.parts[0]
        try:
            relative_path = absolute.relative_to(task_root)
        except ValueError as exc:
            raise RuntimeError(f"checkpoint path escapes task: {absolute}") from exc
        if not relative_path.parts:
            raise RuntimeError("checkpoint operation requires a file below the task root")
        storage = self._task_storages.get(task_root)
        if storage is None or storage.closed:
            storage = SecureDirectory(task_root, create=create)
            self._task_storages[task_root] = storage
        return storage, relative_path

    def _write_json(self, path: Path, data: dict) -> None:
        storage, relative = self._task_storage_for_path(path, create=True)
        storage.atomic_write_json(relative, data)

    @staticmethod
    def _atomic_write(path: Path, data: dict) -> None:
        """Write JSON atomically through an anchored parent descriptor."""
        with SecureDirectory(path.parent, create=True) as storage:
            storage.atomic_write_json(path.name, data)

    def _read_json(self, path: Path) -> dict | None:
        """Read a JSON file, returning *None* when absent or corrupt."""
        try:
            storage, relative = self._task_storage_for_path(path, create=False)
            value = storage.read_json(relative)
            return value if isinstance(value, dict) else None
        except (
            FileNotFoundError,
            UnicodeError,
            json.JSONDecodeError,
            OSError,
            RuntimeError,
        ):
            return None

    def _read_task_events_from_path(self, path: Path) -> list[dict]:
        """Read append-only task events, skipping malformed crash-tail lines."""
        events: list[dict] = []
        try:
            storage, relative = self._task_storage_for_path(path, create=False)
            payload = storage.read_bytes(relative)
            for line_no, line in enumerate(payload.splitlines(), start=1):
                try:
                    raw = line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    _logger.warning(
                        "Skipping undecodable checkpoint event %s:%d",
                        path,
                        line_no,
                    )
                    continue
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    _logger.warning("Skipping malformed checkpoint event %s:%d", path, line_no)
                    continue
                if not isinstance(event, dict) or not event.get("type"):
                    _logger.warning("Skipping invalid checkpoint event %s:%d", path, line_no)
                    continue
                events.append(event)
        except (FileNotFoundError, OSError, RuntimeError) as exc:
            _logger.warning("Failed reading checkpoint events %s: %s", path, exc)
        return events

    def _append_task_event_unlocked(self, task_id: str, event: dict) -> None:
        """Append one event. Caller must hold ``_tree_lock``."""
        event = _jsonable(event)
        event.setdefault("timestamp", datetime.now().astimezone().isoformat())
        path = self._task_events_path(task_id)
        storage, relative = self._task_storage_for_path(path, create=True)
        storage.append_text(
            relative,
            json.dumps(event, ensure_ascii=False, default=str) + "\n",
            ensure_line_boundary=True,
        )

    def _load_task_tree_unlocked(self, task_id: str) -> dict | None:
        """Load task tree projection. Caller must hold ``_tree_lock``."""
        event_tree = _project_task_tree_from_events(
            self._read_task_events_from_path(self._task_events_path(task_id)),
            fallback_task_id=task_id,
        )
        if event_tree is not None:
            return event_tree

        tree = self._read_json(self._task_tree_path(task_id))
        if tree is not None:
            tree = _migrate_task_tree_workers(tree)
        return tree

    def _load_task_tree_from_dir(self, task_dir: Path) -> dict | None:
        event_tree = _project_task_tree_from_events(
            self._read_task_events_from_path(task_dir / "task_events.jsonl"),
            fallback_task_id=task_dir.name,
        )
        if event_tree is not None:
            return event_tree

        tree = self._read_json(task_dir / "task_tree.json")
        if tree is not None:
            tree = _migrate_task_tree_workers(tree)
        return tree

    def _write_task_tree_projection_unlocked(self, task_id: str, tree: dict) -> Path:
        """Persist the compatibility projection. Caller must hold ``_tree_lock``."""
        p = self._task_tree_path(task_id)
        self._write_json(p, _migrate_task_tree_workers(_jsonable(tree)))
        return p

    def _append_event_and_refresh_projection_unlocked(self, task_id: str, event: dict) -> dict:
        """Append an event and refresh ``task_tree.json`` from the event log."""
        self._append_task_event_unlocked(task_id, event)
        tree = (
            _project_task_tree_from_events(
                self._read_task_events_from_path(self._task_events_path(task_id)),
                fallback_task_id=task_id,
            )
            or {}
        )
        self._write_task_tree_projection_unlocked(task_id, tree)
        return tree

    # ── task tree ────────────────────────────────────────────────────────

    def save_task_tree(self, task_id: str, tree: dict) -> Path:
        """Persist the execution-tree metadata (thread-safe).

        New code treats ``task_events.jsonl`` as the source of truth and this
        method as a compatibility escape hatch: it appends a full replacement
        event, then writes the legacy ``task_tree.json`` projection.
        """
        with self._tree_lock:
            event = {
                "type": "task_tree_replaced",
                "tree": _migrate_task_tree_workers(_jsonable(tree)),
            }
            self._append_event_and_refresh_projection_unlocked(task_id, event)
            return self._task_tree_path(task_id)

    def load_task_tree(self, task_id: str) -> dict | None:
        with self._tree_lock:
            return self._load_task_tree_unlocked(task_id)

    def load_task_tree_projection(
        self,
        task_id: str,
        *,
        max_bytes: int,
    ) -> dict | None:
        """Read a bounded maintained projection without replaying events."""

        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        with self._tree_lock:
            try:
                storage, relative = self._task_storage_for_path(
                    self._task_tree_path(task_id),
                    create=False,
                )
                payload, truncated = storage.read_bytes_up_to(relative, max_bytes)
                if truncated:
                    return None
                tree = json.loads(payload.decode("utf-8"))
            except (
                FileNotFoundError,
                UnicodeError,
                json.JSONDecodeError,
                OSError,
                RuntimeError,
            ):
                return None
            return _migrate_task_tree_workers(tree) if isinstance(tree, dict) else None

    def load_task_events(self, task_id: str) -> list[dict]:
        """Return a stable copy of the append-only events for one task."""

        with self._tree_lock:
            return list(self._read_task_events_from_path(self._task_events_path(task_id)))

    def update_task_tree(self, task_id: str, updater) -> dict:
        """Atomically read-modify-write the task tree (thread-safe).

        Args:
            task_id: The task to update.
            updater: A callable ``(tree: dict) -> dict`` that receives
                the current tree (or ``{}``) and returns the modified
                tree to persist.

        Returns:
            The updated tree dict.
        """
        with self._tree_lock:
            tree = self._load_task_tree_unlocked(task_id) or {}
            updated = updater(tree)
            updated = _migrate_task_tree_workers(_jsonable(updated))
            self._append_event_and_refresh_projection_unlocked(
                task_id,
                {"type": "task_tree_replaced", "tree": updated},
            )
            return updated

    def record_task_created(
        self,
        task_id: str,
        *,
        yaml_path: str,
        agent_name: str,
        task_text: str,
        created_at: str,
    ) -> dict:
        """Append a task creation event and refresh the task-tree projection."""
        with self._tree_lock:
            return self._append_event_and_refresh_projection_unlocked(
                task_id,
                {
                    "type": "task_created",
                    "task_id": task_id,
                    "yaml_path": yaml_path,
                    "agent_name": agent_name,
                    "task_text": task_text,
                    "created_at": created_at,
                },
            )

    def _record_run_event(self, task_id: str, event_type: str) -> dict:
        if not self._run_id:
            raise ValueError("run_id is required to record checkpoint run events")
        with self._tree_lock:
            return self._append_event_and_refresh_projection_unlocked(
                task_id,
                {
                    "type": event_type,
                    "task_id": task_id,
                    "run_id": self._run_id,
                },
            )

    def record_run_started(self, task_id: str) -> dict:
        """Record the first execution attempt for a logical task."""
        return self._record_run_event(task_id, "run_started")

    def record_run_resumed(self, task_id: str) -> dict:
        """Record a new execution attempt reusing an existing task."""
        return self._record_run_event(task_id, "run_resumed")

    def record_task_status_changed(
        self,
        task_id: str,
        status: str,
        *,
        result: str | None = None,
        error: str | None = None,
    ) -> dict:
        """Append a task status event and refresh the projection."""
        event: dict[str, Any] = {
            "type": "task_status_changed",
            "status": status,
        }
        if result is not None:
            event["result"] = result
        if error is not None:
            event["error"] = error
        if status == "interrupted":
            event["interrupted_at"] = datetime.now().astimezone().isoformat()
        with self._tree_lock:
            return self._append_event_and_refresh_projection_unlocked(task_id, event)

    def record_worker_started(
        self,
        task_id: str,
        worker_name: str,
        *,
        input_hash: str,
        task_input: str,
        reuse_incomplete: bool = False,
    ) -> int:
        """Allocate or claim an executable worker call.

        Runtime execution uses :meth:`prepare_worker_call`, which also makes
        completed-result reuse an atomic decision.  This lower-level method is
        retained for event-model callers that always intend to execute.
        """
        preparation = self._prepare_worker_call(
            task_id,
            worker_name,
            input_hash=input_hash,
            task_input=task_input,
            resume=reuse_incomplete,
            allow_completed_cache=False,
        )
        return preparation.call_index

    def prepare_worker_call(
        self,
        task_id: str,
        worker_name: str,
        *,
        input_hash: str,
        task_input: str,
        resume: bool,
    ) -> WorkerCallPreparation:
        """Atomically decide whether one invocation executes or uses cache.

        A resumed attempt first claims unfinished matching work, then claims
        one completed matching result, both in call-index order.  Each prior
        call can be claimed only once per run attempt.  A fresh run never
        reuses checkpoint results.
        """
        return self._prepare_worker_call(
            task_id,
            worker_name,
            input_hash=input_hash,
            task_input=task_input,
            resume=resume,
            allow_completed_cache=True,
        )

    def _prepare_worker_call(
        self,
        task_id: str,
        worker_name: str,
        *,
        input_hash: str,
        task_input: str,
        resume: bool,
        allow_completed_cache: bool,
    ) -> WorkerCallPreparation:
        self.worker_dir(task_id, worker_name)
        if resume and not self._run_id:
            raise ValueError("run_id is required to prepare resumed worker calls")
        with self._tree_lock:
            events_path = self._task_events_path(task_id)
            storage, relative = self._task_storage_for_path(events_path, create=True)
            # ``_tree_lock`` is manager-local.  Lock the event source of truth
            # as well so independent managers cannot select the same call.
            with storage.advisory_file_lock(relative, create=True):
                tree = self._load_task_tree_unlocked(task_id) or {
                    "task_id": task_id,
                    "agent_name": self._supervisor_name,
                    "status": "running",
                    "workers": {},
                }
                workers = tree.setdefault("workers", {})
                calls = workers.setdefault(worker_name, [])
                if not isinstance(calls, list):
                    calls = [calls]
                    workers[worker_name] = calls
                if resume:
                    reusable_statuses = {"running", "interrupted", "crashed"}
                    for call in _sorted_worker_calls(calls):
                        if (
                            call.get("input_hash") == input_hash
                            and call.get("status") in reusable_statuses
                            and call.get("attempt_run_id") != self._run_id
                        ):
                            call_index = _coerce_call_index(call.get("call_index"), 0)
                            self._append_event_and_refresh_projection_unlocked(
                                task_id,
                                {
                                    "type": "worker_call_resume_claimed",
                                    "agent_name": worker_name,
                                    "call_index": call_index,
                                    "input_hash": input_hash,
                                    "task_input": str(task_input),
                                    "run_id": self._run_id,
                                    "claimed_at": datetime.now().astimezone().isoformat(),
                                },
                            )
                            return WorkerCallPreparation(
                                call_index=call_index,
                                should_execute=True,
                            )
                    if allow_completed_cache:
                        for call in _sorted_worker_calls(calls):
                            if (
                                call.get("input_hash") == input_hash
                                and call.get("status") == "completed"
                                and call.get("attempt_run_id") != self._run_id
                                and call.get("cached_claim_run_id") != self._run_id
                            ):
                                call_index = _coerce_call_index(
                                    call.get("call_index"),
                                    0,
                                )
                                self._append_event_and_refresh_projection_unlocked(
                                    task_id,
                                    {
                                        "type": "worker_call_cached_result_claimed",
                                        "agent_name": worker_name,
                                        "call_index": call_index,
                                        "input_hash": input_hash,
                                        "run_id": self._run_id,
                                        "claimed_at": datetime.now().astimezone().isoformat(),
                                    },
                                )
                                return WorkerCallPreparation(
                                    call_index=call_index,
                                    should_execute=False,
                                    cached_result=call.get("result"),
                                )
                existing = [_coerce_call_index(c.get("call_index"), -1) for c in calls]
                call_index = (max(existing) + 1) if existing else 0
                event: dict[str, Any] = {
                    "type": "worker_call_started",
                    "agent_name": worker_name,
                    "call_index": call_index,
                    "input_hash": input_hash,
                    "task_input": str(task_input),
                    "started_at": datetime.now().astimezone().isoformat(),
                }
                if self._run_id:
                    event["run_id"] = self._run_id
                self._append_event_and_refresh_projection_unlocked(task_id, event)
                return WorkerCallPreparation(
                    call_index=call_index,
                    should_execute=True,
                )

    def record_worker_finished(
        self,
        task_id: str,
        worker_name: str,
        *,
        call_index: int,
        status: str,
        input_hash: str = "",
        task_input: str = "",
        result: str | None = None,
        error: str | None = None,
    ) -> dict:
        """Record terminal state for one worker call."""
        self.worker_dir(task_id, worker_name)
        call_index = _require_call_index(call_index)
        event: dict[str, Any] = {
            "type": "worker_call_finished",
            "agent_name": worker_name,
            "call_index": call_index,
            "status": status,
            "finished_at": datetime.now().astimezone().isoformat(),
        }
        if input_hash:
            event["input_hash"] = input_hash
        if task_input:
            event["task_input"] = str(task_input)
        if result is not None:
            event["result"] = result
        if error is not None:
            event["error"] = error
        with self._tree_lock:
            return self._append_event_and_refresh_projection_unlocked(task_id, event)

    # ── supervisor checkpoint ────────────────────────────────────────────

    def save_supervisor_checkpoint(
        self,
        task_id: str,
        *,
        memory_steps: list,
        task_text: str,
        status: str,
        config_snapshot: dict | None = None,
        result: str | None = None,
        error: str | None = None,
        context_store: dict | None = None,
    ) -> Path:
        """Save the supervisor's ``memory.steps`` plus metadata."""
        data = {
            "agent_name": self._supervisor_name,
            "agent_type": "supervisor",
            "task_id": task_id,
            "task_text": task_text,
            "status": status,
            "step_count": len(memory_steps),
            "memory_steps": CheckpointSerializer.serialize_memory_steps(memory_steps),
            "saved_at": datetime.now().astimezone().isoformat(),
        }
        if self._run_id:
            data["run_id"] = self._run_id
        if config_snapshot:
            data["config_snapshot"] = config_snapshot
        if result is not None:
            data["result"] = result
        if error is not None:
            data["error"] = error
        if context_store is not None:
            data["context_store"] = context_store
        p = self._supervisor_ckpt(task_id)
        self._write_json(p, data)
        return p

    def load_supervisor_checkpoint(self, task_id: str) -> dict | None:
        return self._read_json(self._supervisor_ckpt(task_id))

    # ── worker checkpoint ────────────────────────────────────────────────

    def save_worker_checkpoint(
        self,
        task_id: str,
        worker_name: str,
        *,
        call_index: int = 0,
        input_hash: str = "",
        memory_steps: list | None = None,
        task_input: str = "",
        status: str = "completed",
        result: str | None = None,
        error: str | None = None,
    ) -> Path:
        data: dict[str, Any] = {
            "agent_name": worker_name,
            "agent_type": "worker",
            "task_id": task_id,
            "call_index": call_index,
            "input_hash": input_hash,
            "task_input": task_input,
            "status": status,
            "saved_at": datetime.now().astimezone().isoformat(),
        }
        if self._run_id:
            data["run_id"] = self._run_id
        if memory_steps:
            data["step_count"] = len(memory_steps)
            data["memory_steps"] = CheckpointSerializer.serialize_memory_steps(memory_steps)
        if result is not None:
            data["result"] = result
        if error is not None:
            data["error"] = error
        p = self._worker_call_ckpt(task_id, worker_name, call_index)
        self._write_json(p, data)
        return p

    def load_worker_checkpoint(
        self,
        task_id: str,
        worker_name: str,
        call_index: int | None = None,
    ) -> dict | None:
        """Load a worker checkpoint.

        Checkpoints are stored per call under
        ``workers/{worker_name}/calls/{call_index}/checkpoint.json``.  When
        *call_index* is omitted, the latest call for the worker is returned.
        """
        if call_index is not None:
            return self._read_json(self._worker_call_ckpt(task_id, worker_name, call_index))

        latest_index: int | None = None
        try:
            tree = self.load_task_tree(task_id) or {}
            calls = tree.get("workers", {}).get(worker_name, [])
            if isinstance(calls, dict):
                calls = [calls]
            if isinstance(calls, list) and calls:
                latest_index = max(_coerce_call_index(c.get("call_index"), -1) for c in calls)
                if latest_index < 0:
                    latest_index = None
        except Exception:
            latest_index = None

        if latest_index is None:
            calls_dir = self.worker_dir(task_id, worker_name) / "calls"
            try:
                storage, relative = self._task_storage_for_path(
                    calls_dir / ".placeholder",
                    create=False,
                )
                indexes = [_coerce_call_index(name, -1) for name in storage.directory_names(relative.parent)]
                indexes = [idx for idx in indexes if idx >= 0]
                latest_index = max(indexes) if indexes else None
            except (FileNotFoundError, OSError, RuntimeError):
                latest_index = None

        if latest_index is not None:
            return self._read_json(self._worker_call_ckpt(task_id, worker_name, latest_index))

        return None

    # ── listing / enumeration ────────────────────────────────────────────

    def _iter_all_task_dirs(self) -> list[Path]:
        """Return task directories directly managed by this instance."""
        if self._checkpoint_dir is not None:
            return [self._checkpoint_dir] if self._checkpoint_dir.is_dir() else []
        if not self._checkpoints_root.is_dir():
            return []
        return [child for child in sorted(self._checkpoints_root.iterdir()) if _is_checkpoint_task_dir(child)]

    def list_tasks(self) -> list[dict]:
        """Return metadata for every checkpoint under this supervisor.

        Each entry contains at least ``task_id``, ``agent_name``, ``status``,
        and ``saved_at``.  Tasks whose ``status`` is ``"running"`` but whose
        heartbeat PID is dead are reported as ``"crashed"``.
        """
        all_task_dirs = self._iter_all_task_dirs()
        if not all_task_dirs:
            return []

        entries: list[dict] = []
        for task_dir in all_task_dirs:
            if not task_dir.is_dir():
                continue
            tree = self._load_task_tree_from_dir(task_dir)
            if tree:
                entry = {
                    "task_id": tree.get("task_id", task_dir.name),
                    "agent_name": tree.get("agent_name", self._supervisor_name),
                    "status": tree.get("status", "unknown"),
                    "created_at": tree.get("created_at", ""),
                    "interrupted_at": tree.get("interrupted_at", ""),
                    "run_id": tree.get("run_id", ""),
                }
            else:
                # Fallback: derive from supervisor checkpoint
                sup = self._read_json(task_dir / "checkpoint.json")
                if sup:
                    entry = {
                        "task_id": sup.get("task_id", task_dir.name),
                        "agent_name": sup.get("agent_name", self._supervisor_name),
                        "status": sup.get("status", "unknown"),
                        "created_at": sup.get("saved_at", ""),
                        "interrupted_at": sup.get("saved_at", ""),
                        "run_id": sup.get("run_id", ""),
                    }
                else:
                    continue

            # Read heartbeat for crash detection + dashboard metadata.
            hb = self._read_json(task_dir / "heartbeat.json")
            if entry["status"] == "running":
                entry["status"] = _detect_crashed_status(hb)

            # Attach heartbeat details for dashboard display.
            if hb:
                entry["step"] = hb.get("step")
                entry["pid"] = hb.get("pid")
                hb_ts = hb.get("timestamp")
                entry["heartbeat_ts"] = hb_ts
                entry["heartbeat_age"] = round(time.time() - hb_ts, 1) if hb_ts else None
            else:
                entry["step"] = None
                entry["pid"] = None
                entry["heartbeat_ts"] = None
                entry["heartbeat_age"] = None

            # ── File history stats (snapshot count + tracked file count) ──
            fh_index = self._read_json(task_dir / "file-history" / "snapshots.json")
            if fh_index:
                snaps = fh_index.get("snapshots", [])
                tracked = set()
                for s in snaps:
                    tracked.update(s.get("tracked_file_backups", {}).keys())
                entry["fh_snapshots"] = len(snaps)
                entry["fh_tracked_files"] = len(tracked)
            else:
                entry["fh_snapshots"] = 0
                entry["fh_tracked_files"] = 0

            # ── Worker details (from task_tree + per-worker heartbeat) ──
            workers_detail: list[dict] = []
            tree_workers = (tree or {}).get("workers", {})
            for w_name, w_calls in tree_workers.items():
                try:
                    worker_dir = self.worker_dir(task_dir.name, w_name)
                except ValueError:
                    continue
                if not isinstance(w_calls, list):
                    w_calls = [w_calls]
                # Try to read the per-worker heartbeat file.
                w_hb_path = worker_dir / "heartbeat.json"
                w_hb = self._read_json(w_hb_path)
                for w_call in w_calls:
                    ci = w_call.get("call_index", 0)
                    w_status = w_call.get("status", "unknown")
                    # Crash detection: if task_tree says running, check heartbeat.
                    if w_status == "running":
                        if w_hb is not None:
                            w_status = _detect_worker_call_crashed(w_hb, ci)
                        elif entry["status"] == "crashed":
                            # No worker heartbeat; supervisor crashed → infer.
                            w_status = "crashed"
                    w_detail: dict = {
                        "agent_name": w_name,
                        "call_index": ci,
                        "status": w_status,
                        "error": w_call.get("error"),
                    }
                    # Enrich from worker heartbeat calls dict.
                    if w_hb is not None:
                        hb_call = w_hb.get("calls", {}).get(str(ci), {})
                        w_detail["step"] = hb_call.get("step")
                        w_detail["thread_id"] = hb_call.get("thread_id")
                        w_detail["started_at"] = hb_call.get("started_at")
                        w_detail["finished_at"] = hb_call.get("finished_at")
                        w_hb_ts = w_hb.get("timestamp")
                        w_detail["heartbeat_age"] = round(time.time() - w_hb_ts, 1) if w_hb_ts else None
                    else:
                        w_detail["step"] = None
                        w_detail["heartbeat_age"] = None
                    workers_detail.append(w_detail)
            entry["workers"] = workers_detail
            entry["checkpoint_dir"] = str(task_dir)

            entries.append(entry)
        return entries

    # ── cleanup ──────────────────────────────────────────────────────────

    def delete_task(self, task_id: str) -> bool:
        """Delete all checkpoint files for *task_id*."""
        d = self._task_dir(task_id)
        if d.is_symlink() or not d.is_dir():
            return False
        try:
            storage = self._task_storages.get(d)
            if storage is None:
                storage = SecureDirectory(d, create=False)
                self._task_storages[d] = storage
            if not storage.matches_path():
                return False
            storage.close()
            self._task_storages.pop(d, None)
            shutil.rmtree(d)
            return True
        except (FileNotFoundError, OSError, RuntimeError):
            return False

    def close(self) -> None:
        for storage in list(self._task_storages.values()):
            storage.close()
        self._task_storages.clear()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


# =========================================================================
# Cross-supervisor listing (for CLI ``list-tasks``)
# =========================================================================


_TASK_MARKERS = ("task_events.jsonl", "task_tree.json", "checkpoint.json")
_EXPIRABLE_TASK_STATUSES = frozenset(
    {
        "cancelled",
        "completed",
        "crashed",
        "error",
        "failed",
        "interrupted",
        "success",
        "succeeded",
    }
)


def _is_checkpoint_task_dir(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    return any(
        marker_path.is_file() and not marker_path.is_symlink()
        for marker_path in (path / marker for marker in _TASK_MARKERS)
    )


def _has_symlink_component(path: Path, root: Path) -> bool:
    """Return whether *path* reaches *root* through a symlinked component."""
    current = path
    while current != root:
        if current.is_symlink():
            return True
        if current.parent == current:
            return True
        current = current.parent
    return False


def iter_checkpoint_task_dirs(checkpoints_root: Path) -> list[Path]:
    """Discover canonical task directories without consulting any index."""
    configured_root = Path(checkpoints_root).expanduser()
    if configured_root.is_symlink():
        return []
    root = configured_root.resolve()
    if not root.is_dir():
        return []
    try:
        candidates = root.rglob("*")
        return sorted(
            path
            for path in candidates
            if not _has_symlink_component(path, root)
            and _is_checkpoint_task_dir(path)
            and not _has_checkpoint_task_ancestor(path, root)
        )
    except OSError:
        return []


def _iter_direct_checkpoint_task_dirs(checkpoints_root: Path) -> list[Path]:
    """Return only task children owned by one exact Application root."""

    if checkpoints_root.is_symlink() or not checkpoints_root.is_dir():
        return []
    try:
        return sorted(
            child for child in checkpoints_root.iterdir() if not child.is_symlink() and _is_checkpoint_task_dir(child)
        )
    except OSError:
        return []


def _has_checkpoint_task_ancestor(path: Path, root: Path) -> bool:
    """Reject nested worker checkpoints beneath an already identified task."""
    current = path.parent
    while current != root:
        if current.parent == current:
            return True
        if _is_checkpoint_task_dir(current):
            return True
        current = current.parent
    return False


def _task_events_are_intact(path: Path) -> bool:
    """Validate events, tolerating one crash-truncated final append.

    Appends are not atomic: SIGKILL may leave only the final JSON object
    truncated.  A valid canonical task tree plus an inactive task lease is
    sufficient deletion evidence in that case.  Malformation before the tail
    remains ambiguous and is retained.
    """
    if not path.exists():
        return True
    if path.is_symlink():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    saw_event = False
    nonempty = [line.strip() for line in lines if line.strip()]
    for index, raw in enumerate(nonempty):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return saw_event and index == len(nonempty) - 1
        if not isinstance(event, dict) or not event.get("type"):
            return False
        saw_event = True
    return saw_event


def _read_heartbeat_for_cleanup(path: Path) -> tuple[str, dict | None]:
    """Return ``missing``, ``valid``, or ``unknown`` plus the payload."""
    if not path.exists():
        return "missing", None
    if path.is_symlink():
        return "unknown", None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "unknown", None
    if not isinstance(payload, dict):
        return "unknown", None
    return "valid", payload


def _heartbeat_process_state(payload: dict) -> str:
    """Classify a structurally valid writer payload as live/inactive/unknown."""
    pid = payload.get("pid")
    timestamp = payload.get("timestamp")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(float(timestamp))
    ):
        return "unknown"
    return "live" if _detect_crashed_status(payload) == "running" else "inactive"


def _task_heartbeat_state(task_dir: Path) -> str:
    """Return live/inactive/unknown, preserving ambiguity instead of deleting."""
    state, payload = _read_heartbeat_for_cleanup(task_dir / "heartbeat.json")
    if state == "unknown":
        return "unknown"
    if payload is not None:
        status = payload.get("status")
        if status == "running":
            process_state = _heartbeat_process_state(payload)
            if process_state != "inactive":
                return process_state
        elif status not in {"stopped", "exited"}:
            return "unknown"

    workers_root = task_dir / "workers"
    if not workers_root.exists():
        return "inactive"
    if workers_root.is_symlink():
        return "unknown"
    try:
        worker_heartbeats = list(workers_root.rglob("heartbeat.json"))
    except OSError:
        return "unknown"
    for heartbeat_path in worker_heartbeats:
        if _has_symlink_component(heartbeat_path, task_dir):
            return "unknown"
        worker_state, worker_payload = _read_heartbeat_for_cleanup(heartbeat_path)
        if worker_state == "unknown":
            return "unknown"
        if worker_payload is None:
            continue
        calls = worker_payload.get("calls")
        if not isinstance(calls, dict):
            return "unknown"
        if any(isinstance(call, dict) and call.get("status") == "running" for call in calls.values()):
            process_state = _heartbeat_process_state(worker_payload)
            if process_state != "inactive":
                return process_state
    return "inactive"


def list_all_tasks(*, checkpoints_root: Path) -> list[dict]:
    """Scan the canonical ``checkpoints/<application>/<task>`` tree."""
    configured_root = Path(checkpoints_root).expanduser()
    if configured_root.is_symlink():
        return []
    root = configured_root.resolve()
    if not root.exists():
        return []

    all_entries: list[dict] = []
    for task_dir in iter_checkpoint_task_dirs(root):
        application_dir = task_dir.parent
        application_id = application_dir.relative_to(root).as_posix()
        manager = CheckpointManager(
            application_id,
            checkpoint_dir=task_dir,
        )
        for entry in manager.list_tasks():
            entry["application_id"] = application_id
            all_entries.append(entry)
    return all_entries


def _acquire_task_cleanup_lease(task_dir: Path) -> int | None:
    """Try to exclude an active run attempt before destructive cleanup."""
    if task_dir.is_symlink():
        return None
    try:
        fd = os.open(task_dir, os.O_RDONLY)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        os.close(fd)
        return None
    return fd


def _release_task_cleanup_lease(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _cleanup_expired_task(
    task_dir: Path,
    *,
    current: datetime,
    ttl: float,
) -> bool:
    task_lock_fd = _acquire_task_cleanup_lease(task_dir)
    if task_lock_fd is None:
        return False
    try:
        # An acquired task lease proves no canonical writer remains. Stale
        # framework temporary files therefore cannot extend task lifetime.
        if not _task_events_are_intact(task_dir / "task_events.jsonl"):
            return False
        if _task_heartbeat_state(task_dir) != "inactive":
            return False

        manager = CheckpointManager(
            task_dir.parent.name,
            checkpoint_dir=task_dir,
        )
        tree = manager._load_task_tree_from_dir(task_dir)
        if not isinstance(tree, dict):
            return False
        task_id = tree.get("task_id")
        if task_id != task_dir.name:
            return False
        status = str(tree.get("status", "")).strip().lower()
        if status == "running":
            status = "crashed"
        if status not in _EXPIRABLE_TASK_STATUSES:
            return False

        raw_created_at = tree.get("created_at")
        if not isinstance(raw_created_at, str) or not raw_created_at.strip():
            return False
        try:
            created_at = datetime.fromisoformat(raw_created_at.strip().replace("Z", "+00:00"))
        except ValueError:
            return False
        if created_at.tzinfo is None:
            return False
        age = (current.astimezone(created_at.tzinfo) - created_at).total_seconds()
        if age <= ttl:
            return False

        # The task lease closes the pre-heartbeat and cross-process race; the
        # second heartbeat check still protects non-run legacy writers.
        if _task_heartbeat_state(task_dir) != "inactive":
            return False
        return manager.delete_task(task_id)
    except (OSError, ValueError, TypeError):
        return False
    finally:
        _release_task_cleanup_lease(task_lock_fd)


def delete_checkpoint_task_if_inactive(task_dir: Path) -> bool:
    """Delete one checkpoint only when no run or heartbeat still owns it."""

    task_path = Path(task_dir).expanduser()
    task_lock_fd = _acquire_task_cleanup_lease(task_path)
    if task_lock_fd is None:
        return False
    try:
        if _task_heartbeat_state(task_path) != "inactive":
            return False
        manager = CheckpointManager(
            task_path.parent.name,
            checkpoint_dir=task_path,
        )
        tree = manager._load_task_tree_from_dir(task_path)
        if not isinstance(tree, dict) or tree.get("task_id") != task_path.name:
            return False
        return manager.delete_task(task_path.name)
    except (OSError, ValueError, TypeError):
        return False
    finally:
        _release_task_cleanup_lease(task_lock_fd)


def cleanup_expired_tasks(
    *,
    checkpoints_root: Path,
    max_age_seconds: int | float,
    now: datetime | None = None,
    recursive: bool = True,
) -> int:
    """Delete terminal checkpoints whose logical task age exceeds the TTL.

    ``created_at`` is the lifecycle anchor.  Filesystem mtimes, heartbeats,
    ContextStore writes, and file-history writes must not revive an expired
    task.  A task whose heartbeat still identifies a live process is preserved
    even when its original creation time is old.

    Invalid or missing timestamps are retained for manual inspection; guessing
    an age would make automated cleanup destructive.
    """
    if isinstance(max_age_seconds, bool):
        raise ValueError("max_age_seconds must be a finite number")
    try:
        ttl = float(max_age_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_age_seconds must be a finite number") from exc
    if not math.isfinite(ttl):
        raise ValueError("max_age_seconds must be finite")
    # This matches resume semantics: non-positive max_resume_age means that
    # task checkpoints do not expire.
    if ttl <= 0:
        return 0

    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()

    configured_root = Path(checkpoints_root).expanduser()
    if configured_root.is_symlink():
        return 0
    root = configured_root.resolve()
    if not root.is_dir():
        return 0

    lock_fd: int | None = None
    lock_acquired = False
    try:
        lock_fd = os.open(root, os.O_RDONLY)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        lock_acquired = True

        removed = 0
        task_dirs = iter_checkpoint_task_dirs(root) if recursive else _iter_direct_checkpoint_task_dirs(root)
        for task_dir in task_dirs:
            if _cleanup_expired_task(task_dir, current=current, ttl=ttl):
                removed += 1
        return removed
    except OSError:
        return 0
    finally:
        if lock_fd is not None:
            try:
                if lock_acquired:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
            finally:
                os.close(lock_fd)
