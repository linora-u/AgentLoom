"""
File history manager for checkpoint / resume.

Ported from the reference implementation's ``fileHistory.ts``.

Provides pre-edit backup, post-step snapshots, and rewind-to-step
functionality so that file modifications made by agent tools can be
reversed when resuming from a checkpoint.

Storage layout::

    .runtime/{agent_name}/file-history/{task_id}/
        {hash}@v1          # Pre-edit backup of original file
        {hash}@v2          # Post-step snapshot
        snapshots.json     # Persistent index for resume

Usage::

    fh = FileHistoryManager(backup_dir)
    fh.track_edit("/path/to/file.py", step_number=3)   # before tool writes
    fh.make_post_step_snapshot(step_number=3)           # after step completes
    fh.rewind_to_step(step_number=3)                    # restore file state
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.lib.logging import get_logger

_logger = get_logger(__name__)

MAX_SNAPSHOTS = 100


@dataclass
class FileHistoryBackup:
    """Record of a single file backup.

    Attributes:
        backup_filename: Relative filename inside the backup directory,
            or ``None`` if the file did not exist at backup time (null-backup).
        version: Monotonically increasing version counter per tracked file.
        backup_time: Unix timestamp of when the backup was taken.
    """
    backup_filename: Optional[str]
    version: int
    backup_time: float


@dataclass
class FileHistorySnapshot:
    """A point-in-time snapshot of all tracked files at a given step.

    Attributes:
        step_number: The agent step this snapshot corresponds to.
        tracked_file_backups: Mapping of absolute file path → backup info.
        timestamp: Unix timestamp.
    """
    step_number: int
    tracked_file_backups: dict[str, FileHistoryBackup] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


def _path_hash(file_path: str) -> str:
    """Return a deterministic short hash of a file path (first 16 hex chars)."""
    return hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:16]


class FileHistoryManager:
    """Three-phase, lock-free-safe file backup manager.

    Thread safety: ``track_edit`` uses a two-phase lock pattern so that
    the expensive I/O (file copy) happens outside the critical section.
    """

    def __init__(self, backup_dir: Path | str) -> None:
        self._backup_dir = Path(backup_dir)
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._snapshots: list[FileHistorySnapshot] = []
        self._tracked_files: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def track_edit(self, file_path: str, step_number: int) -> None:
        """Record a pre-edit backup of *file_path*.

        Must be called **before** the tool actually modifies the file.
        Uses a three-phase pattern (check → I/O → commit) to minimise
        time spent under the lock.

        Args:
            file_path: Absolute path to the file about to be edited.
            step_number: Current agent step number.
        """
        abs_path = os.path.abspath(file_path)

        # Phase 1: fast check under lock.
        with self._lock:
            current = self._get_or_create_snapshot(step_number)
            if abs_path in current.tracked_file_backups:
                return  # Already backed up for this step.

        # Phase 2: expensive I/O outside the lock.
        try:
            backup = self._create_backup(abs_path, version=1)
        except Exception as exc:
            _logger.warning("FileHistory: backup failed for %s: %s", abs_path, exc)
            return

        # Phase 3: commit under lock (re-check for races).
        with self._lock:
            current = self._get_or_create_snapshot(step_number)
            if abs_path not in current.tracked_file_backups:
                current.tracked_file_backups[abs_path] = backup
                self._tracked_files.add(abs_path)

    def make_post_step_snapshot(self, step_number: int) -> None:
        """Create a post-step snapshot for all tracked files.

        Files that changed since their last backup get a new version;
        unchanged files reuse the previous backup reference.

        Old snapshots beyond ``MAX_SNAPSHOTS`` are evicted (LRU).
        """
        with self._lock:
            if not self._tracked_files:
                return
            # Get or create the snapshot for this step.
            current = self._get_or_create_snapshot(step_number)

        # I/O phase: check each tracked file.
        new_backups: dict[str, FileHistoryBackup] = {}
        for abs_path in list(self._tracked_files):
            try:
                prev_backup = current.tracked_file_backups.get(abs_path)
                next_version = (prev_backup.version + 1) if prev_backup else 1

                if not os.path.exists(abs_path):
                    # File was deleted — null-backup.
                    new_backups[abs_path] = FileHistoryBackup(
                        backup_filename=None,
                        version=next_version,
                        backup_time=time.time(),
                    )
                    continue

                # Check if file changed since last backup.
                if prev_backup and prev_backup.backup_filename is not None:
                    backup_path = self._backup_dir / prev_backup.backup_filename
                    if backup_path.exists() and not self._file_changed(abs_path, str(backup_path)):
                        # Unchanged — reuse previous backup.
                        new_backups[abs_path] = prev_backup
                        continue

                # File changed — create new backup.
                new_backups[abs_path] = self._create_backup(abs_path, next_version)
            except Exception as exc:
                _logger.warning("FileHistory: snapshot backup failed for %s: %s", abs_path, exc)

        # Commit phase.
        with self._lock:
            current = self._get_or_create_snapshot(step_number)
            current.tracked_file_backups.update(new_backups)
            current.timestamp = time.time()

            # Evict oldest snapshots.
            if len(self._snapshots) > MAX_SNAPSHOTS:
                self._snapshots = self._snapshots[-MAX_SNAPSHOTS:]

        self._persist_index()

    def rewind_to_step(self, step_number: int) -> list[str]:
        """Restore all tracked files to their state at *step_number*.

        Args:
            step_number: The step to rewind to.

        Returns:
            List of file paths that were restored.

        Raises:
            ValueError: If no snapshot exists for *step_number*.
        """
        with self._lock:
            target = None
            for snap in reversed(self._snapshots):
                if snap.step_number == step_number:
                    target = snap
                    break
            if target is None:
                raise ValueError(
                    f"No snapshot for step_number={step_number}. "
                    f"Available: {[s.step_number for s in self._snapshots]}"
                )
            # Shallow copy so mutations don't affect state.
            backups = dict(target.tracked_file_backups)

        restored: list[str] = []
        for abs_path, backup in backups.items():
            try:
                if backup.backup_filename is None:
                    # Null-backup: file didn't exist — delete it if present.
                    if os.path.exists(abs_path):
                        os.unlink(abs_path)
                        restored.append(abs_path)
                        _logger.info("FileHistory: deleted %s (didn't exist at step %d)", abs_path, step_number)
                else:
                    backup_path = self._backup_dir / backup.backup_filename
                    if not backup_path.exists():
                        _logger.warning("FileHistory: backup file missing: %s", backup_path)
                        continue
                    # Ensure parent directory exists (file may have been deleted).
                    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                    shutil.copy2(str(backup_path), abs_path)
                    restored.append(abs_path)
                    _logger.info("FileHistory: restored %s to step %d (v%d)", abs_path, step_number, backup.version)
            except Exception as exc:
                _logger.warning("FileHistory: failed to restore %s: %s", abs_path, exc)

        return restored

    def restore_from_index(self, data: dict) -> None:
        """Rebuild internal state from a persisted ``snapshots.json``.

        Called on resume to restore file history tracking state.
        """
        with self._lock:
            self._snapshots = []
            self._tracked_files = set()
            for snap_data in data.get("snapshots", []):
                backups = {}
                for path, bk in snap_data.get("tracked_file_backups", {}).items():
                    backups[path] = FileHistoryBackup(
                        backup_filename=bk.get("backup_filename"),
                        version=bk.get("version", 1),
                        backup_time=bk.get("backup_time", 0.0),
                    )
                    self._tracked_files.add(path)
                self._snapshots.append(FileHistorySnapshot(
                    step_number=snap_data.get("step_number", 0),
                    tracked_file_backups=backups,
                    timestamp=snap_data.get("timestamp", 0.0),
                ))

    @property
    def snapshot_count(self) -> int:
        """Number of snapshots currently held."""
        with self._lock:
            return len(self._snapshots)

    @property
    def tracked_file_count(self) -> int:
        """Number of distinct files being tracked."""
        with self._lock:
            return len(self._tracked_files)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_snapshot(self, step_number: int) -> FileHistorySnapshot:
        """Return the snapshot for *step_number*, creating if needed.

        Must be called under ``self._lock``.
        """
        for snap in reversed(self._snapshots):
            if snap.step_number == step_number:
                return snap
        new_snap = FileHistorySnapshot(step_number=step_number)
        self._snapshots.append(new_snap)
        return new_snap

    def _create_backup(self, file_path: str, version: int) -> FileHistoryBackup:
        """Copy *file_path* into the backup directory.

        Returns a null-backup if the source does not exist.
        """
        if not os.path.exists(file_path):
            return FileHistoryBackup(
                backup_filename=None,
                version=version,
                backup_time=time.time(),
            )

        phash = _path_hash(file_path)
        filename = f"{phash}@v{version}"
        dest = self._backup_dir / filename

        # Atomic copy via temp file.
        fd, tmp_path = tempfile.mkstemp(dir=str(self._backup_dir), prefix=f".bk_{phash}_")
        try:
            os.close(fd)
            shutil.copy2(file_path, tmp_path)
            os.replace(tmp_path, str(dest))
        except Exception:
            # Clean up temp file on failure.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return FileHistoryBackup(
            backup_filename=filename,
            version=version,
            backup_time=time.time(),
        )

    def _file_changed(self, original: str, backup: str) -> bool:
        """Check if *original* differs from *backup* by comparing content."""
        try:
            orig_stat = os.stat(original)
            bk_stat = os.stat(backup)
            # Quick size check.
            if orig_stat.st_size != bk_stat.st_size:
                return True
            # Content comparison.
            with open(original, "rb") as f1, open(backup, "rb") as f2:
                return f1.read() != f2.read()
        except OSError:
            return True

    def _persist_index(self) -> None:
        """Atomically write ``snapshots.json`` to the backup directory."""
        index_path = self._backup_dir / "snapshots.json"
        with self._lock:
            data = {
                "snapshots": [
                    {
                        "step_number": s.step_number,
                        "tracked_file_backups": {
                            path: {
                                "backup_filename": bk.backup_filename,
                                "version": bk.version,
                                "backup_time": bk.backup_time,
                            }
                            for path, bk in s.tracked_file_backups.items()
                        },
                        "timestamp": s.timestamp,
                    }
                    for s in self._snapshots
                ],
            }

        fd, tmp_path = tempfile.mkstemp(dir=str(self._backup_dir), prefix=".idx_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, str(index_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
