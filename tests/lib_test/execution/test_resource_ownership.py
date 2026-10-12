"""Owned resource failures remain retryable by Application finalization."""
import pytest
from agentloom.execution import RuntimeHome, bind_run_context
from agentloom.execution.resources import close_instance_resources, close_run_resources, register_resource


def test_failed_instance_handle_is_retried_by_run_without_closing_a_sibling(tmp_path):
    context = RuntimeHome(tmp_path / "runtime").new_context(application_id="batch")
    closed = []
    attempts = 0

    def close_worker():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary close failure")
        closed.append("worker")

    with bind_run_context(context):
        register_resource("worker-handle", close_worker, instance_id="worker")
        register_resource("sibling-handle", lambda: closed.append("sibling"), instance_id="sibling")
        with pytest.raises(ExceptionGroup, match="Execution resource cleanup failed"):
            close_instance_resources("worker")
        assert closed == []
        close_run_resources()
        close_run_resources()

    assert attempts == 2
    assert sorted(closed) == ["sibling", "worker"]
