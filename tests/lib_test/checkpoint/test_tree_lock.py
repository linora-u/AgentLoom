"""
Tests for thread-safe task_tree operations in CheckpointManager.

Verifies that concurrent save_task_tree / load_task_tree calls
from multiple worker threads do not cause data loss or corruption.
"""
from __future__ import annotations

import contextvars
import json
import threading

import pytest

from src.lib.checkpoint.checkpoint_manager import CheckpointManager
from src.lib.checkpoint.coordinator import CheckpointCoordinator


@pytest.fixture
def cm(tmp_path):
    """Create a CheckpointManager with a temp base dir."""
    return CheckpointManager(
        "test_supervisor",
        checkpoints_root=tmp_path,
        run_id="run_test",
    )


class TestCoordinatorContext:
    """The coordinator is visible only through explicit ContextVar propagation."""

    def test_current_requires_context_copy_and_clears_on_deactivate(self, tmp_path):
        cm = CheckpointManager(
            "test_supervisor",
            checkpoints_root=tmp_path,
            run_id="run_test",
        )
        coord = CheckpointCoordinator.activate(cm, "task_ctx", "task text")

        try:
            seen = []

            def worker():
                seen.append(CheckpointCoordinator.current())

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=5)

            assert seen == [None]

            propagated_context = contextvars.copy_context()
            seen_with_context = []
            thread = threading.Thread(
                target=propagated_context.run,
                args=(lambda: seen_with_context.append(CheckpointCoordinator.current()),),
            )
            thread.start()
            thread.join(timeout=5)

            assert seen_with_context == [coord]

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

    def test_concurrent_resume_claims_are_unique_across_managers(self, tmp_path):
        """One run attempt claims each matching unfinished call at most once."""
        task_id = "task_worker_resume_claim_race"
        initial = CheckpointManager(
            "test_supervisor",
            checkpoints_root=tmp_path,
            run_id="run_initial",
        )
        assert initial.record_worker_started(
            task_id,
            "same_worker",
            input_hash="same-hash",
            task_input="first",
        ) == 0
        assert initial.record_worker_started(
            task_id,
            "same_worker",
            input_hash="same-hash",
            task_input="second",
        ) == 1

        task_dir = tmp_path / task_id
        managers = [
            CheckpointManager(
                "test_supervisor",
                checkpoint_dir=task_dir,
                run_id="run_resume",
            )
            for _ in range(2)
        ]
        barrier = threading.Barrier(2)
        indexes: list[int] = []
        errors: list[Exception] = []
        result_lock = threading.Lock()

        def claim(manager: CheckpointManager) -> None:
            try:
                barrier.wait(timeout=5)
                index = manager.record_worker_started(
                    task_id,
                    "same_worker",
                    input_hash="same-hash",
                    task_input="resumed",
                    reuse_incomplete=True,
                )
                with result_lock:
                    indexes.append(index)
            except Exception as exc:
                with result_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=claim, args=(manager,)) for manager in managers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors
        assert sorted(indexes) == [0, 1]
        tree = managers[0].load_task_tree(task_id)
        assert [
            call["attempt_run_id"] for call in tree["workers"]["same_worker"]
        ] == ["run_resume", "run_resume"]
        events = [
            json.loads(line)
            for line in (task_dir / "task_events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        claim_events = [
            event for event in events if event.get("type") == "worker_call_resume_claimed"
        ]
        assert [event["call_index"] for event in claim_events] == [0, 1]
        assert {event["run_id"] for event in claim_events} == {"run_resume"}

        # Claims belong to one attempt only.  A later resume can reclaim both
        # calls in the original call_index order after the previous run dies.
        later = CheckpointManager(
            "test_supervisor",
            checkpoint_dir=task_dir,
            run_id="run_later",
        )
        assert [
            later.record_worker_started(
                task_id,
                "same_worker",
                input_hash="same-hash",
                task_input="resumed again",
                reuse_incomplete=True,
            )
            for _ in range(2)
        ] == [0, 1]

    def test_fresh_prepare_never_reuses_completed_result(self, tmp_path):
        task_id = "task_fresh_does_not_cache"
        initial = CheckpointManager(
            "test_supervisor",
            checkpoints_root=tmp_path,
            run_id="run_initial",
        )
        call_index = initial.record_worker_started(
            task_id,
            "worker",
            input_hash="same",
            task_input="first",
        )
        initial.record_worker_finished(
            task_id,
            "worker",
            call_index=call_index,
            status="completed",
            result="old-result",
        )
        fresh = CheckpointManager(
            "test_supervisor",
            checkpoint_dir=tmp_path / task_id,
            run_id="run_fresh",
        )

        preparation = fresh.prepare_worker_call(
            task_id,
            "worker",
            input_hash="same",
            task_input="second",
            resume=False,
        )

        assert preparation.should_execute is True
        assert preparation.call_index == 1

    @pytest.mark.parametrize("cached_result", ["", None])
    def test_resume_prioritizes_incomplete_then_claims_falsey_cache(
        self,
        tmp_path,
        cached_result,
    ):
        task_id = f"task_mixed_{'none' if cached_result is None else 'empty'}"
        initial = CheckpointManager(
            "test_supervisor",
            checkpoints_root=tmp_path,
            run_id="run_initial",
        )
        completed = initial.record_worker_started(
            task_id,
            "worker",
            input_hash="same",
            task_input="completed",
        )
        initial.record_worker_finished(
            task_id,
            "worker",
            call_index=completed,
            status="completed",
            result=cached_result,
        )
        interrupted = initial.record_worker_started(
            task_id,
            "worker",
            input_hash="same",
            task_input="interrupted",
        )
        initial.record_worker_finished(
            task_id,
            "worker",
            call_index=interrupted,
            status="interrupted",
        )
        resumed = CheckpointManager(
            "test_supervisor",
            checkpoint_dir=tmp_path / task_id,
            run_id="run_resume",
        )

        execute = resumed.prepare_worker_call(
            task_id,
            "worker",
            input_hash="same",
            task_input="retry",
            resume=True,
        )
        cached = resumed.prepare_worker_call(
            task_id,
            "worker",
            input_hash="same",
            task_input="replay",
            resume=True,
        )

        assert (execute.call_index, execute.should_execute) == (1, True)
        assert (cached.call_index, cached.should_execute) == (0, False)
        assert cached.cached_result == cached_result
        calls = resumed.load_task_tree(task_id)["workers"]["worker"]
        assert calls[0]["status"] == "completed"
        assert calls[0]["cached_claim_run_id"] == "run_resume"
        assert calls[1]["attempt_run_id"] == "run_resume"

    def test_concurrent_mixed_same_hash_prepare_is_one_to_one(self, tmp_path):
        task_id = "task_atomic_mixed_prepare"
        initial = CheckpointManager(
            "test_supervisor",
            checkpoints_root=tmp_path,
            run_id="run_initial",
        )
        completed = initial.record_worker_started(
            task_id,
            "worker",
            input_hash="same",
            task_input="completed",
        )
        initial.record_worker_finished(
            task_id,
            "worker",
            call_index=completed,
            status="completed",
            result="cached",
        )
        interrupted = initial.record_worker_started(
            task_id,
            "worker",
            input_hash="same",
            task_input="interrupted",
        )
        initial.record_worker_finished(
            task_id,
            "worker",
            call_index=interrupted,
            status="interrupted",
        )
        managers = [
            CheckpointManager(
                "test_supervisor",
                checkpoint_dir=tmp_path / task_id,
                run_id="run_resume",
            )
            for _ in range(2)
        ]
        barrier = threading.Barrier(2)
        preparations = []
        errors = []
        result_lock = threading.Lock()

        def prepare(manager):
            try:
                barrier.wait(timeout=5)
                result = manager.prepare_worker_call(
                    task_id,
                    "worker",
                    input_hash="same",
                    task_input="resume",
                    resume=True,
                )
                with result_lock:
                    preparations.append(result)
            except Exception as exc:
                with result_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=prepare, args=(manager,)) for manager in managers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors
        assert sorted(
            (item.call_index, item.should_execute, item.cached_result)
            for item in preparations
        ) == [(0, False, "cached"), (1, True, None)]
        calls = managers[0].load_task_tree(task_id)["workers"]["worker"]
        assert len(calls) == 2

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
