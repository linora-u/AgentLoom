"""Tests for ``src.lib.checkpoint.checkpoint_manager.CheckpointManager``."""

from __future__ import annotations

import json
import time
import threading
from pathlib import Path

import pytest

from src.lib.checkpoint.checkpoint_manager import CheckpointManager, list_all_tasks


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def cm(tmp_path: Path) -> CheckpointManager:
    """A CheckpointManager rooted in a temp directory."""
    return CheckpointManager("test_supervisor", base_dir=tmp_path)


@pytest.fixture()
def task_id() -> str:
    return "task_1234567890"


# ── task tree ────────────────────────────────────────────────────────────


class TestTaskTree:

    def test_save_load(self, cm: CheckpointManager, task_id: str):
        tree = {"task_id": task_id, "agent_name": "test_supervisor", "status": "running", "workers": {}}
        cm.save_task_tree(task_id, tree)
        loaded = cm.load_task_tree(task_id)
        assert loaded is not None
        assert loaded["task_id"] == task_id
        assert loaded["status"] == "running"

    def test_load_nonexistent(self, cm: CheckpointManager):
        assert cm.load_task_tree("nonexistent") is None

    def test_overwrite(self, cm: CheckpointManager, task_id: str):
        cm.save_task_tree(task_id, {"status": "running"})
        cm.save_task_tree(task_id, {"status": "interrupted"})
        loaded = cm.load_task_tree(task_id)
        assert loaded["status"] == "interrupted"


# ── supervisor checkpoint ────────────────────────────────────────────────


class TestSupervisorCheckpoint:

    def test_save_load(self, cm: CheckpointManager, task_id: str):
        from smolagents.memory import TaskStep, ActionStep
        from smolagents.monitoring import Timing
        steps = [
            TaskStep(task="test task"),
            ActionStep(step_number=1, timing=Timing(start_time=time.time()), observations="ok"),
        ]
        cm.save_supervisor_checkpoint(
            task_id,
            memory_steps=steps,
            task_text="test task",
            status="interrupted",
        )
        loaded = cm.load_supervisor_checkpoint(task_id)
        assert loaded is not None
        assert loaded["status"] == "interrupted"
        assert loaded["step_count"] == 2
        assert len(loaded["memory_steps"]) == 2

    def test_load_nonexistent(self, cm: CheckpointManager, task_id: str):
        assert cm.load_supervisor_checkpoint(task_id) is None


# ── worker checkpoint ────────────────────────────────────────────────────


class TestWorkerCheckpoint:

    def test_save_load(self, cm: CheckpointManager, task_id: str):
        cm.save_worker_checkpoint(
            task_id, "search_worker",
            task_input="scan src/",
            status="completed",
            result="42 files found",
        )
        loaded = cm.load_worker_checkpoint(task_id, "search_worker")
        assert loaded is not None
        assert loaded["status"] == "completed"
        assert loaded["result"] == "42 files found"

    def test_multiple_workers(self, cm: CheckpointManager, task_id: str):
        cm.save_worker_checkpoint(task_id, "w1", status="completed", result="done1")
        cm.save_worker_checkpoint(task_id, "w2", status="failed", error="timeout")
        cm.save_worker_checkpoint(task_id, "w3", status="interrupted")

        assert cm.load_worker_checkpoint(task_id, "w1")["status"] == "completed"
        assert cm.load_worker_checkpoint(task_id, "w2")["status"] == "failed"
        assert cm.load_worker_checkpoint(task_id, "w3")["status"] == "interrupted"


# ── listing ──────────────────────────────────────────────────────────────


class TestListAndCleanup:

    def test_list_tasks(self, cm: CheckpointManager):
        for i in range(3):
            tid = f"task_{i}"
            cm.save_task_tree(tid, {"task_id": tid, "agent_name": "test_supervisor", "status": "interrupted"})
        tasks = cm.list_tasks()
        assert len(tasks) == 3
        assert all(t["status"] == "interrupted" for t in tasks)

    def test_list_empty(self, cm: CheckpointManager):
        assert cm.list_tasks() == []

    def test_delete_task(self, cm: CheckpointManager, task_id: str):
        cm.save_task_tree(task_id, {"status": "running"})
        assert cm.delete_task(task_id) is True
        assert cm.load_task_tree(task_id) is None
        assert cm.delete_task(task_id) is False

    def test_cleanup_old(self, cm: CheckpointManager):
        old_tid = "task_old"
        new_tid = "task_new"
        cm.save_task_tree(old_tid, {"task_id": old_tid, "status": "interrupted"})
        cm.save_task_tree(new_tid, {"task_id": new_tid, "status": "interrupted"})

        # Backdate the old task directory's mtime.
        import os
        old_dir = cm._task_dir(old_tid)
        old_time = time.time() - 100
        os.utime(old_dir, (old_time, old_time))

        removed = cm.cleanup_old_tasks(max_age_seconds=50)
        assert removed == 1
        assert cm.load_task_tree(old_tid) is None
        assert cm.load_task_tree(new_tid) is not None


# ── cross-supervisor listing ─────────────────────────────────────────────


class TestListAllTasks:

    def test_scans_all_supervisors(self, tmp_path: Path):
        cm1 = CheckpointManager("sup_a", base_dir=tmp_path)
        cm2 = CheckpointManager("sup_b", base_dir=tmp_path)
        cm1.save_task_tree("t1", {"task_id": "t1", "agent_name": "sup_a", "status": "interrupted"})
        cm2.save_task_tree("t2", {"task_id": "t2", "agent_name": "sup_b", "status": "failed"})

        all_tasks = list_all_tasks(base_dir=tmp_path)
        assert len(all_tasks) == 2
        names = {t["agent_name"] for t in all_tasks}
        assert names == {"sup_a", "sup_b"}


# ── atomic write safety ─────────────────────────────────────────────────


class TestAtomicWrite:

    def test_concurrent_writes(self, cm: CheckpointManager, task_id: str):
        """Multiple threads writing the same tree should not corrupt the file."""
        errors = []

        def _writer(i: int):
            try:
                cm.save_task_tree(task_id, {"task_id": task_id, "writer": i})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        loaded = cm.load_task_tree(task_id)
        assert loaded is not None
        assert "writer" in loaded
