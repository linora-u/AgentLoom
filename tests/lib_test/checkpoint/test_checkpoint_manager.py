"""Tests for ``src.lib.checkpoint.checkpoint_manager.CheckpointManager``."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.lib.checkpoint.checkpoint_manager import (
    CheckpointManager,
    cleanup_expired_tasks,
    list_all_tasks,
)

# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def cm(tmp_path: Path) -> CheckpointManager:
    """A CheckpointManager rooted in a temp directory."""
    return CheckpointManager(
        "test_supervisor",
        checkpoints_root=tmp_path,
        run_id="run_test",
    )


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


class TestTaskEvents:

    def test_event_log_rebuilds_task_tree(self, cm: CheckpointManager):
        task_id = "task_events"
        cm.record_task_created(
            task_id,
            yaml_path="applications/demo/workflows/agent.yaml",
            agent_name="test_supervisor",
            task_text="do work",
            created_at="2026-06-15T12:00:00+08:00",
        )
        call_index = cm.record_worker_started(
            task_id,
            "worker_a",
            input_hash="hash-a",
            task_input="scan files",
        )
        cm.record_worker_finished(
            task_id,
            "worker_a",
            call_index=call_index,
            status="completed",
            input_hash="hash-a",
            task_input="scan files",
            result="done",
        )
        cm.record_task_status_changed(task_id, "completed", result="ok")

        event_path = cm._task_events_path(task_id)
        assert event_path.exists()

        # Remove the compatibility projection; load_task_tree must replay events.
        cm._task_tree_path(task_id).unlink()
        loaded = cm.load_task_tree(task_id)

        assert loaded["task_id"] == task_id
        assert loaded["status"] == "completed"
        assert loaded["result"] == "ok"
        assert loaded["workers"]["worker_a"][0]["call_index"] == 0
        assert loaded["workers"]["worker_a"][0]["status"] == "completed"
        assert loaded["workers"]["worker_a"][0]["result"] == "done"

    def test_legacy_task_tree_without_events_still_loads(self, cm: CheckpointManager):
        task_id = "task_legacy_only"
        path = cm._task_tree_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "agent_name": "test_supervisor",
                    "status": "interrupted",
                    "workers": {"legacy_worker": {"status": "completed"}},
                }
            ),
            encoding="utf-8",
        )

        loaded = cm.load_task_tree(task_id)
        assert loaded["status"] == "interrupted"
        assert isinstance(loaded["workers"]["legacy_worker"], list)
        assert loaded["workers"]["legacy_worker"][0]["call_index"] == 0

    def test_malformed_event_line_is_skipped(self, cm: CheckpointManager):
        task_id = "task_malformed_events"
        event_path = cm._task_events_path(task_id)
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "task_created",
                            "task_id": task_id,
                            "yaml_path": "app.yaml",
                            "agent_name": "test_supervisor",
                            "task_text": "task",
                            "created_at": "2026-06-15T12:00:00+08:00",
                        }
                    ),
                    "{not valid json",
                    json.dumps(
                        {
                            "type": "worker_call_started",
                            "agent_name": "worker_a",
                            "call_index": 0,
                            "input_hash": "h",
                            "task_input": "input",
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )

        loaded = cm.load_task_tree(task_id)
        assert loaded["task_id"] == task_id
        assert loaded["workers"]["worker_a"][0]["status"] == "running"

    def test_append_after_partial_event_tail_keeps_new_event_readable(self, cm: CheckpointManager):
        task_id = "task_partial_tail"
        event_path = cm._task_events_path(task_id)
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_path.write_text('{"type": "task_created"', encoding="utf-8")

        cm.record_task_created(
            task_id,
            yaml_path="app.yaml",
            agent_name="test_supervisor",
            task_text="task",
            created_at="2026-06-15T12:00:00+08:00",
        )

        loaded = cm.load_task_tree(task_id)
        assert loaded["task_id"] == task_id
        assert loaded["agent_name"] == "test_supervisor"

    def test_invalid_utf8_event_tail_preserves_complete_events(self, cm: CheckpointManager):
        task_id = "task_invalid_utf8_tail"
        cm.record_task_created(
            task_id,
            yaml_path="app.yaml",
            agent_name="test_supervisor",
            task_text="中文任务",
            created_at="2026-06-15T12:00:00+08:00",
        )
        event_path = cm._task_events_path(task_id)
        with event_path.open("ab") as stream:
            stream.write(b'{"type":"task_status_changed","result":"\xe4')

        loaded = cm.load_task_tree(task_id)

        assert loaded is not None
        assert loaded["task_id"] == task_id
        assert loaded["task_text"] == "中文任务"

    def test_valid_event_after_invalid_utf8_tail_is_recovered(self, cm: CheckpointManager):
        task_id = "task_invalid_utf8_then_append"
        event_path = cm._task_events_path(task_id)
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_path.write_bytes(b'{"type":"task_created","task_id":"broken\xe4')

        cm.record_task_created(
            task_id,
            yaml_path="app.yaml",
            agent_name="test_supervisor",
            task_text="recovered",
            created_at="2026-06-15T12:00:00+08:00",
        )

        loaded = cm.load_task_tree(task_id)
        assert loaded is not None
        assert loaded["task_text"] == "recovered"


# ── supervisor checkpoint ────────────────────────────────────────────────


class TestSupervisorCheckpoint:

    def test_save_load(self, cm: CheckpointManager, task_id: str):
        from smolagents.memory import ActionStep, TaskStep
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

    def test_bound_manager_rejects_a_different_task(self, tmp_path):
        task_dir = tmp_path / "checkpoints" / "app" / "task_bound"
        cm = CheckpointManager(
            "test_supervisor",
            checkpoint_dir=task_dir,
            run_id="run_test",
        )

        with pytest.raises(ValueError, match="bound to task"):
            cm.save_task_tree("task_other", {"status": "running"})


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

    def test_worker_checkpoints_are_per_call_and_latest_default(self, cm: CheckpointManager, task_id: str):
        cm.save_task_tree(task_id, {"task_id": task_id, "agent_name": "sup", "status": "running", "workers": {}})
        c0 = cm.record_worker_started(task_id, "repeat_worker", input_hash="h0", task_input="first")
        cm.save_worker_checkpoint(task_id, "repeat_worker", call_index=c0, status="completed", result="first")
        cm.record_worker_finished(task_id, "repeat_worker", call_index=c0, status="completed", result="first")

        c1 = cm.record_worker_started(task_id, "repeat_worker", input_hash="h1", task_input="second")
        cm.save_worker_checkpoint(task_id, "repeat_worker", call_index=c1, status="completed", result="second")
        cm.record_worker_finished(task_id, "repeat_worker", call_index=c1, status="completed", result="second")

        assert c0 == 0
        assert c1 == 1
        assert cm.load_worker_checkpoint(task_id, "repeat_worker", call_index=0)["result"] == "first"
        assert cm.load_worker_checkpoint(task_id, "repeat_worker", call_index=1)["result"] == "second"
        assert cm.load_worker_checkpoint(task_id, "repeat_worker")["result"] == "second"
        assert cm._worker_call_ckpt(task_id, "repeat_worker", 0).exists()
        assert cm._worker_call_ckpt(task_id, "repeat_worker", 1).exists()

    def test_worker_start_reuses_incomplete_call_only_when_requested(self, cm: CheckpointManager, task_id: str):
        cm.save_task_tree(task_id, {"task_id": task_id, "agent_name": "sup", "status": "interrupted", "workers": {}})
        c0 = cm.record_worker_started(task_id, "resume_worker", input_hash="same", task_input="first")
        cm.record_worker_finished(
            task_id,
            "resume_worker",
            call_index=c0,
            status="interrupted",
            input_hash="same",
            task_input="first",
        )

        c1 = cm.record_worker_started(
            task_id,
            "resume_worker",
            input_hash="same",
            task_input="normal retry",
        )
        resumed_cm = CheckpointManager(
            "test_supervisor",
            checkpoint_dir=cm._task_dir(task_id),
            run_id="run_resume",
        )
        reused = resumed_cm.record_worker_started(
            task_id,
            "resume_worker",
            input_hash="same",
            task_input="resume retry",
            reuse_incomplete=True,
        )

        assert c0 == 0
        assert c1 == 1
        assert reused == 0
        calls = resumed_cm.load_task_tree(task_id)["workers"]["resume_worker"]
        assert [c["call_index"] for c in calls] == [0, 1]


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
        now = datetime.now(UTC)
        cm.save_task_tree(
            old_tid,
            {
                "task_id": old_tid,
                "status": "interrupted",
                "created_at": (now - timedelta(seconds=100)).isoformat(),
            },
        )
        cm.save_task_tree(
            new_tid,
            {
                "task_id": new_tid,
                "status": "interrupted",
                "created_at": now.isoformat(),
            },
        )

        removed = cleanup_expired_tasks(
            checkpoints_root=cm._checkpoints_root,
            max_age_seconds=50,
            now=now,
        )
        assert removed == 1
        assert cm.load_task_tree(old_tid) is None
        assert cm.load_task_tree(new_tid) is not None


# ── cross-supervisor listing ─────────────────────────────────────────────


class TestListAllTasks:

    def test_scans_all_supervisors(self, tmp_path: Path):
        app_a = tmp_path / "sup_a"
        app_b = tmp_path / "sup_b"
        cm1 = CheckpointManager("sup_a", checkpoints_root=app_a)
        cm2 = CheckpointManager("sup_b", checkpoints_root=app_b)
        cm1.save_task_tree("t1", {"task_id": "t1", "agent_name": "sup_a", "status": "interrupted"})
        cm2.save_task_tree("t2", {"task_id": "t2", "agent_name": "sup_b", "status": "failed"})

        all_tasks = list_all_tasks(checkpoints_root=tmp_path)
        assert len(all_tasks) == 2
        names = {t["agent_name"] for t in all_tasks}
        assert names == {"sup_a", "sup_b"}


# ── atomic write safety ─────────────────────────────────────────────────


class TestAtomicWrite:

    def test_failed_replace_does_not_leave_temporary_file(self, tmp_path, monkeypatch):
        target = tmp_path / "task_tree.json"

        def fail_replace(*_args, **_kwargs):
            raise OSError("replace failed")

        monkeypatch.setattr(os, "replace", fail_replace)

        with pytest.raises(OSError, match="replace failed"):
            CheckpointManager._atomic_write(target, {"status": "running"})

        assert list(tmp_path.glob("*.tmp")) == []

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
