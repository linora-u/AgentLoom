"""One-time migration from the legacy log-coupled checkpoint layout.

The legacy ``.task_index.json`` files are intentionally never read.  Real
checkpoint directories and their own events/tree/checkpoint metadata are the
only source of truth.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .context import (
    portable_runtime_component,
    resolve_application_id,
    validate_runtime_id,
    validate_runtime_owned_path,
)

MigrationValidator = Callable[["MigrationCandidate", Path], None]


class MigrationError(RuntimeError):
    """Raised when migration cannot complete without risking partial state."""


@dataclass(frozen=True, slots=True)
class MigrationCandidate:
    source_dir: Path
    destination_dir: Path
    application_id: str
    task_id: str
    yaml_path: str
    created_at: datetime | None
    last_progress_at: datetime
    progress_kinds: tuple[str, ...]
    checksum: str
    malformed_event_lines: int = 0


@dataclass(frozen=True, slots=True)
class SkippedMigration:
    source_dir: Path
    task_id: str
    reason: str


@dataclass(slots=True)
class MigrationPlan:
    candidates: list[MigrationCandidate] = field(default_factory=list)
    skipped: list[SkippedMigration] = field(default_factory=list)

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


@dataclass(slots=True)
class MigrationResult:
    plan: MigrationPlan
    dry_run: bool
    migrated: list[MigrationCandidate] = field(default_factory=list)
    already_migrated: list[MigrationCandidate] = field(default_factory=list)
    archive_dir: Path | None = None

    @property
    def migrated_count(self) -> int:
        return len(self.migrated)

    @property
    def already_migrated_count(self) -> int:
        return len(self.already_migrated)


@dataclass(frozen=True, slots=True)
class _PreparedMigration:
    candidate: MigrationCandidate
    staged_dir: Path
    checksum: str


@dataclass(frozen=True, slots=True)
class _WorkflowIdentity:
    path: Path
    config: dict[str, Any]
    application_id: str


class RuntimeMigration:
    """Plan and apply a legacy checkpoint migration."""

    def __init__(
        self,
        *,
        legacy_logs_dir: str | Path,
        runtime_root: str | Path,
        agent_root: str | Path | None = None,
        max_age: timedelta = timedelta(days=7),
        now: datetime | None = None,
    ) -> None:
        self.legacy_logs_dir = Path(legacy_logs_dir).expanduser().absolute()
        self.runtime_root = Path(runtime_root).expanduser().absolute()
        self.agent_root = Path(agent_root).expanduser().absolute() if agent_root is not None else None
        self.max_age = max_age
        self.now = _as_utc(now or datetime.now(UTC))
        if _is_relative_to(self.runtime_root, self.legacy_logs_dir):
            raise ValueError("runtime_root must not be inside legacy_logs_dir")

    def scan(self) -> MigrationPlan:
        plan = MigrationPlan()
        candidates_by_destination: dict[Path, MigrationCandidate] = {}
        for task_dir in _iter_legacy_task_dirs(self.legacy_logs_dir):
            inspected = self._inspect_task(task_dir)
            if isinstance(inspected, SkippedMigration):
                plan.skipped.append(inspected)
                continue
            previous = candidates_by_destination.get(inspected.destination_dir)
            if previous is None or inspected.last_progress_at > previous.last_progress_at:
                if previous is not None:
                    plan.skipped.append(
                        SkippedMigration(
                            previous.source_dir,
                            previous.task_id,
                            "older duplicate checkpoint",
                        )
                    )
                candidates_by_destination[inspected.destination_dir] = inspected
            else:
                plan.skipped.append(
                    SkippedMigration(
                        inspected.source_dir,
                        inspected.task_id,
                        "older duplicate checkpoint",
                    )
                )

        plan.candidates = sorted(
            candidates_by_destination.values(),
            key=lambda candidate: candidate.destination_dir.as_posix(),
        )
        plan.skipped.sort(key=lambda item: (item.task_id, item.source_dir.as_posix()))
        return plan

    def migrate(
        self,
        *,
        dry_run: bool = True,
        archive_legacy: bool = False,
        validator: MigrationValidator | None = None,
    ) -> MigrationResult:
        plan = self.scan()
        result = MigrationResult(plan=plan, dry_run=dry_run)
        if dry_run:
            return result

        from src.lib.checkpoint.checkpoint_manager import CheckpointTaskLease

        _validate_migration_path(self.runtime_root, root=self.runtime_root)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.runtime_root / ".migration.lock"
        lock_stream = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_stream.close()
            raise MigrationError("another runtime migration is already in progress") from exc

        staging_parent = self.runtime_root / ".migration-staging"
        staging_root = staging_parent / uuid.uuid4().hex
        _validate_migration_path(staging_root, root=self.runtime_root)
        prepared: list[_PreparedMigration] = []
        created_destinations: list[Path] = []
        task_leases: list[CheckpointTaskLease] = []
        try:
            if staging_parent.exists():
                shutil.rmtree(staging_parent)

            # Phase 1 is intentionally invisible to runtime consumers.  Copy,
            # normalize, checksum, and all caller-provided validation happen in
            # staging before any canonical task path is published.
            for candidate in plan.candidates:
                staged = staging_root / Path(*candidate.application_id.split("/")) / candidate.task_id
                _validate_migration_path(candidate.destination_dir, root=self.runtime_root)
                _validate_migration_path(staged, root=self.runtime_root)
                if _has_symlink_component(candidate.source_dir, self.legacy_logs_dir):
                    raise MigrationError(
                        f"legacy checkpoint source has a symlink component: {candidate.source_dir}"
                    )
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(candidate.source_dir, staged, symlinks=True)
                copied_checksum = _tree_checksum(staged)
                if copied_checksum != candidate.checksum:
                    raise MigrationError(f"checksum mismatch while staging {candidate.task_id}")
                _normalize_legacy_worker_checkpoints(staged)
                validate_migrated_checkpoint(candidate, staged)
                if validator is not None:
                    validator(candidate, staged)
                # A validator may read through framework code that repairs an
                # old schema.  Revalidate and checksum that final staged tree.
                validate_migrated_checkpoint(candidate, staged)
                prepared.append(
                    _PreparedMigration(
                        candidate=candidate,
                        staged_dir=staged,
                        checksum=_tree_checksum(staged),
                    )
                )

            self._assert_sources_quiescent_and_unchanged(prepared)

            # Phase 2 publishes each canonical task.  A new task's directory
            # inode is leased while it is still staged, so the same exclusive
            # lease remains effective across the atomic rename.  Existing
            # destinations are leased before they are inspected.  Every lease
            # remains held through legacy archival or rollback; a concurrent
            # resume can therefore never become active and then be deleted by
            # rollback.
            for item in prepared:
                candidate = item.candidate
                destination = candidate.destination_dir
                staged = item.staged_dir
                if destination.exists():
                    lease = CheckpointTaskLease(
                        destination,
                        require_exists=True,
                    ).acquire()
                    task_leases.append(lease)
                    if _tree_checksum(destination) != item.checksum:
                        raise MigrationError(
                            f"destination already exists with different contents: {destination}"
                        )
                    validate_migrated_checkpoint(candidate, destination)
                    result.already_migrated.append(candidate)
                    shutil.rmtree(staged)
                    continue

                lease = CheckpointTaskLease(staged, require_exists=True).acquire()
                task_leases.append(lease)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, destination)
                created_destinations.append(destination)
                if _tree_checksum(destination) != item.checksum:
                    raise MigrationError(f"post-rename checksum mismatch for {candidate.task_id}")
                validate_migrated_checkpoint(candidate, destination)
                result.migrated.append(candidate)

            if archive_legacy:
                # Publishing and canonical validation can take time.  Refuse
                # to archive if a legacy writer appeared or any source gained
                # progress after the first pre-publish check.
                self._assert_sources_quiescent_and_unchanged(prepared)
                _validate_migration_path(
                    self.runtime_root / "legacy",
                    root=self.runtime_root,
                )
                result.archive_dir = archive_legacy_logs(
                    self.legacy_logs_dir,
                    self.runtime_root,
                    now=self.now,
                )
            return result
        except BaseException as exc:
            for destination in reversed(created_destinations):
                shutil.rmtree(destination, ignore_errors=True)
                _remove_empty_parents(
                    destination.parent,
                    stop=self.runtime_root / "checkpoints",
                )
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError(f"migration validation failed: {exc}") from exc
        finally:
            for lease in reversed(task_leases):
                lease.release()
            shutil.rmtree(staging_root, ignore_errors=True)
            try:
                staging_parent.rmdir()
            except OSError:
                pass
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
            lock_stream.close()

    def _inspect_task(self, task_dir: Path) -> MigrationCandidate | SkippedMigration:
        task_id = task_dir.name
        try:
            validate_runtime_id(task_id, field="task_id")
        except ValueError:
            return SkippedMigration(task_dir, task_id, "unsafe task id")
        if _has_symlink_component(task_dir, self.legacy_logs_dir):
            return SkippedMigration(task_dir, task_id, "unsafe symlink")

        events, malformed_lines, unsafe_event_corruption = _read_jsonl_objects(
            task_dir / "task_events.jsonl"
        )
        if unsafe_event_corruption:
            return SkippedMigration(task_dir, task_id, "corrupt task events")
        writer_state = _legacy_task_writer_state(task_dir)
        if writer_state == "live":
            return SkippedMigration(task_dir, task_id, "live legacy heartbeat")
        if writer_state == "unknown":
            return SkippedMigration(task_dir, task_id, "unverifiable legacy heartbeat")

        task_tree_path = task_dir / "task_tree.json"
        task_tree = _read_json_object(task_tree_path)
        if task_tree is None:
            reason = "invalid task tree" if task_tree_path.exists() else "missing task tree"
            return SkippedMigration(task_dir, task_id, reason)
        checkpoint_path = task_dir / "checkpoint.json"
        checkpoint = _read_json_object(checkpoint_path)
        if checkpoint is None:
            reason = "invalid checkpoint" if checkpoint_path.exists() else "missing checkpoint"
            return SkippedMigration(task_dir, task_id, reason)
        task_created = next(
            (event for event in events if event.get("type") == "task_created"),
            {},
        )
        yaml_path = str(
            task_created.get("yaml_path") or task_tree.get("yaml_path") or checkpoint.get("yaml_path") or ""
        )
        if not yaml_path.strip():
            return SkippedMigration(task_dir, task_id, "missing workflow path")
        supervisor = str(
            task_created.get("agent_name")
            or task_tree.get("agent_name")
            or checkpoint.get("agent_name")
            or _legacy_supervisor_name(task_dir, self.legacy_logs_dir)
        )
        workflow = self._workflow_identity(yaml_path)
        if isinstance(workflow, str):
            return SkippedMigration(task_dir, task_id, workflow)
        yaml_path = str(workflow.path)
        application_id = workflow.application_id
        recorded_application_id = str(
            task_created.get("application_id")
            or task_tree.get("application_id")
            or checkpoint.get("application_id")
            or ""
        ).strip()
        if recorded_application_id and recorded_application_id != application_id:
            return SkippedMigration(task_dir, task_id, "workflow application mismatch")
        managed_workflow = bool(
            self.agent_root is not None
            and _application_id_from_workflow(workflow.path, self.agent_root)
        )

        if _is_test_task(
            yaml_path=yaml_path,
            application_id=application_id,
            supervisor=supervisor,
            managed_workflow=managed_workflow,
        ):
            return SkippedMigration(task_dir, task_id, "test task")

        metadata: list[Any] = [events, task_tree, checkpoint]
        for path in sorted((task_dir / "workers").glob("**/checkpoint.json")):
            worker_checkpoint = _read_json_object(path)
            if worker_checkpoint is None:
                return SkippedMigration(
                    task_dir,
                    task_id,
                    "invalid worker checkpoint",
                )
            metadata.append(worker_checkpoint)
        for heartbeat_path in (
            task_dir / "heartbeat.json",
            *((task_dir / "workers").glob("**/heartbeat.json")),
        ):
            heartbeat = _read_json_object(heartbeat_path)
            if heartbeat is not None:
                metadata.append(heartbeat)

        timestamps = list(_metadata_timestamps(metadata))
        if not timestamps:
            return SkippedMigration(task_dir, task_id, "missing execution timestamp")
        last_progress_at = max(timestamps)
        created_at = _parse_datetime(
            task_created.get("created_at") or task_tree.get("created_at") or checkpoint.get("created_at")
        )
        if created_at is None:
            return SkippedMigration(task_dir, task_id, "missing original created_at")
        # Resume expiry is anchored to the original logical task creation time.
        # A freshly touched directory/heartbeat must not revive an expired task.
        if self.now - created_at > self.max_age:
            return SkippedMigration(task_dir, task_id, "outside migration window")

        file_history_state = _file_history_progress_state(task_dir)
        if file_history_state == "invalid":
            return SkippedMigration(task_dir, task_id, "invalid file history")
        context_store_state = _context_store_progress_state(task_dir)
        if context_store_state == "invalid":
            return SkippedMigration(task_dir, task_id, "invalid context store")
        progress_kinds = _progress_kinds(task_dir, checkpoint)
        if not progress_kinds:
            return SkippedMigration(task_dir, task_id, "no resumable progress")

        try:
            checksum = _tree_checksum(task_dir)
        except MigrationError:
            return SkippedMigration(task_dir, task_id, "unsafe symlink")
        destination = self.runtime_root / "checkpoints" / Path(*application_id.split("/")) / task_id
        return MigrationCandidate(
            source_dir=task_dir,
            destination_dir=destination,
            application_id=application_id,
            task_id=task_id,
            yaml_path=yaml_path,
            created_at=created_at,
            last_progress_at=last_progress_at,
            progress_kinds=progress_kinds,
            checksum=checksum,
            malformed_event_lines=malformed_lines,
        )

    def _application_id(
        self,
        yaml_path: str,
        supervisor: str,
        task_dir: Path,
    ) -> str:
        del supervisor, task_dir
        workflow = self._workflow_identity(yaml_path)
        if isinstance(workflow, str):
            raise MigrationError(workflow)
        return workflow.application_id

    def _workflow_identity(self, yaml_path: str) -> _WorkflowIdentity | str:
        path = Path(yaml_path).expanduser()
        if not path.is_absolute():
            if self.agent_root is None:
                return "relative workflow path requires agent_root"
            path = self.agent_root / path
        try:
            path = path.resolve(strict=True)
        except (FileNotFoundError, OSError):
            return "workflow file not found"
        if not path.is_file():
            return "workflow file not found"

        # Use the exact loader and launch-time validation used by run_app.  In
        # particular, the full config (including explicit application_id/app/
        # application) must participate in application scope resolution.
        from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory
        from src.runner import validate_required_yaml_fields

        try:
            agent_config = YamlAgentFactory._load_config_from_file(path)
            validate_required_yaml_fields(agent_config, path)
            application_id = resolve_application_id(
                agent_config,
                path,
                agent_root=self.agent_root,
            )
        except Exception:
            return "invalid workflow"
        if not application_id:
            return "invalid workflow"
        return _WorkflowIdentity(
            path=path,
            config=agent_config,
            application_id=application_id,
        )

    def _assert_sources_quiescent_and_unchanged(
        self,
        prepared: Iterable[_PreparedMigration],
    ) -> None:
        # Archival moves the whole legacy tree, not just candidates.  A live or
        # ambiguous writer in any skipped task therefore blocks the operation.
        for task_dir in _iter_legacy_task_dirs(self.legacy_logs_dir):
            state = _legacy_task_writer_state(task_dir)
            if state != "inactive":
                raise MigrationError(
                    f"legacy checkpoint writer is {state}: {task_dir}"
                )
        for item in prepared:
            if _has_symlink_component(
                item.candidate.source_dir,
                self.legacy_logs_dir,
            ):
                raise MigrationError(
                    f"legacy checkpoint source has a symlink component: "
                    f"{item.candidate.source_dir}"
                )
            try:
                current_checksum = _tree_checksum(item.candidate.source_dir)
            except (OSError, MigrationError) as exc:
                raise MigrationError(
                    f"legacy checkpoint source became unreadable: {item.candidate.source_dir}"
                ) from exc
            if current_checksum != item.candidate.checksum:
                raise MigrationError(
                    f"legacy checkpoint changed during migration: {item.candidate.task_id}"
                )


def _validate_migration_path(path: Path, *, root: Path) -> None:
    try:
        validate_runtime_owned_path(path, root=root)
    except RuntimeError as exc:
        raise MigrationError(str(exc)) from exc


def plan_migration(
    legacy_logs_dir: str | Path,
    runtime_root: str | Path,
    *,
    agent_root: str | Path | None = None,
    max_age: timedelta = timedelta(days=7),
    now: datetime | None = None,
) -> MigrationPlan:
    return RuntimeMigration(
        legacy_logs_dir=legacy_logs_dir,
        runtime_root=runtime_root,
        agent_root=agent_root,
        max_age=max_age,
        now=now,
    ).scan()


def migrate_runtime(
    legacy_logs_dir: str | Path,
    runtime_root: str | Path,
    *,
    dry_run: bool = True,
    archive_legacy: bool = False,
    validator: MigrationValidator | None = None,
    agent_root: str | Path | None = None,
    max_age: timedelta = timedelta(days=7),
    now: datetime | None = None,
) -> MigrationResult:
    return RuntimeMigration(
        legacy_logs_dir=legacy_logs_dir,
        runtime_root=runtime_root,
        agent_root=agent_root,
        max_age=max_age,
        now=now,
    ).migrate(
        dry_run=dry_run,
        archive_legacy=archive_legacy,
        validator=validator,
    )


def archive_legacy_logs(
    legacy_logs_dir: str | Path,
    runtime_root: str | Path,
    *,
    now: datetime | None = None,
) -> Path | None:
    """Atomically move the whole legacy log tree beneath ``legacy/``."""

    source = Path(legacy_logs_dir).expanduser().absolute()
    if not source.exists():
        return None
    if source.is_symlink():
        raise MigrationError(f"legacy_logs_dir must not be a symlink: {source}")
    root = Path(runtime_root).expanduser().absolute()
    if _is_relative_to(root, source):
        raise MigrationError("runtime_root must not be inside legacy_logs_dir")
    legacy_dir = root / "legacy"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    timestamp = _as_utc(now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")
    destination = legacy_dir / f"logs-v1-{timestamp}"
    suffix = 1
    while destination.exists():
        destination = legacy_dir / f"logs-v1-{timestamp}-{suffix}"
        suffix += 1
    try:
        os.replace(source, destination)
    except OSError as exc:
        raise MigrationError(f"failed to atomically archive {source}: {exc}") from exc
    return destination


def _iter_legacy_task_dirs(legacy_root: Path) -> list[Path]:
    if legacy_root.is_symlink() or not legacy_root.is_dir():
        return []
    found: set[Path] = set()
    patterns = ("*/*/checkpoints/*", "*/checkpoints/*")
    for pattern in patterns:
        for path in legacy_root.glob(pattern):
            if (
                path.is_dir()
                and not path.name.startswith(".")
                and not _has_symlink_component(path, legacy_root)
            ):
                found.add(path.absolute())
    return sorted(found, key=lambda path: path.as_posix())


def _has_symlink_component(path: Path, root: Path) -> bool:
    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    if current.is_symlink():
        return True
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _read_jsonl_objects(path: Path) -> tuple[list[dict[str, Any]], int, bool]:
    events: list[dict[str, Any]] = []
    malformed: list[tuple[int, bool]] = []
    try:
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except UnicodeError:
        return events, 1, True
    except OSError:
        return events, 0, False
    for index, line in enumerate(lines):
        raw = line.strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            malformed.append((index, True))
            continue
        if isinstance(value, dict) and value.get("type"):
            events.append(value)
        else:
            malformed.append((index, False))

    # A process can die halfway through its final append.  That one specific
    # case is recoverable.  Corruption in the middle (or multiple bad records)
    # can hide completed worker events and would make resume repeat work.
    safe_crash_tail = (
        len(malformed) == 1
        and malformed[0][0] == len(lines) - 1
        and malformed[0][1]
    )
    return events, len(malformed), bool(malformed) and not safe_crash_tail


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _legacy_task_writer_state(task_dir: Path) -> str:
    """Return inactive/live/unknown for every legacy writer heartbeat."""

    heartbeat_paths = [task_dir / "heartbeat.json"]
    workers_root = task_dir / "workers"
    if workers_root.is_symlink():
        return "unknown"
    if workers_root.exists():
        try:
            heartbeat_paths.extend(workers_root.rglob("heartbeat.json"))
        except OSError:
            return "unknown"

    for heartbeat_path in heartbeat_paths:
        if not heartbeat_path.exists():
            continue
        if heartbeat_path.is_symlink():
            return "unknown"
        payload = _read_json_object(heartbeat_path)
        if payload is None:
            return "unknown"

        calls = payload.get("calls")
        if isinstance(calls, dict):
            running = any(
                isinstance(call, dict) and call.get("status") == "running"
                for call in calls.values()
            )
            if not running:
                continue
        else:
            status = str(payload.get("status") or "").strip().lower()
            if status in {"stopped", "exited", "completed", "failed"}:
                continue
            if status not in {"", "running"}:
                return "unknown"

        pid = payload.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return "unknown"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            return "live"
        except OSError:
            return "unknown"
        return "live"
    return "inactive"


def _metadata_timestamps(values: Iterable[Any]) -> Iterable[datetime]:
    timestamp_keys = {
        "created_at",
        "finished_at",
        "interrupted_at",
        "last_run_at",
        "saved_at",
        "started_at",
        "timestamp",
        "updated_at",
    }

    def walk(value: Any) -> Iterable[datetime]:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in timestamp_keys:
                    parsed = _parse_datetime(child)
                    if parsed is not None:
                        yield parsed
                elif isinstance(child, (dict, list, tuple)):
                    yield from walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                yield from walk(child)

    for value in values:
        yield from walk(value)


def _progress_kinds(task_dir: Path, checkpoint: dict[str, Any]) -> tuple[str, ...]:
    kinds: list[str] = []
    checkpoints = [checkpoint]
    for path in sorted((task_dir / "workers").glob("**/checkpoint.json")):
        worker = _read_json_object(path)
        if worker is not None:
            checkpoints.append(worker)
    if any(_checkpoint_has_memory(item) for item in checkpoints):
        kinds.append("memory")

    if _context_store_progress_state(task_dir) == "valid":
        kinds.append("context_store")

    if _file_history_progress_state(task_dir) == "valid":
        kinds.append("file_history")
    return tuple(kinds)


def _context_store_progress_state(task_dir: Path) -> str:
    """Return absent, valid, or invalid for durable ContextStore entries."""

    store_dir = task_dir / "context_store"
    if not store_dir.exists():
        return "absent"
    if not store_dir.is_dir() or store_dir.is_symlink():
        return "invalid"
    entries_dir = store_dir / "entries"
    if not entries_dir.exists():
        return "absent"
    if not entries_dir.is_dir() or entries_dir.is_symlink():
        return "invalid"
    found = False
    try:
        entry_paths = sorted(entries_dir.glob("ctx_*.json"))
    except OSError:
        return "invalid"
    for entry_path in entry_paths:
        if entry_path.is_symlink():
            return "invalid"
        entry = _read_json_object(entry_path)
        if entry is None:
            return "invalid"
        if entry.get("ref") and entry.get("original") is not None:
            found = True
    return "valid" if found else "absent"


def _file_history_progress_state(task_dir: Path) -> str:
    """Return absent, valid, or invalid for durable file-history progress."""

    history_dir = task_dir / "file-history"
    if not history_dir.is_dir() or history_dir.is_symlink():
        return "absent"
    index_path = history_dir / "snapshots.json"
    if not index_path.exists():
        # Crash leftovers such as .bk_* / .idx_* and metadata files are not
        # resumable state and must not turn a task into a migration candidate.
        return "absent"
    history_index = _read_json_object(index_path)
    if history_index is None:
        return "invalid"
    snapshots = history_index.get("snapshots", [])
    first_backups = history_index.get("first_tracked_backups", {})
    if not isinstance(snapshots, list) or not isinstance(first_backups, dict):
        return "invalid"
    has_progress = bool(snapshots or first_backups)
    if not has_progress:
        return "absent"
    try:
        _validate_file_history_backups(history_dir, history_index)
    except MigrationError:
        return "invalid"
    return "valid"


def _checkpoint_has_memory(checkpoint: dict[str, Any]) -> bool:
    steps = checkpoint.get("memory_steps")
    return isinstance(steps, list) and len(steps) > 0


def _application_id_from_workflow(workflow: Path, agent_root: Path) -> str:
    path = workflow.expanduser()
    if not path.is_absolute():
        path = agent_root / path
    try:
        relative = path.absolute().relative_to((agent_root / "applications").absolute())
    except ValueError:
        return ""
    parts = relative.parts
    try:
        workflows_index = parts.index("workflows")
    except ValueError:
        return ""
    if workflows_index < 1:
        return ""
    return "/".join(parts[:workflows_index])


def _is_test_task(
    *,
    yaml_path: str,
    application_id: str,
    supervisor: str,
    managed_workflow: bool,
) -> bool:
    normalized = yaml_path.replace("\\", "/").lower()
    path_parts = {part for part in normalized.split("/") if part}
    app_parts = {part.lower() for part in application_id.split("/")}
    if not managed_workflow and ("pytest-of-" in normalized or any(part.startswith("pytest-") for part in path_parts)):
        return True
    if "tests" in path_parts:
        return True
    if any(part == "test" or part.startswith("test_") for part in app_parts):
        return True
    return supervisor.lower().startswith("test_")


def _legacy_supervisor_name(task_dir: Path, legacy_root: Path) -> str:
    try:
        relative = task_dir.relative_to(legacy_root)
    except ValueError:
        return "legacy"
    return relative.parts[0] if relative.parts else "legacy"


def _normalize_legacy_worker_checkpoints(task_dir: Path) -> None:
    """Convert v1 worker checkpoints to the canonical per-call layout."""

    workers_dir = task_dir / "workers"
    if not workers_dir.is_dir():
        return
    tree = _read_json_object(task_dir / "task_tree.json") or {}
    for worker_dir in sorted(path for path in workers_dir.iterdir() if path.is_dir()):
        raw_worker_name = worker_dir.name
        portable_name = portable_runtime_component(raw_worker_name, fallback="worker")
        if portable_name != raw_worker_name:
            portable_dir = workers_dir / portable_name
            if portable_dir.exists():
                raise MigrationError(
                    f"conflicting portable worker directory: {portable_dir}"
                )
            worker_dir.rename(portable_dir)
            worker_dir = portable_dir
        legacy_checkpoint = worker_dir / "checkpoint.json"
        if not legacy_checkpoint.is_file():
            continue
        checkpoint = _read_json_object(legacy_checkpoint)
        if checkpoint is None:
            raise MigrationError(f"invalid legacy worker checkpoint: {legacy_checkpoint}")
        call_index = _worker_call_index(checkpoint, tree, raw_worker_name)
        target = worker_dir / "calls" / str(call_index) / "checkpoint.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != legacy_checkpoint.read_bytes():
                raise MigrationError(f"conflicting worker checkpoints for {worker_dir.name} call {call_index}")
            legacy_checkpoint.unlink()
        else:
            os.replace(legacy_checkpoint, target)


def _worker_call_index(checkpoint: dict[str, Any], tree: dict[str, Any], worker_name: str) -> int:
    raw_index = checkpoint.get("call_index")
    if isinstance(raw_index, int) and not isinstance(raw_index, bool) and raw_index >= 0:
        return raw_index
    if isinstance(raw_index, str) and raw_index.isdigit():
        return int(raw_index)

    worker = (tree.get("workers") or {}).get(worker_name, [])
    calls = worker if isinstance(worker, list) else [worker]
    indexes: list[int] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        try:
            index = int(call.get("call_index"))
        except (TypeError, ValueError):
            continue
        if index >= 0:
            indexes.append(index)
    return max(indexes) if indexes else 0


def _tree_checksum(root: Path) -> str:
    digest = hashlib.sha256()
    if root.is_symlink():
        raise MigrationError(f"symlink is not allowed in checkpoint tree: {root}")
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink():
            raise MigrationError(f"symlink is not allowed in checkpoint tree: {path}")
        if path.is_dir():
            digest.update(b"D\0" + relative + b"\0")
            continue
        if not path.is_file():
            raise MigrationError(f"unsupported checkpoint entry: {path}")
        digest.update(b"F\0" + relative + b"\0")
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            raise MigrationError(f"failed to checksum {path}: {exc}") from exc
        digest.update(b"\0")
    return digest.hexdigest()


def validate_migrated_checkpoint(candidate: MigrationCandidate, destination: Path) -> None:
    """Deeply validate canonical resume, ContextRefs, and file history.

    Imports stay local so planning and ``--dry-run`` do not initialize the
    checkpoint/runtime stack.
    """

    if not (destination / "task_events.jsonl").is_file() and not (destination / "task_tree.json").is_file():
        raise MigrationError(f"task metadata missing for {candidate.task_id}")
    progress = _progress_kinds(
        destination,
        _read_json_object(destination / "checkpoint.json") or {},
    )
    if not progress:
        raise MigrationError(f"resumable progress missing for {candidate.task_id}")

    from types import SimpleNamespace

    from src.lib.checkpoint import CheckpointManager
    from src.lib.checkpoint.coordinator import CheckpointCoordinator

    manager = CheckpointManager(
        "migration-validator",
        checkpoint_dir=destination,
    )
    tree = manager.load_task_tree(candidate.task_id)
    if not isinstance(tree, dict):
        raise MigrationError(f"canonical task tree unreadable for {candidate.task_id}")
    supervisor_checkpoint = manager.load_supervisor_checkpoint(candidate.task_id)
    if not isinstance(supervisor_checkpoint, dict):
        raise MigrationError(f"supervisor checkpoint unreadable for {candidate.task_id}")

    sentinel = object()
    holder = SimpleNamespace(memory=SimpleNamespace(steps=sentinel))
    coordinator = CheckpointCoordinator(
        manager,
        candidate.task_id,
        str(supervisor_checkpoint.get("task_text", "")),
        resume=True,
    )
    coordinator.restore(holder)
    if holder.memory.steps is sentinel:
        raise MigrationError(f"supervisor restore failed for {candidate.task_id}")
    if supervisor_checkpoint.get("memory_steps") and not holder.memory.steps:
        raise MigrationError(f"supervisor memory was not restored for {candidate.task_id}")

    workers_dir = destination / "workers"
    if workers_dir.is_dir():
        legacy_workers = sorted(workers_dir.glob("*/checkpoint.json"))
        if legacy_workers:
            raise MigrationError(f"legacy worker layout remains at {legacy_workers[0]}")
        for worker_path in sorted(workers_dir.glob("*/calls/*/checkpoint.json")):
            relative = worker_path.relative_to(workers_dir)
            worker_name = relative.parts[0]
            try:
                call_index = int(relative.parts[2])
            except (IndexError, ValueError) as exc:
                raise MigrationError(f"invalid worker call path: {worker_path}") from exc
            worker_checkpoint = manager.load_worker_checkpoint(
                candidate.task_id,
                worker_name,
                call_index,
            )
            if not isinstance(worker_checkpoint, dict):
                raise MigrationError(f"canonical worker checkpoint unreadable: {worker_path}")
            raw_steps = worker_checkpoint.get("memory_steps") or []
            if raw_steps and worker_checkpoint.get("status") != "completed":
                worker_holder = SimpleNamespace(memory=SimpleNamespace(steps=[]))
                if not coordinator.restore_worker(worker_holder, worker_name, call_index):
                    raise MigrationError(f"worker restore failed for {worker_name} call {call_index}")

    context_store_dir = destination / "context_store"
    if context_store_dir.is_dir():
        from src.lib.context_engine.store import ContextStore

        store = ContextStore(context_store_dir)
        # Retrieval normally appends an audit event. Disable only that side
        # effect so validation stays checksum/idempotency neutral while still
        # exercising the real lookup and slicing path.
        store._append_event = lambda _event: None  # type: ignore[method-assign]
        refs = store.refs()
        if "context_store" in candidate.progress_kinds and not refs:
            raise MigrationError(f"ContextStore has no refs for {candidate.task_id}")
        for ref in refs:
            if store.get(ref) is None or store.retrieve(ref, limit=0) is None:
                raise MigrationError(f"ContextRef retrieval failed: {ref}")

    history_dir = destination / "file-history"
    history_index_path = history_dir / "snapshots.json"
    if history_index_path.exists():
        history_index = _read_json_object(history_index_path)
        if history_index is None:
            raise MigrationError(f"file-history index unreadable for {candidate.task_id}")
        _validate_file_history_backups(history_dir, history_index)

        from src.lib.checkpoint.file_history import FileHistoryManager

        history = FileHistoryManager(history_dir)
        try:
            history.restore_from_index(history_index)
        except Exception as exc:
            raise MigrationError(f"file-history restore failed: {exc}") from exc
        expected_snapshots = history_index.get("snapshots", [])
        if history.snapshot_count != len(expected_snapshots):
            raise MigrationError(f"file-history snapshots were not fully restored for {candidate.task_id}")
    elif "file_history" in candidate.progress_kinds:
        raise MigrationError(f"file-history index missing for {candidate.task_id}")


def _validate_file_history_backups(history_dir: Path, history_index: dict[str, Any]) -> None:
    groups: list[Any] = [history_index.get("first_tracked_backups", {})]
    for snapshot in history_index.get("snapshots", []):
        if isinstance(snapshot, dict):
            groups.append(snapshot.get("tracked_file_backups", {}))
    for group in groups:
        if not isinstance(group, dict):
            raise MigrationError("file-history backup mapping is invalid")
        for backup in group.values():
            if not isinstance(backup, dict):
                raise MigrationError("file-history backup record is invalid")
            filename = backup.get("backup_filename")
            if filename is None:
                continue
            backup_path = history_dir / str(filename)
            if not _is_relative_to(backup_path, history_dir) or not backup_path.is_file():
                raise MigrationError(f"file-history backup missing: {filename}")


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def _remove_empty_parents(path: Path, *, stop: Path) -> None:
    current = path
    while _is_relative_to(current, stop):
        try:
            current.rmdir()
        except OSError:
            return
        if current == stop:
            return
        current = current.parent
