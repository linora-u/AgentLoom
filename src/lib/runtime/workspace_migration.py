"""One-time archival of the unscoped legacy ``.runtime`` workspace.

Legacy workspace files contain agent names but no application or task
identity.  Assigning them to a live canonical workspace would therefore
fabricate provenance.  The migration preserves the complete tree under an
explicit ``legacy-unscoped`` namespace and removes the old top-level root.
"""

from __future__ import annotations

import fcntl
import os
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .context import _FILE_NOFOLLOW, _open_runtime_directory


@dataclass(frozen=True, slots=True)
class LegacyWorkspaceMigrationResult:
    source_dir: Path
    file_count: int
    total_bytes: int
    archive_dir: Path | None = None


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def preview_legacy_agent_workspaces(
    legacy_workspace_dir: str | Path,
) -> LegacyWorkspaceMigrationResult:
    """Count the legacy tree without changing it."""

    source = _absolute(legacy_workspace_dir)
    if not source.exists():
        return LegacyWorkspaceMigrationResult(source, 0, 0)
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"legacy_workspace_dir must be a real directory: {source}")

    file_count = 0
    total_bytes = 0
    for root, directories, files in os.walk(source, followlinks=False):
        root_path = Path(root)
        directories[:] = [
            name for name in directories if not (root_path / name).is_symlink()
        ]
        for name in files:
            path = root_path / name
            file_count += 1
            try:
                total_bytes += path.lstat().st_size
            except OSError:
                continue
    return LegacyWorkspaceMigrationResult(source, file_count, total_bytes)


def archive_legacy_agent_workspaces(
    legacy_workspace_dir: str | Path,
    runtime_root: str | Path,
    *,
    now: datetime | None = None,
) -> LegacyWorkspaceMigrationResult:
    """Atomically move an unscoped legacy tree beneath runtime workspaces."""

    source = _absolute(legacy_workspace_dir)
    root = _absolute(runtime_root)
    if _is_relative_to(root, source):
        raise ValueError("runtime_root must not be inside legacy_workspace_dir")

    if not source.exists():
        return LegacyWorkspaceMigrationResult(source, 0, 0)

    archive_root = root / "workspaces" / "legacy-unscoped"
    workspaces_fd = _open_runtime_directory(
        root / "workspaces",
        root=root,
        create=True,
    )
    archive_fd = -1
    lock_fd = -1
    try:
        try:
            os.mkdir("legacy-unscoped", mode=0o700, dir_fd=workspaces_fd)
        except FileExistsError:
            pass
        archive_fd = os.open(
            "legacy-unscoped",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=workspaces_fd,
        )
        lock_fd = os.open(
            ".legacy-workspace-migration.lock",
            os.O_RDWR | os.O_CREAT | _FILE_NOFOLLOW,
            0o600,
            dir_fd=workspaces_fd,
        )
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise RuntimeError("legacy workspace migration lock is not a regular file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if not source.exists():
            return LegacyWorkspaceMigrationResult(source, 0, 0)
        if source.is_symlink() or not source.is_dir():
            raise ValueError(f"legacy_workspace_dir must be a real directory: {source}")

        preview = preview_legacy_agent_workspaces(source)
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        destination_name = f"workspace-v1-{timestamp}-{uuid.uuid4().hex[:8]}"
        destination = archive_root / destination_name
        try:
            os.replace(source, destination_name, dst_dir_fd=archive_fd)
        except OSError as exc:
            raise RuntimeError(
                f"failed to atomically archive legacy workspace {source}: {exc}"
            ) from exc
        return LegacyWorkspaceMigrationResult(
            source_dir=source,
            file_count=preview.file_count,
            total_bytes=preview.total_bytes,
            archive_dir=destination,
        )
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        if archive_fd >= 0:
            os.close(archive_fd)
        os.close(workspaces_fd)
