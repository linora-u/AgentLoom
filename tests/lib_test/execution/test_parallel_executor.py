from agentloom.execution.concurrency import ParallelAgentExecutor


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
