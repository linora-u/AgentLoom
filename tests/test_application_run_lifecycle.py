from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from src.application_run_lifecycle import (
    ApplicationRunFinalization,
    ApplicationRunLifecycle,
    ApplicationRunResources,
)


def test_lifecycle_defers_agent_checkpoint_until_run_owner_commits() -> None:
    lifecycle = ApplicationRunLifecycle()
    coordinator = MagicMock()
    runtime_agent = SimpleNamespace(memory=SimpleNamespace(steps=[]))
    manager = MagicMock()

    lifecycle.enter_execution()
    lifecycle.report_agent_invocation(
        coordinator=coordinator,
        runtime_agent=runtime_agent,
        result="done",
        error=None,
        goal={"status": "complete"},
    )
    lifecycle.settle_reported_agent_invocation()

    coordinator.save_supervisor.assert_not_called()
    manager.record_task_status_changed.assert_not_called()

    lifecycle.commit_checkpoint(
        checkpoint_manager=manager,
        task_id="task-1",
    )

    coordinator.save_supervisor.assert_called_once_with(
        runtime_agent,
        "completed",
        result="done",
        error=None,
    )
    assert lifecycle.goal == {"status": "complete"}


def test_finalization_failure_replaces_provisional_success_checkpoint() -> None:
    lifecycle = ApplicationRunLifecycle()
    coordinator = MagicMock()
    runtime_agent = SimpleNamespace(memory=SimpleNamespace(steps=[]))
    manager = MagicMock()

    lifecycle.enter_execution()
    lifecycle.report_agent_invocation(
        coordinator=coordinator,
        runtime_agent=runtime_agent,
        result="done",
        error=None,
        goal=None,
    )
    lifecycle.complete_execution("done")
    lifecycle.commit_checkpoint(
        checkpoint_manager=manager,
        task_id="task-1",
    )

    failure = OSError("manifest commit failed")
    lifecycle.fail_finalization(failure)
    lifecycle.commit_checkpoint(
        checkpoint_manager=manager,
        task_id="task-1",
    )

    assert coordinator.save_supervisor.call_args_list == [
        call(runtime_agent, "completed", result="done", error=None),
        call(
            runtime_agent,
            "failed",
            result=None,
            error="manifest commit failed",
        ),
    ]


def test_finalize_run_owns_evidence_manifest_and_success_cleanup(
    monkeypatch,
) -> None:
    lifecycle = ApplicationRunLifecycle()
    runtime_context = MagicMock()
    manager = MagicMock()
    manager.load_task_tree_projection.return_value = {"status": "completed"}
    manager.delete_task.return_value = True
    persist_observability = MagicMock()
    manifest_updates: dict[str, object] = {}
    monkeypatch.setattr(
        "src.application_run_lifecycle._persist_run_observability",
        persist_observability,
    )

    lifecycle.enter_execution()
    lifecycle.complete_execution("done")
    lifecycle.finalize_run(
        ApplicationRunFinalization(
            runtime_context=runtime_context,
            checkpoint_manager=manager,
            task_id="task-1",
            event_start_offset=10,
            manifest_updates=manifest_updates,
            cleanup_on_success=True,
            log=MagicMock(),
        )
    )

    manager.record_task_status_changed.assert_called_once_with(
        "task-1",
        "completed",
        result="done",
        error=None,
    )
    persist_observability.assert_called_once_with(
        runtime_context,
        manager,
        "task-1",
        result="done",
        event_start_offset=10,
        manifest_updates=manifest_updates,
    )
    runtime_context.update_manifest.assert_called_once()
    assert runtime_context.update_manifest.call_args.kwargs["status"] == "completed"
    manager.delete_task.assert_called_once_with("task-1")


def test_finalization_failure_can_replace_an_execution_failure() -> None:
    lifecycle = ApplicationRunLifecycle()

    lifecycle.enter_execution()
    lifecycle.fail_execution(RuntimeError("agent failed"))
    lifecycle.fail_finalization(OSError("checkpoint commit failed"))

    assert lifecycle.phase == "finalization"
    assert lifecycle.outcome == "failed"
    assert lifecycle.error_message == "checkpoint commit failed"


def test_terminal_checkpoint_precedes_coordinator_deactivation(monkeypatch) -> None:
    from src.lib.checkpoint.coordinator import CheckpointCoordinator

    lifecycle = ApplicationRunLifecycle()
    coordinator = MagicMock()
    runtime_agent = SimpleNamespace(memory=SimpleNamespace(steps=[]))
    manager = MagicMock()
    observed: list[str] = []
    coordinator.save_supervisor.side_effect = lambda *_args, **_kwargs: observed.append("checkpoint")
    monkeypatch.setattr(
        CheckpointCoordinator,
        "deactivate",
        lambda current: observed.append("deactivate"),
    )

    lifecycle.enter_execution()
    lifecycle.report_agent_invocation(
        coordinator=coordinator,
        runtime_agent=runtime_agent,
        result="done",
        error=None,
        goal=None,
    )
    lifecycle.complete_execution("done")
    lifecycle.commit_checkpoint(
        checkpoint_manager=manager,
        task_id="task-1",
    )
    lifecycle.close_execution_resources(
        supervisor=None,
        heartbeat=None,
        file_history=None,
        log=MagicMock(),
    )

    assert observed == ["checkpoint", "deactivate"]


def test_checkpoint_resources_close_when_coordinator_deactivation_fails(
    monkeypatch,
) -> None:
    from src.lib.checkpoint.coordinator import CheckpointCoordinator

    lifecycle = ApplicationRunLifecycle()
    coordinator = MagicMock()
    task_lease = MagicMock()
    manager = MagicMock()
    lifecycle.enter_execution()
    lifecycle.report_agent_invocation(
        coordinator=coordinator,
        runtime_agent=MagicMock(),
        result=None,
        error=RuntimeError("agent failed"),
        goal=None,
    )
    lifecycle.settle_reported_agent_invocation()
    monkeypatch.setattr(
        CheckpointCoordinator,
        "deactivate",
        lambda _current: (_ for _ in ()).throw(OSError("context close failed")),
    )

    with pytest.raises(OSError, match="context close failed"):
        lifecycle.close_resources(
            ApplicationRunResources(
                supervisor=None,
                heartbeat=None,
                file_history=None,
                log=MagicMock(),
                task_lease=task_lease,
                checkpoint_manager=manager,
            )
        )

    task_lease.release.assert_called_once_with()
    manager.close.assert_called_once_with()
    assert lifecycle.phase == "cleanup"


def test_lifecycle_rejects_invalid_phase_transition() -> None:
    lifecycle = ApplicationRunLifecycle()

    with pytest.raises(RuntimeError, match="cannot complete execution"):
        lifecycle.complete_execution("too early")


def test_uncaught_cleanup_failure_replaces_completed_internal_state() -> None:
    lifecycle = ApplicationRunLifecycle()
    runtime_context = MagicMock()
    failure = OSError("lease release failed")

    lifecycle.enter_execution()
    lifecycle.complete_execution("done")
    lifecycle.enter_cleanup()
    lifecycle.persist_uncaught_failure(
        runtime_context=runtime_context,
        manifest_initialized=True,
        manifest_updates={},
        error=failure,
    )

    assert lifecycle.phase == "cleanup"
    assert lifecycle.outcome == "failed"
    assert lifecycle.error is failure
    runtime_context.update_manifest.assert_called_once()
    assert runtime_context.update_manifest.call_args.kwargs["status"] == "failed"
