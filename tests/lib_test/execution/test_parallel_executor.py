from dataclasses import replace
from importlib import import_module
from threading import Barrier

import pytest
from agentloom.execution.concurrency import ParallelAgentExecutor
from agentloom.execution.trace.task_context import (
    bind_explicit_execution_context,
    capture_explicit_execution_context,
    sub_task_context,
)


def test_disabled_circuit_breaker_runs_remaining_tasks_after_failure() -> None:
    def run(task_id: str) -> str:
        if task_id == "first":
            raise RuntimeError("temporary failure")
        return task_id

    executor = ParallelAgentExecutor(max_workers=1, max_pending=1, circuit_breaker_threshold=None)
    results = executor.execute_batch(
        [{"task_id": "first"}, {"task_id": "second"}, {"task_id": "third"}], run
    )

    assert [(result.task_id, result.status) for result in results] == [
        ("first", "failed"), ("second", "completed"), ("third", "completed")
    ]


@pytest.mark.parametrize("workers", [2, 8, 20])
@pytest.mark.parametrize("fail_some", [False, True])
def test_parallel_then_serial_preserves_parent_context_and_legacy_globals(monkeypatch, workers, fail_some):
    contexts = import_module("agentloom.execution.trace.task_context")
    fallbacks = (
        "_global_sub_task_id_fallback", "_global_agent_id_fallback", "_global_agent_name_fallback",
    )
    foreign = ("foreign-sub", "foreign-instance", "foreign-agent")
    for name, value in zip(fallbacks, foreign, strict=True):
        monkeypatch.setattr(contexts, name, value)
    baseline = capture_explicit_execution_context()
    barrier = Barrier(workers)
    executor = ParallelAgentExecutor(
        max_workers=workers, max_pending=workers, circuit_breaker_threshold=None,
    )

    for round_number in range(10):
        parent = replace(
            baseline, task_id=f"task-{round_number}", root_run_id=f"root-{round_number}",
            local_run_id=f"run-{round_number}",
            sub_task_id=f"parent-sub-{round_number}" if round_number % 2 else None,
            agent_id=f"parent-instance-{round_number}", agent_name="parent",
        )

        def worker_tool(task_id, index, parallel=True, _parent=parent):
            outer = capture_explicit_execution_context()
            name = f"worker_tool_{task_id}"
            assert outer.sub_task_id
            assert outer == replace(_parent, sub_task_id=outer.sub_task_id, agent_id=name, agent_name=name)
            assert outer.root_run_state is _parent.root_run_state
            try:
                with sub_task_context("nested", f"nested-{task_id}", agent_id=f"nested-instance-{task_id}"):
                    if parallel:
                        barrier.wait(timeout=5)
                    assert tuple(getattr(contexts, field) for field in fallbacks) == foreign
                    assert capture_explicit_execution_context() == replace(
                        outer, sub_task_id=f"nested-{task_id}",
                        agent_id=f"nested-instance-{task_id}", agent_name="nested",
                    )
                    if parallel and fail_some and index % 2:
                        raise RuntimeError(f"expected worker failure: {task_id}")
            finally:
                assert capture_explicit_execution_context() == outer
            return task_id

        tasks = [{"task_id": f"job-{round_number}-{index}", "index": index}
                 for index in range(workers * 2)]
        with bind_explicit_execution_context(parent):
            results = executor.execute_batch(tasks, worker_tool)
            assert len(results) == len(tasks)
            by_id = {result.task_id: result for result in results}
            assert len(by_id) == len(tasks)
            for task in tasks:
                result = by_id[task["task_id"]]
                if fail_some and task["index"] % 2:
                    assert result.status == "failed"
                    assert result.error == f"expected worker failure: {task['task_id']}"
                else:
                    assert result.status == "completed"
                    assert result.result == task["task_id"]
            assert capture_explicit_execution_context() == parent
            serial = executor.execute_batch(
                [{"task_id": "serial", "index": -1, "parallel": False}], worker_tool,
            )
            assert len(serial) == 1
            assert serial[0].status == "completed"
            assert serial[0].result == "serial"
            assert capture_explicit_execution_context() == parent
            assert capture_explicit_execution_context().root_run_state is parent.root_run_state
        assert capture_explicit_execution_context() == baseline
        assert tuple(getattr(contexts, field) for field in fallbacks) == foreign
