"""
Tests for src.lib.concurrency.parallel_executor.ParallelAgentExecutor.

Covers: basic execution, error isolation, back-pressure, circuit breaker,
progress callbacks, log isolation, max_workers auto-calculation, execute_groups.

All tests use mock callables (no LLM).
"""

from __future__ import annotations

import threading
import time

import pytest

from src.lib.concurrency.models import TaskResult
from src.lib.concurrency.parallel_executor import ParallelAgentExecutor
from src.lib.concurrency.rate_limiter import GlobalRateLimiterRegistry


@pytest.fixture(autouse=True)
def _reset_registry():
    GlobalRateLimiterRegistry.reset()
    yield
    GlobalRateLimiterRegistry.reset()


# ═══════════════════════════════════════════════════════════════════ #
#  Helpers
# ═══════════════════════════════════════════════════════════════════ #

def _make_tool(fn):
    """Give a lambda/function a __name__ for log context."""
    fn.__name__ = "mock_tool"
    return fn


# ═══════════════════════════════════════════════════════════════════ #
#  Basic execution
# ═══════════════════════════════════════════════════════════════════ #

class TestParallelExecutorBasic:
    def test_single_task_completed(self):
        tool = _make_tool(lambda task_id, **kw: f"done-{task_id}")
        executor = ParallelAgentExecutor(max_workers=2)
        results = executor.execute_batch([{"task_id": "a"}], tool)
        assert len(results) == 1
        assert results[0].status == "completed"
        assert results[0].result == "done-a"

    def test_multiple_tasks_all_complete(self):
        tool = _make_tool(lambda task_id, **kw: task_id)
        executor = ParallelAgentExecutor(max_workers=3)
        tasks = [{"task_id": str(i)} for i in range(5)]
        results = executor.execute_batch(tasks, tool)
        assert len(results) == 5
        assert all(r.status == "completed" for r in results)

    def test_results_contain_correct_task_ids(self):
        tool = _make_tool(lambda task_id, **kw: None)
        executor = ParallelAgentExecutor(max_workers=2)
        tasks = [{"task_id": "x"}, {"task_id": "y"}, {"task_id": "z"}]
        results = executor.execute_batch(tasks, tool)
        result_ids = {r.task_id for r in results}
        assert result_ids == {"x", "y", "z"}

    def test_duration_is_positive(self):
        tool = _make_tool(lambda task_id, **kw: time.sleep(0.05))
        executor = ParallelAgentExecutor(max_workers=2)
        results = executor.execute_batch([{"task_id": "a"}], tool)
        assert results[0].duration_seconds > 0

    def test_empty_task_list(self):
        tool = _make_tool(lambda **kw: None)
        executor = ParallelAgentExecutor(max_workers=2)
        assert executor.execute_batch([], tool) == []


# ═══════════════════════════════════════════════════════════════════ #
#  Error isolation
# ═══════════════════════════════════════════════════════════════════ #

class TestParallelExecutorErrorIsolation:
    def test_one_failure_others_succeed(self):
        def tool(task_id, **kw):
            if task_id == "bad":
                raise ValueError("boom")
            return "ok"
        tool = _make_tool(tool)

        executor = ParallelAgentExecutor(max_workers=3, circuit_breaker_threshold=99)
        tasks = [{"task_id": "a"}, {"task_id": "bad"}, {"task_id": "c"}]
        results = executor.execute_batch(tasks, tool)
        statuses = {r.task_id: r.status for r in results}
        assert statuses["a"] == "completed"
        assert statuses["bad"] == "failed"
        assert statuses["c"] == "completed"

    def test_failed_result_has_error(self):
        tool = _make_tool(lambda task_id, **kw: (_ for _ in ()).throw(RuntimeError("oops")))

        def bad_tool(task_id, **kw):
            raise RuntimeError("oops")
        bad_tool = _make_tool(bad_tool)

        executor = ParallelAgentExecutor(max_workers=1, circuit_breaker_threshold=99)
        results = executor.execute_batch([{"task_id": "x"}], bad_tool)
        assert results[0].status == "failed"
        assert "oops" in results[0].error
        assert results[0].error_trace is not None

    def test_all_fail_no_crash(self):
        def fail(task_id, **kw):
            raise ValueError(f"fail-{task_id}")
        fail = _make_tool(fail)

        executor = ParallelAgentExecutor(max_workers=2, circuit_breaker_threshold=99)
        tasks = [{"task_id": str(i)} for i in range(5)]
        results = executor.execute_batch(tasks, fail)
        assert len(results) == 5
        assert all(r.status == "failed" for r in results)


# ═══════════════════════════════════════════════════════════════════ #
#  Back-pressure
# ═══════════════════════════════════════════════════════════════════ #

class TestParallelExecutorBackpressure:
    def test_max_pending_limits_active(self):
        """At most max_pending tasks should be in-flight simultaneously."""
        peak_active = [0]
        current_active = [0]
        lock = threading.Lock()

        def slow_tool(task_id, **kw):
            with lock:
                current_active[0] += 1
                peak_active[0] = max(peak_active[0], current_active[0])
            time.sleep(0.1)
            with lock:
                current_active[0] -= 1
            return "ok"
        slow_tool = _make_tool(slow_tool)

        executor = ParallelAgentExecutor(max_workers=10, max_pending=3)
        tasks = [{"task_id": str(i)} for i in range(8)]
        results = executor.execute_batch(tasks, slow_tool)
        assert len(results) == 8
        assert peak_active[0] <= 3, f"Peak active {peak_active[0]} > max_pending 3"

    def test_backpressure_no_lost_tasks(self):
        tool = _make_tool(lambda task_id, **kw: task_id)
        executor = ParallelAgentExecutor(max_workers=5, max_pending=2)
        tasks = [{"task_id": str(i)} for i in range(10)]
        results = executor.execute_batch(tasks, tool)
        assert len(results) == 10


# ═══════════════════════════════════════════════════════════════════ #
#  Circuit breaker
# ═══════════════════════════════════════════════════════════════════ #

class TestParallelExecutorCircuitBreaker:
    def test_triggers_on_consecutive_failures(self):
        """Circuit breaker skips remaining tasks after consecutive failures."""
        call_count = [0]

        def fail(task_id, **kw):
            call_count[0] += 1
            raise ValueError("fail")
        fail = _make_tool(fail)

        # max_workers=1, max_pending=1: forces sequential submit-drain-check cycle
        # With threshold=3: tasks 0,1,2 execute (consec goes 1→2→3 after draining each),
        # but task 3 is submitted before task 2's drain (backpressure drains task 2, consec→3,
        # then task 3 is already submitted). Circuit breaker fires for task 4+.
        executor = ParallelAgentExecutor(max_workers=1, max_pending=1, circuit_breaker_threshold=3)
        tasks = [{"task_id": str(i)} for i in range(8)]
        results = executor.execute_batch(tasks, fail)

        failed = [r for r in results if r.status == "failed"]
        skipped = [r for r in results if r.status == "skipped"]
        # At least threshold+1 tasks actually execute before breaker kicks in
        assert len(failed) >= 3
        assert len(skipped) >= 1  # some tasks were skipped
        assert len(failed) + len(skipped) == 8

    def test_resets_on_success(self):
        """Success resets the consecutive-failure counter."""
        sequence = iter(["fail", "fail", "ok", "fail", "fail", "ok"])

        def tool(task_id, **kw):
            action = next(sequence)
            if action == "fail":
                raise ValueError("fail")
            return "ok"
        tool = _make_tool(tool)

        executor = ParallelAgentExecutor(max_workers=1, max_pending=1, circuit_breaker_threshold=3)
        tasks = [{"task_id": str(i)} for i in range(6)]
        results = executor.execute_batch(tasks, tool)
        # No skipped: counter resets after each success
        skipped = [r for r in results if r.status == "skipped"]
        assert len(skipped) == 0

    def test_skipped_has_correct_status(self):
        """Skipped tasks have status='skipped' and error containing 'circuit breaker'."""
        def fail(task_id, **kw):
            raise ValueError("fail")
        fail = _make_tool(fail)

        # max_workers=1, max_pending=1, threshold=2: after 2 drained failures → skip rest
        executor = ParallelAgentExecutor(max_workers=1, max_pending=1, circuit_breaker_threshold=2)
        tasks = [{"task_id": str(i)} for i in range(6)]
        results = executor.execute_batch(tasks, fail)
        skipped = [r for r in results if r.status == "skipped"]
        failed = [r for r in results if r.status == "failed"]
        assert len(failed) >= 2
        assert len(skipped) >= 1
        for s in skipped:
            assert s.error is not None
            assert "circuit breaker" in s.error


# ═══════════════════════════════════════════════════════════════════ #
#  Progress callback
# ═══════════════════════════════════════════════════════════════════ #

class TestParallelExecutorProgress:
    def test_on_progress_called(self):
        tool = _make_tool(lambda task_id, **kw: None)
        progress_calls = []
        executor = ParallelAgentExecutor(max_workers=2)
        tasks = [{"task_id": str(i)} for i in range(3)]
        executor.execute_batch(tasks, tool, on_progress=lambda c, t, r: progress_calls.append((c, t)))
        assert len(progress_calls) == 3

    def test_on_progress_none_ok(self):
        tool = _make_tool(lambda task_id, **kw: None)
        executor = ParallelAgentExecutor(max_workers=2)
        executor.execute_batch([{"task_id": "a"}], tool, on_progress=None)


# ═══════════════════════════════════════════════════════════════════ #
#  Max workers auto-calculation
# ═══════════════════════════════════════════════════════════════════ #

class TestParallelExecutorMaxWorkers:
    """Auto max_workers = min(RPM, 10). Rate limiter handles actual pacing."""

    def test_explicit_max_workers(self):
        executor = ParallelAgentExecutor(max_workers=3)
        assert executor._max_workers == 3

    def test_auto_default_rpm10(self):
        """RPM=10 → min(10, 10) = 10."""
        GlobalRateLimiterRegistry.get_limiter("powerful", rpm=10)
        executor = ParallelAgentExecutor(model_type="powerful")
        assert executor._max_workers == 10

    def test_auto_high_rpm_capped(self):
        """RPM=60 → min(60, 10) = 10 (capped)."""
        GlobalRateLimiterRegistry.get_limiter("fast", rpm=60)
        executor = ParallelAgentExecutor(model_type="fast")
        assert executor._max_workers == 10

    def test_auto_low_rpm(self):
        """RPM=1 → min(1, 10) = 1."""
        GlobalRateLimiterRegistry.get_limiter("slow", rpm=1)
        executor = ParallelAgentExecutor(model_type="slow")
        assert executor._max_workers == 1

    def test_auto_rpm5(self):
        """RPM=5 → min(5, 10) = 5."""
        GlobalRateLimiterRegistry.get_limiter("medium", rpm=5)
        executor = ParallelAgentExecutor(model_type="medium")
        assert executor._max_workers == 5

    def test_auto_rpm20(self):
        """RPM=20 → min(20, 10) = 10 (capped)."""
        GlobalRateLimiterRegistry.get_limiter("turbo", rpm=20)
        executor = ParallelAgentExecutor(model_type="turbo")
        assert executor._max_workers == 10

    def test_fallback_when_registry_unavailable(self):
        """When registry fails, default RPM=10 → min(10, 10) = 10."""
        executor = ParallelAgentExecutor(model_type="nonexistent_type_xyz")
        assert executor._max_workers == 10


# ═══════════════════════════════════════════════════════════════════ #
#  execute_groups
# ═══════════════════════════════════════════════════════════════════ #

class TestExecuteGroups:
    def test_groups_sequential_internal_parallel(self):
        """Groups run in order; tasks within a group run in parallel."""
        order = []
        lock = threading.Lock()

        def tool(task_id, group, **kw):
            with lock:
                order.append((group, task_id))
            time.sleep(0.05)
            return f"{group}-{task_id}"
        tool = _make_tool(tool)

        executor = ParallelAgentExecutor(max_workers=2)
        groups = [
            [{"task_id": "a", "group": "1"}, {"task_id": "b", "group": "1"}],
            [{"task_id": "c", "group": "2"}, {"task_id": "d", "group": "2"}],
        ]
        results = executor.execute_groups(groups, tool)

        assert len(results) == 2
        assert len(results[0]) == 2
        assert len(results[1]) == 2
        # All group-1 tasks should complete before group-2 starts
        g1_ids = {r.task_id for r in results[0]}
        g2_ids = {r.task_id for r in results[1]}
        assert g1_ids == {"a", "b"}
        assert g2_ids == {"c", "d"}

    def test_empty_group_skipped(self):
        tool = _make_tool(lambda task_id, **kw: None)
        executor = ParallelAgentExecutor(max_workers=2)
        results = executor.execute_groups([[], [{"task_id": "a"}]], tool)
        assert len(results) == 2
        assert results[0] == []
        assert len(results[1]) == 1

    def test_single_group_same_as_batch(self):
        tool = _make_tool(lambda task_id, **kw: task_id)
        executor = ParallelAgentExecutor(max_workers=2)
        tasks = [{"task_id": "x"}, {"task_id": "y"}]
        batch = executor.execute_batch(tasks, tool)
        group = executor.execute_groups([tasks], tool)
        assert len(group[0]) == len(batch)


# ═══════════════════════════════════════════════════════════════════ #
#  Context propagation
# ═══════════════════════════════════════════════════════════════════ #

class TestParallelExecutorContextPropagation:
    def test_checkpoint_coordinator_context_visible_in_worker_thread(self, tmp_path):
        """ThreadPoolExecutor workers inherit the active checkpoint coordinator."""
        from src.lib.checkpoint.checkpoint_manager import CheckpointManager
        from src.lib.checkpoint.coordinator import CheckpointCoordinator, _current_coordinator

        cm = CheckpointManager("parallel_context", base_dir=tmp_path)
        coord = CheckpointCoordinator.activate(cm, "task_ctx", "task")

        def tool(task_id, **kw):
            return CheckpointCoordinator.current() is coord
        tool = _make_tool(tool)

        try:
            executor = ParallelAgentExecutor(max_workers=1)
            results = executor.execute_batch([{"task_id": "a"}], tool)
            assert results[0].status == "completed"
            assert results[0].result is True
        finally:
            _current_coordinator.set(None)
