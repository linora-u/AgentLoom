"""
Tests for thread-safe task_tree operations in CheckpointManager.

Verifies that concurrent save_task_tree / load_task_tree calls
from multiple worker threads do not cause data loss or corruption.
"""
from __future__ import annotations

import threading

import pytest

from src.lib.checkpoint.checkpoint_manager import CheckpointManager


@pytest.fixture
def cm(tmp_path):
    """Create a CheckpointManager with a temp base dir."""
    return CheckpointManager("test_supervisor", base_dir=tmp_path)


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
