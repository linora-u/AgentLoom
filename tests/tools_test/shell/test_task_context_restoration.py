"""Legacy task contexts must not promote another thread's fallback into a parent."""

from threading import Thread

from agentloom.execution.trace.task_context import (
    capture_explicit_execution_context, clear_current_task_id,
    set_current_task_id, task_context,
)


def test_task_context_restores_only_its_own_explicit_parent():
    clear_current_task_id()
    try:
        thread = Thread(target=set_current_task_id, args=("foreign-thread-task",))
        thread.start()
        thread.join()
        assert capture_explicit_execution_context().task_id is None
        with task_context("local-task"):
            assert capture_explicit_execution_context().task_id == "local-task"
        assert capture_explicit_execution_context().task_id is None
    finally:
        clear_current_task_id()
