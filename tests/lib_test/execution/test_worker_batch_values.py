"""Immutable public work values owned by a finite batch."""

import pytest
from agentloom.execution.concurrency import WorkItem, WorkPlan


def test_work_plan_owns_nested_inputs_and_provides_independent_invocation_copies():
    source = {"story": {"tags": ["news"]}}
    plan = WorkPlan([WorkItem("one", source), WorkItem("two", source)])
    source["story"]["tags"].append("caller mutation")
    first = plan.items[0].mutable_input()
    first["story"]["tags"].append("worker mutation")
    assert plan.items[1].mutable_input() == {"story": {"tags": ["news"]}}
    assert plan.items[0].mutable_input() == {"story": {"tags": ["news"]}}
    with pytest.raises(TypeError):
        plan.items[0].input["story"]["tags"][0] = "changed"
    assert (
        plan.input_fingerprint
        == WorkPlan(
            [
                WorkItem("one", {"story": {"tags": ["news"]}}),
                WorkItem("two", {"story": {"tags": ["news"]}}),
            ]
        ).input_fingerprint
    )


def test_receipts_own_nested_results_and_preserve_none_success_and_original_batch_error():
    from agentloom.execution.concurrency import (
        BatchOutcome,
        CleanupDiagnostic,
        WorkerBatchError,
        WorkerOutcome,
        resolve_worker_execution,
    )

    result = {"answer": ["original"]}
    completed = WorkerOutcome("one", 0, "success", invocation_id="agent-one", output=result)
    result["answer"].append("later mutation")
    empty_answer = WorkerOutcome("two", 1, "success", invocation_id="agent-two", output=None)
    batch = BatchOutcome(
        "batch-one",
        "run-one",
        "a.yaml",
        "completed",
        resolve_worker_execution("a.yaml"),
        outcomes=[completed, empty_answer],
    )
    assert batch.to_dict()["outcomes"][0]["output"] == {"answer": ["original"]}
    assert batch.to_dict()["outcomes"][1]["status"] == "success"
    assert batch.to_dict()["outcomes"][1]["output"] is None
    batch.to_dict()["outcomes"][0]["output"]["answer"].append("receipt reader mutation")
    assert completed.to_dict()["output"] == {"answer": ["original"]}
    original = RuntimeError("owned handle did not close")
    diagnostic = CleanupDiagnostic("resource_close", "RuntimeError", "owned handle did not close")
    error = WorkerBatchError(
        "incomplete drain", phase="cleanup", original_error=original, outcome=batch, cleanup_errors=[diagnostic]
    )
    assert error.original_error is original
    assert error.outcomes == batch.outcomes
    assert error.cleanup_errors == (diagnostic,)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), {1: "bad key"}, {"not_json": object()}])
def test_work_item_rejects_invalid_json(invalid):
    with pytest.raises(ValueError, match="JSON"):
        WorkItem("one", invalid)


def test_work_plan_rejects_duplicate_ids_and_streaming_input():
    with pytest.raises(ValueError, match="duplicate"):
        WorkPlan([WorkItem("one", {}), WorkItem("one", {})])
    with pytest.raises(ValueError, match="finite Sequence"):
        WorkPlan(WorkItem(str(index), {}) for index in range(2))
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="cycle"):
        WorkItem("cycle", cyclic)


def test_receipts_reject_mutable_untyped_diagnostics():
    from agentloom.execution.concurrency import WorkerOutcome

    with pytest.raises(ValueError, match="WorkerError"):
        WorkerOutcome("one", 0, "failed", error={"message": "mutable cause"})


def test_worker_error_preserves_runtime_diagnostic_metadata_without_authorizing_replay():
    from agentloom.execution.agent_runtime import AgentRuntimeError
    from agentloom.execution.concurrency import WorkerError

    runtime_error = AgentRuntimeError("provider unavailable", category="provider", retryable=True)
    cause = WorkerError.from_exception(runtime_error, traceback="recorded provider traceback")
    assert cause.category == "provider"
    assert cause.retryable is True
    assert cause.traceback == "recorded provider traceback"
    assert cause.error_type == "AgentRuntimeError"
