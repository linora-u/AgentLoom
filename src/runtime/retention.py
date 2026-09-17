"""Bounded retention for run-scoped runtime data.

Only ``<runtime-root>/runs`` is eligible for deletion.  Checkpoints have their
own resume lifecycle, while ``legacy`` and application-owned output trees are
deliberately outside this cleaner's traversal boundary.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .context import RuntimeRunLease, safe_application_id, validate_runtime_id

_SUCCESS_STATUSES = frozenset({"completed", "success", "succeeded"})
_FAILURE_STATUSES = frozenset({"cancelled", "crashed", "error", "failed", "interrupted"})
_ORPHAN_RUN_GRACE = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    successful_runs: timedelta = timedelta(days=7)
    failed_runs: timedelta = timedelta(days=30)
    raw_artifacts: timedelta = timedelta(days=3)
    automatic_interval: timedelta = timedelta(hours=24)


@dataclass(slots=True)
class CleanupResult:
    removed_runs: list[Path] = field(default_factory=list)
    removed_artifacts: list[Path] = field(default_factory=list)
    reclaimed_bytes: int = 0
    skipped: bool = False
    skip_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def removed_run_count(self) -> int:
        return len(self.removed_runs)

    @property
    def removed_artifact_count(self) -> int:
        return len(self.removed_artifacts)


def clean_runtime(
    runtime_root: str | Path,
    *,
    policy: RetentionPolicy | None = None,
    config: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> CleanupResult:
    """Explicitly enforce run and raw-artifact retention.

    A directory is considered a run only when it owns a readable
    ``manifest.json``.  Unknown and currently-running statuses are preserved:
    deleting a live attempt is worse than retaining an orphan for manual
    inspection.
    """

    configured_root = Path(runtime_root).expanduser()
    if policy is not None and config is not None:
        raise ValueError("pass either policy or config, not both")
    policy = policy or retention_policy_from_config(config)
    current = _as_utc(now or datetime.now(UTC))
    if configured_root.is_symlink() or not configured_root.exists():
        return CleanupResult()
    root = configured_root.resolve()
    lock_fd = _acquire_cleanup_lock(root)
    if lock_fd is None:
        return CleanupResult(skipped=True, skip_reason="cleanup already in progress")
    try:
        return _clean_runtime_unlocked(root, policy=policy, current=current)
    finally:
        _release_cleanup_lock(lock_fd)


def _clean_runtime_unlocked(
    root: Path,
    *,
    policy: RetentionPolicy,
    current: datetime,
) -> CleanupResult:
    """Apply retention while the caller owns the runtime-root lock."""
    result = CleanupResult()
    runs_root = root / "runs"
    if runs_root.is_symlink() or not runs_root.is_dir():
        return result

    for manifest_path in sorted(runs_root.rglob("manifest.json")):
        if manifest_path.is_symlink() or not manifest_path.is_file():
            continue
        manifest = _read_json_object(manifest_path)
        run_dir = _canonical_run_dir(runs_root, manifest_path, manifest)
        if run_dir is None or run_dir.is_symlink():
            continue
        run_lease = RuntimeRunLease(run_dir)
        try:
            run_lease.acquire()
        except (BlockingIOError, OSError, RuntimeError):
            continue
        try:
            # Re-read only through the already validated canonical pathname.
            # The run lease prevents a cooperating AgentLoom process from
            # finalizing or cleaning the same run while retention evaluates it.
            manifest = _read_json_object(manifest_path)
            if _canonical_run_dir(runs_root, manifest_path, manifest) != run_dir:
                continue
            status = str(manifest.get("status", "")).strip().lower()
            if status in _SUCCESS_STATUSES:
                run_ttl = policy.successful_runs
            elif status in _FAILURE_STATUSES or status == "running":
                # A running manifest whose directory lease is acquirable has
                # no live owner: the attempt crashed before finalization.
                run_ttl = policy.failed_runs
            else:
                continue

            reference_time = _manifest_time(manifest)
            if reference_time is None:
                continue
            age = current - reference_time
            if age < timedelta(0):
                continue

            if age >= run_ttl:
                size = _tree_size(run_dir)
                try:
                    shutil.rmtree(run_dir)
                except OSError as exc:
                    result.errors.append(f"failed to remove run {run_dir}: {exc}")
                else:
                    result.removed_runs.append(run_dir)
                    result.reclaimed_bytes += size
                continue

            artifacts_dir = run_dir / "artifacts"
            if age >= policy.raw_artifacts and artifacts_dir.is_dir():
                size = _tree_size(artifacts_dir)
                try:
                    shutil.rmtree(artifacts_dir)
                except OSError as exc:
                    result.errors.append(f"failed to remove raw artifacts {artifacts_dir}: {exc}")
                else:
                    result.removed_artifacts.append(artifacts_dir)
                    result.reclaimed_bytes += size
        finally:
            run_lease.release()

    # A SIGKILL during the atomic first-manifest write can leave only the
    # canonical empty run skeleton and a framework manifest temp file.  Such a
    # run has no timestamp payload, so its newest filesystem mtime is used only
    # for this narrowly identified orphan case, with a non-zero safety grace.
    orphan_ttl = max(policy.failed_runs, _ORPHAN_RUN_GRACE)
    for run_dir in _safe_orphan_run_dirs(runs_root):
        run_lease = RuntimeRunLease(run_dir)
        try:
            run_lease.acquire()
        except (BlockingIOError, OSError, RuntimeError):
            continue
        try:
            reference_time = _orphan_reference_time(run_dir)
            if reference_time is None or current - reference_time < orphan_ttl:
                continue
            size = _tree_size(run_dir)
            try:
                shutil.rmtree(run_dir)
            except OSError as exc:
                result.errors.append(f"failed to remove orphan run {run_dir}: {exc}")
            else:
                result.removed_runs.append(run_dir)
                result.reclaimed_bytes += size
        finally:
            run_lease.release()

    return result


def _safe_orphan_run_dirs(runs_root: Path) -> list[Path]:
    candidates: set[Path] = set()
    try:
        markers = list(runs_root.rglob(".run-starting.json"))
    except OSError:
        return []
    for marker in markers:
        if marker.is_symlink() or not marker.is_file():
            continue
        identity = _read_json_object(marker)
        run_dir = _canonical_run_dir(runs_root, marker, identity, filename=marker.name)
        if run_dir is None:
            continue
        logs_dir = run_dir / "logs"
        if not logs_dir.is_dir() or logs_dir.is_symlink():
            continue
        if (run_dir / "manifest.json").exists() or run_dir.is_symlink():
            continue
        try:
            children = list(run_dir.iterdir())
        except OSError:
            continue
        safe = True
        for child in children:
            if child.name in {"logs", "audit", "artifacts"}:
                if not child.is_dir() or child.is_symlink():
                    safe = False
                    break
                try:
                    nested = list(child.iterdir())
                except OSError:
                    safe = False
                    break
                if child.name == "artifacts":
                    if any(
                        item.name not in {"shell", "background", "skills"}
                        or not item.is_dir()
                        or item.is_symlink()
                        or any(item.iterdir())
                        for item in nested
                    ):
                        safe = False
                        break
                elif nested:
                    safe = False
                    break
            elif child.name == ".run-starting.json":
                if child != marker or child.is_symlink() or not child.is_file():
                    safe = False
                    break
            elif not (
                child.is_file()
                and not child.is_symlink()
                and child.name.startswith(".manifest.json.")
                and child.name.endswith(".tmp")
            ):
                safe = False
                break
        if safe and {"logs", "audit", "artifacts"}.issubset(
            {child.name for child in children}
        ):
            candidates.add(run_dir)
    return sorted(candidates)


def _canonical_run_dir(
    runs_root: Path,
    identity_path: Path,
    payload: dict[str, Any] | None,
    *,
    filename: str = "manifest.json",
) -> Path | None:
    """Return the canonical run directory named by one trusted identity file.

    A raw artifact may legitimately be called ``manifest.json``.  Its content
    never grants deletion authority: the identity must map back to the exact
    ``runs/<application_id>/<run_id>`` pathname where it was found.
    """

    if payload is None or identity_path.name != filename:
        return None
    application_id = payload.get("application_id")
    run_id = payload.get("run_id")
    task_id = payload.get("task_id")
    if not all(isinstance(value, str) for value in (application_id, run_id, task_id)):
        return None
    try:
        canonical_application = safe_application_id(application_id)
        canonical_run = validate_runtime_id(run_id, field="run_id")
        validate_runtime_id(task_id, field="task_id")
    except ValueError:
        return None
    if canonical_application != application_id or canonical_run != run_id:
        return None
    expected = runs_root / Path(*canonical_application.split("/")) / canonical_run
    if identity_path != expected / filename or not _is_within(expected, runs_root):
        return None
    return expected


def _orphan_reference_time(run_dir: Path) -> datetime | None:
    newest: float | None = None
    try:
        paths = [run_dir, *run_dir.iterdir()]
    except OSError:
        return None
    for path in paths:
        try:
            modified = path.stat(follow_symlinks=False).st_mtime
        except OSError:
            return None
        newest = modified if newest is None else max(newest, modified)
    return datetime.fromtimestamp(newest, tz=UTC) if newest is not None else None


def maybe_clean_runtime(
    runtime_root: str | Path,
    *,
    policy: RetentionPolicy | None = None,
    now: datetime | None = None,
) -> CleanupResult:
    """Run automatic cleanup no more than once per policy interval."""

    root = Path(runtime_root).expanduser()
    policy = policy or RetentionPolicy()
    current = _as_utc(now or datetime.now(UTC))
    if root.is_symlink():
        return CleanupResult(skipped=True, skip_reason="runtime root is a symlink")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    state_path = root / ".cleanup-state.json"
    lock_fd = _acquire_cleanup_lock(root)
    if lock_fd is None:
        return CleanupResult(skipped=True, skip_reason="cleanup already in progress")
    try:
        state = _read_json_object(state_path) or {}
        last_run = _parse_datetime(state.get("last_completed_at"))
        if last_run is not None:
            elapsed = current - last_run
            if timedelta(0) <= elapsed < policy.automatic_interval:
                return CleanupResult(
                    skipped=True,
                    skip_reason="automatic cleanup throttled",
                )

        result = _clean_runtime_unlocked(root, policy=policy, current=current)
        _atomic_write_json(state_path, {"last_completed_at": current.isoformat()})
        return result
    finally:
        _release_cleanup_lock(lock_fd)


def prune_runtime_if_due(
    runtime_root: str | Path,
    config: Mapping[str, Any] | RetentionPolicy | None = None,
    *,
    now: datetime | None = None,
) -> CleanupResult:
    """Canonical runner entry point for throttled automatic cleanup."""

    policy = config if isinstance(config, RetentionPolicy) else retention_policy_from_config(config)
    return maybe_clean_runtime(runtime_root, policy=policy, now=now)


def retention_policy_from_config(
    config: Mapping[str, Any] | None,
) -> RetentionPolicy:
    """Build a policy from the canonical ``runtime`` config mapping."""

    values = config or {}
    successful_days = _non_negative_number(
        values.get("successful_run_retention_days", 7),
        "successful_run_retention_days",
    )
    failed_days = _non_negative_number(
        values.get("failed_run_retention_days", 30),
        "failed_run_retention_days",
    )
    artifact_days = _non_negative_number(
        values.get("artifact_retention_days", 3),
        "artifact_retention_days",
    )
    cleanup_hours = _non_negative_number(
        values.get("cleanup_interval_hours", 24),
        "cleanup_interval_hours",
    )
    if cleanup_hours < 24:
        raise ValueError("cleanup_interval_hours must be at least 24")
    return RetentionPolicy(
        successful_runs=timedelta(days=successful_days),
        failed_runs=timedelta(days=failed_days),
        raw_artifacts=timedelta(days=artifact_days),
        automatic_interval=timedelta(hours=cleanup_hours),
    )


def _non_negative_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative number")
    try:
        converted = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative number") from exc
    if converted < 0:
        raise ValueError(f"{field} must be a non-negative number")
    return converted


def _manifest_time(manifest: dict[str, Any]) -> datetime | None:
    for key in ("finished_at", "ended_at", "updated_at", "started_at"):
        parsed = _parse_datetime(manifest.get(key))
        if parsed is not None:
            return parsed
    return None


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
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _tree_size(path: Path) -> int:
    total = 0
    try:
        entries = list(path.rglob("*"))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def _acquire_cleanup_lock(root: Path) -> int | None:
    """Lock the stable runtime directory inode without leaving lock files."""
    try:
        lock_fd = os.open(root, os.O_RDONLY)
    except OSError:
        return None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        os.close(lock_fd)
        return None

    # Remove the lock-file artifact used by the first implementation.  The
    # directory lock remains valid even when files below the root are renamed.
    try:
        (root / ".cleanup.lock").unlink()
    except (FileNotFoundError, OSError):
        pass
    return lock_fd


def _release_cleanup_lock(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
