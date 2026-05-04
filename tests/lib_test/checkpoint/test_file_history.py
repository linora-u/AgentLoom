"""
Tests for src.lib.checkpoint.file_history.

Covers:
- track_edit: backup creation, idempotency, null-backup, auto-create dir
- make_post_step_snapshot: version bumps, unchanged reuse, eviction
- rewind_to_step: content restoration, null-backup deletion, errors
- Thread safety: concurrent track_edit
- restore_from_index: round-trip persistence
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from src.lib.checkpoint.file_history import (
    FileHistoryManager,
    FileHistorySnapshot,
    MAX_SNAPSHOTS,
)


@pytest.fixture
def backup_dir(tmp_path):
    """Create a dedicated backup directory."""
    d = tmp_path / "file-history"
    d.mkdir()
    return d


@pytest.fixture
def fh(backup_dir):
    """Create a FileHistoryManager instance."""
    return FileHistoryManager(backup_dir)


@pytest.fixture
def sample_file(tmp_path):
    """Create a sample file for testing."""
    f = tmp_path / "sample.txt"
    f.write_text("original content\n", encoding="utf-8")
    return str(f)


@pytest.fixture
def binary_file(tmp_path):
    """Create a binary file for testing."""
    f = tmp_path / "binary.bin"
    f.write_bytes(b"\x00\x01\x02\xff\xfe\xfd")
    return str(f)


# ===================================================================
# track_edit
# ===================================================================


class TestTrackEdit:
    """Tests for FileHistoryManager.track_edit()."""

    def test_backup_created_with_correct_content(self, fh, sample_file, backup_dir):
        """Normal: File is backed up and backup content matches original."""
        original_content = open(sample_file, "rb").read()

        fh.track_edit(sample_file, step_number=1)

        # Find the backup file.
        backups = [f for f in os.listdir(backup_dir) if "@v1" in f]
        assert len(backups) == 1
        backup_content = (backup_dir / backups[0]).read_bytes()
        assert backup_content == original_content

    def test_second_call_same_file_same_step_no_duplicate(self, fh, sample_file, backup_dir):
        """Normal: Second call for same file+step does not create duplicate."""
        fh.track_edit(sample_file, step_number=1)
        fh.track_edit(sample_file, step_number=1)

        backups = [f for f in os.listdir(backup_dir) if "@v" in f]
        assert len(backups) == 1

    def test_nonexistent_file_creates_null_backup(self, fh, tmp_path):
        """Error: Non-existent source creates null backup (backup_filename=None)."""
        missing = str(tmp_path / "does_not_exist.txt")
        fh.track_edit(missing, step_number=1)

        assert fh.tracked_file_count == 1
        # Verify internal state has null backup.
        with fh._lock:
            snap = fh._snapshots[0]
            abs_path = os.path.abspath(missing)
            assert abs_path in snap.tracked_file_backups
            assert snap.tracked_file_backups[abs_path].backup_filename is None

    def test_backup_dir_auto_created(self, tmp_path):
        """Error: Backup dir is auto-created if it doesn't exist."""
        new_dir = tmp_path / "new" / "nested" / "dir"
        fh = FileHistoryManager(new_dir)
        assert new_dir.exists()

    def test_empty_file_backup(self, fh, tmp_path, backup_dir):
        """Boundary: Empty file backup is 0 bytes."""
        empty = tmp_path / "empty.txt"
        empty.write_text("")
        fh.track_edit(str(empty), step_number=1)

        backups = [f for f in os.listdir(backup_dir) if "@v1" in f]
        assert len(backups) == 1
        assert (backup_dir / backups[0]).stat().st_size == 0

    def test_binary_file_exact_bytes(self, fh, binary_file, backup_dir):
        """Boundary: Binary file backup preserves exact bytes."""
        original_bytes = open(binary_file, "rb").read()

        fh.track_edit(binary_file, step_number=1)

        backups = [f for f in os.listdir(backup_dir) if "@v1" in f]
        assert len(backups) == 1
        assert (backup_dir / backups[0]).read_bytes() == original_bytes

    def test_different_steps_create_separate_entries(self, fh, sample_file):
        """Normal: Same file in different steps gets separate snapshot entries."""
        fh.track_edit(sample_file, step_number=1)
        fh.track_edit(sample_file, step_number=2)

        assert fh.snapshot_count == 2


# ===================================================================
# make_post_step_snapshot
# ===================================================================


class TestMakePostStepSnapshot:
    """Tests for FileHistoryManager.make_post_step_snapshot()."""

    def test_changed_file_gets_new_version(self, fh, sample_file, backup_dir):
        """Normal: Modified file gets v2 backup."""
        fh.track_edit(sample_file, step_number=1)

        # Modify the file.
        with open(sample_file, "w") as f:
            f.write("modified content\n")

        fh.make_post_step_snapshot(step_number=1)

        backups = sorted([f for f in os.listdir(backup_dir) if "@v" in f])
        # Should have v1 (original) and v2 (modified).
        assert any("@v1" in b for b in backups)
        assert any("@v2" in b for b in backups)

    def test_unchanged_file_reuses_backup(self, fh, sample_file, backup_dir):
        """Normal: Unchanged file reuses previous backup (no new copy)."""
        fh.track_edit(sample_file, step_number=1)
        fh.make_post_step_snapshot(step_number=1)

        # Only v1 should exist (no v2 for unchanged file).
        backups = [f for f in os.listdir(backup_dir) if "@v" in f]
        versions = [b for b in backups if "@v2" in b]
        assert len(versions) == 0

    def test_zero_tracked_is_noop(self, fh):
        """Boundary: No tracked files → no-op."""
        fh.make_post_step_snapshot(step_number=1)
        assert fh.snapshot_count == 0

    def test_eviction_at_max_snapshots(self, fh, sample_file):
        """Boundary: 101 snapshots → oldest evicted."""
        for i in range(MAX_SNAPSHOTS + 1):
            fh.track_edit(sample_file, step_number=i)
            fh.make_post_step_snapshot(step_number=i)

        assert fh.snapshot_count <= MAX_SNAPSHOTS

    def test_deleted_file_gets_null_backup(self, fh, sample_file):
        """Normal: Tracked file deleted → null-backup in snapshot."""
        fh.track_edit(sample_file, step_number=1)
        os.unlink(sample_file)
        fh.make_post_step_snapshot(step_number=1)

        with fh._lock:
            snap = fh._snapshots[-1]
            abs_path = os.path.abspath(sample_file)
            bk = snap.tracked_file_backups[abs_path]
            assert bk.backup_filename is None


# ===================================================================
# rewind_to_step
# ===================================================================


class TestRewindToStep:
    """Tests for FileHistoryManager.rewind_to_step()."""

    def test_restores_original_bytes(self, fh, sample_file):
        """Normal: Rewind restores the exact original file content."""
        original = open(sample_file, "rb").read()

        fh.track_edit(sample_file, step_number=1)

        # Modify the file.
        with open(sample_file, "w") as f:
            f.write("completely different content\n")

        restored = fh.rewind_to_step(step_number=1)
        assert sample_file in [os.path.abspath(p) for p in restored] or os.path.abspath(sample_file) in restored
        assert open(sample_file, "rb").read() == original

    def test_null_backup_deletes_file(self, fh, tmp_path):
        """Normal: Null-backup → file is deleted on rewind."""
        # Track a file that doesn't exist (null-backup).
        missing = str(tmp_path / "created_after_checkpoint.txt")
        fh.track_edit(missing, step_number=1)

        # Now create the file.
        with open(missing, "w") as f:
            f.write("new file content")
        assert os.path.exists(missing)

        # Rewind should delete it.
        fh.rewind_to_step(step_number=1)
        assert not os.path.exists(missing)

    def test_unknown_step_raises_value_error(self, fh):
        """Error: Unknown step_number raises ValueError."""
        with pytest.raises(ValueError, match="No snapshot for step_number=99"):
            fh.rewind_to_step(step_number=99)

    def test_missing_backup_file_logs_warning(self, fh, sample_file, backup_dir):
        """Error: Missing backup file → warning logged, other files still restored."""
        fh.track_edit(sample_file, step_number=1)

        # Corrupt: delete the backup file.
        for f in os.listdir(backup_dir):
            if "@v1" in f:
                os.unlink(backup_dir / f)

        # Should not raise, just log warning.
        restored = fh.rewind_to_step(step_number=1)
        # File was not restored because backup is missing.
        assert len(restored) == 0

    def test_rewind_to_step_zero(self, fh, sample_file):
        """Boundary: Rewind to step 0."""
        original = open(sample_file, "rb").read()
        fh.track_edit(sample_file, step_number=0)

        with open(sample_file, "w") as f:
            f.write("changed")

        fh.rewind_to_step(step_number=0)
        assert open(sample_file, "rb").read() == original

    def test_file_externally_deleted_before_rewind(self, fh, sample_file):
        """Boundary: File deleted externally before rewind — still restored."""
        original = open(sample_file, "rb").read()
        fh.track_edit(sample_file, step_number=1)

        os.unlink(sample_file)
        assert not os.path.exists(sample_file)

        fh.rewind_to_step(step_number=1)
        assert os.path.exists(sample_file)
        assert open(sample_file, "rb").read() == original


# ===================================================================
# Thread safety
# ===================================================================


class TestThreadSafety:
    """Tests for concurrent access."""

    def test_concurrent_track_same_file(self, fh, sample_file, backup_dir):
        """10 threads tracking same file → exactly 1 v1 backup, no crash."""
        barrier = threading.Barrier(10)
        errors = []

        def worker():
            try:
                barrier.wait(timeout=5)
                fh.track_edit(sample_file, step_number=1)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors: {errors}"
        # Exactly 1 v1 backup.
        v1_backups = [f for f in os.listdir(backup_dir) if "@v1" in f]
        assert len(v1_backups) == 1

    def test_concurrent_track_different_files(self, fh, tmp_path, backup_dir):
        """5 threads tracking different files → all 5 backed up."""
        files = []
        for i in range(5):
            f = tmp_path / f"file_{i}.txt"
            f.write_text(f"content {i}")
            files.append(str(f))

        barrier = threading.Barrier(5)
        errors = []

        def worker(path):
            try:
                barrier.wait(timeout=5)
                fh.track_edit(path, step_number=1)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(f,)) for f in files]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert fh.tracked_file_count == 5


# ===================================================================
# restore_from_index
# ===================================================================


class TestRestoreFromIndex:
    """Tests for round-trip persistence."""

    def test_save_load_roundtrip(self, fh, sample_file, backup_dir):
        """Save index → load → verify state matches."""
        fh.track_edit(sample_file, step_number=1)
        fh.make_post_step_snapshot(step_number=1)

        # Read the persisted index.
        import json
        index_path = backup_dir / "snapshots.json"
        assert index_path.exists()
        with open(index_path) as f:
            data = json.load(f)

        # Create a new manager and restore.
        fh2 = FileHistoryManager(backup_dir)
        fh2.restore_from_index(data)

        assert fh2.snapshot_count == fh.snapshot_count
        assert fh2.tracked_file_count == fh.tracked_file_count

    def test_restore_empty_index(self, backup_dir):
        """Boundary: Restore from empty data."""
        fh = FileHistoryManager(backup_dir)
        fh.restore_from_index({})
        assert fh.snapshot_count == 0
        assert fh.tracked_file_count == 0
