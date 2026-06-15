"""
Tests for thread-safe task_tree operations in CheckpointManager.

Verifies that concurrent save_task_tree / load_task_tree calls
from multiple worker threads do not cause data loss or corruption.
"""
from __future__ import annotations

import threading

import pytest

from src.lib.checkpoint.checkpoint_manager import CheckpointManager
from src.lib.checkpoint.coordinator import CheckpointCoordinator
from src.lib.checkpoint.file_history import FileHistoryManager
from src.lib.smolagents.hooks.hook_manager import HookManager
from src.lib.smolagents.hooks.types import HookEvent


@pytest.fixture
def cm(tmp_path):
    """Create a CheckpointManager with a temp base dir."""
    return CheckpointManager("test_supervisor", base_dir=tmp_path)


class TestCoordinatorFallback:
    """Tests for tool-executor contexts that do not inherit ContextVar state."""

    def test_current_visible_without_context_copy_until_deactivated(self, tmp_path):
        cm = CheckpointManager("test_supervisor", base_dir=tmp_path)
        coord = CheckpointCoordinator.activate(cm, "task_ctx", "task text")

        try:
            seen = []

            def worker():
                seen.append(CheckpointCoordinator.current())

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=5)

            assert seen == [coord]

            CheckpointCoordinator.deactivate(coord)
            seen_after = []
            thread = threading.Thread(
                target=lambda: seen_after.append(CheckpointCoordinator.current())
            )
            thread.start()
            thread.join(timeout=5)

            assert seen_after == [None]
        finally:
            CheckpointCoordinator.deactivate(coord)

    def test_register_file_history_hook_uses_agent_hook_manager(self, tmp_path):
        cm = CheckpointManager("test_supervisor", base_dir=tmp_path)
        coord = CheckpointCoordinator.activate(cm, "task_ctx", "task text")
        fh = FileHistoryManager(tmp_path / "fh")
        hook_manager = HookManager()
        try:
            coord._file_history = fh

            coord.register_file_history_hook(hook_manager)
            coord.register_file_history_hook(hook_manager)

            hooks = hook_manager.hooks[HookEvent.PRE_TOOL_USE]
            file_history_hooks = [
                h for h in hooks if h.get("source") == "file_history"
            ]
            assert len(file_history_hooks) == 1
        finally:
            CheckpointCoordinator.deactivate(coord)


class TestTreeLock:
    """Tests for concurrent task_tree access."""

    def test_concurrent_writers_no_data_loss(self, cm):
        """5 concurrent workers updating task_tree → no data loss."""
        task_id = "task_concurrent_test"
        cm.save_task_tree(task_id, {
            "task_id": task_id,
            "status": "running",
            "workers": {},
        })

        errors = []
        barrier = threading.Barrier(5)

        def worker(worker_idx):
            try:
                barrier.wait(timeout=5)
                for i in range(10):
                    def updater(tree, *, widx=worker_idx, cidx=i):
                        workers = tree.get("workers", {})
                        workers[f"worker_{widx}"] = [{
                            "call_index": cidx,
                            "status": "running",
                            "input_hash": f"hash_{widx}_{cidx}",
                        }]
                        tree["workers"] = workers
                        return tree
                    cm.update_task_tree(task_id, updater)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Errors: {errors}"

        # All 5 workers should be present in the final tree.
        final = cm.load_task_tree(task_id)
        assert final is not None
        assert len(final["workers"]) == 5
        for i in range(5):
            assert f"worker_{i}" in final["workers"]

    def test_save_load_roundtrip_under_lock(self, cm):
        """Basic roundtrip with lock protection."""
        task_id = "task_lock_test"
        tree = {"task_id": task_id, "status": "running"}
        cm.save_task_tree(task_id, tree)
        loaded = cm.load_task_tree(task_id)
        assert loaded["task_id"] == task_id
        assert loaded["status"] == "running"

    def test_concurrent_worker_start_allocates_unique_call_indexes(self, cm):
        """Concurrent same-name worker starts get unique contiguous call indexes."""
        task_id = "task_worker_start_race"
        cm.record_task_created(
            task_id,
            yaml_path="applications/demo/workflows/agent.yaml",
            agent_name="test_supervisor",
            task_text="parallel work",
            created_at="2026-06-15T12:00:00+08:00",
        )

        worker_count = 50
        barrier = threading.Barrier(worker_count)
        indexes = []
        index_lock = threading.Lock()
        errors = []

        def worker(worker_idx):
            try:
                barrier.wait(timeout=10)
                call_index = cm.record_worker_started(
                    task_id,
                    "same_worker",
                    input_hash=f"hash-{worker_idx}",
                    task_input=f"task-{worker_idx}",
                )
                with index_lock:
                    indexes.append(call_index)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(worker_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Errors: {errors}"
        assert sorted(indexes) == list(range(worker_count))
        calls = cm.load_task_tree(task_id)["workers"]["same_worker"]
        assert [c["call_index"] for c in calls] == list(range(worker_count))

    def test_worker_finish_updates_only_matching_call(self, cm):
        """Finishing one worker call does not overwrite sibling calls."""
        task_id = "task_worker_finish_isolated"
        cm.save_task_tree(task_id, {"task_id": task_id, "status": "running", "workers": {}})
        c0 = cm.record_worker_started(task_id, "same_worker", input_hash="h0", task_input="first")
        c1 = cm.record_worker_started(task_id, "same_worker", input_hash="h1", task_input="second")

        cm.record_worker_finished(task_id, "same_worker", call_index=c1, status="completed", result="second")

        calls = cm.load_task_tree(task_id)["workers"]["same_worker"]
        by_index = {c["call_index"]: c for c in calls}
        assert by_index[c0]["status"] == "running"
        assert by_index[c0]["result"] is None
        assert by_index[c1]["status"] == "completed"
        assert by_index[c1]["result"] == "second"
