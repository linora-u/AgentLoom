"""Control signals at the public Application batch and Run boundaries."""

from __future__ import annotations

import pytest
from agentloom.app import Application, current_run_app
from agentloom.app.run import ApplicationRunError, ApplicationRunInterrupted
from agentloom.app.runner import execute_app
from agentloom.execution.concurrency import WorkItem
from agentloom.execution.context import RuntimeContext

from test_support.worker_batch import batch_project as _batch_project

batch_project = _batch_project


class ControlSignal(BaseException):
    """An application-defined control signal outside the Exception hierarchy."""


def _run_at_application_seam(entrypoint, batch_project, business, events):
    root, application, _worker, probe, write = batch_project
    if entrypoint == "standalone":
        with Application.open(application, project_root=root, file_logging=False) as app:
            return business(app)
    workflow = application / "workflows/root.yaml"
    write(
        workflow,
        {
            "name": "batch-root",
            "description": "Orchestrate.",
            "task": "Execute plan.",
            "agent_runtime": "batch-fixture",
            "tools": [],
            "toolsets": [],
        },
    )
    probe.root_behavior = lambda: business(current_run_app())
    return execute_app(workflow, file_logging=False, event_sink=events.append)


@pytest.mark.parametrize("entrypoint", ["standalone", "runner"])
@pytest.mark.parametrize("original_type", [KeyboardInterrupt, SystemExit, ControlSignal])
@pytest.mark.parametrize("secondary_type", [KeyboardInterrupt, SystemExit, ControlSignal])
def test_terminal_manifest_control_preserves_original_worker_control(
    batch_project, monkeypatch, entrypoint, original_type, secondary_type
):
    _root, _application, _worker, probe, _write = batch_project
    original = original_type("original Worker control")
    secondary = secondary_type("terminal manifest control")
    update = RuntimeContext.update_manifest
    events = []

    def work(payload):
        raise original

    def persist(self, **updates):
        if updates.get("status") in {"failed", "interrupted"}:
            raise secondary
        return update(self, **updates)

    probe.behavior = work
    monkeypatch.setattr(RuntimeContext, "update_manifest", persist)

    def business(app):
        return app.worker("workflows/worker.yaml").map(
            [WorkItem("started", {"value": 1}), WorkItem("unstarted", {"value": 2})], concurrency=1
        )

    adapter = ApplicationRunInterrupted if original_type is KeyboardInterrupt else ApplicationRunError
    with pytest.raises(original_type if entrypoint == "standalone" else adapter) as caught:
        _run_at_application_seam(entrypoint, batch_project, business, events)
    propagated = caught.value if entrypoint == "standalone" else caught.value.original_error
    assert propagated is original
    outcomes = original.batch_outcome.outcomes
    assert [item.status for item in outcomes] == ["failed", "canceled"]
    assert outcomes[0].invocation_id is not None and outcomes[1].invocation_id is None
    assert any(entry.phase == "manifest" and entry.error is secondary for entry in original.run_secondary_errors)
    assert any(entry.phase == "finalization" and entry.error is secondary for entry in original.run_secondary_errors)
    if entrypoint == "runner":
        assert events[-1].event == ("run.interrupted" if original_type is KeyboardInterrupt else "run.failed")
    assert probe.active == 0 and probe.closed == [{"value": 1}]


@pytest.mark.parametrize("entrypoint", ["standalone", "runner"])
def test_first_terminal_manifest_control_replaces_ordinary_failure(batch_project, monkeypatch, entrypoint):
    _root, _application, _worker, probe, _write = batch_project
    original = OSError("ordinary Application failure")
    control = KeyboardInterrupt("first terminal manifest control")
    update = RuntimeContext.update_manifest
    terminal_writes = 0
    results, events = [], []

    def persist(self, **updates):
        nonlocal terminal_writes
        if updates.get("status") == "failed":
            terminal_writes += 1
            if terminal_writes == 2:
                raise control
        return update(self, **updates)

    monkeypatch.setattr(RuntimeContext, "update_manifest", persist)

    def business(app):
        results.append(app.worker("workflows/worker.yaml").map([WorkItem("started", {"value": 1})]))
        raise original

    with pytest.raises(KeyboardInterrupt if entrypoint == "standalone" else ApplicationRunInterrupted) as caught:
        _run_at_application_seam(entrypoint, batch_project, business, events)
    propagated = caught.value if entrypoint == "standalone" else caught.value.original_error
    assert propagated is control
    assert any(entry.phase == "execution" and entry.error is original for entry in control.run_secondary_errors)
    assert results[0].status == "completed"
    if entrypoint == "runner":
        assert events[-1].event == "run.interrupted" and events[-1].phase == "execution"
    assert probe.active == 0 and probe.closed == [{"value": 1}]


@pytest.mark.parametrize("entrypoint", ["standalone", "runner"])
@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit, ControlSignal])
def test_terminal_manifest_control_after_success_remains_observable(
    batch_project, monkeypatch, entrypoint, control_type
):
    _root, _application, _worker, probe, _write = batch_project
    control = control_type("first terminal manifest control")
    update = RuntimeContext.update_manifest
    results, events = [], []

    def persist(self, **updates):
        if updates.get("status") in {"completed", "failed", "interrupted"}:
            raise control
        return update(self, **updates)

    monkeypatch.setattr(RuntimeContext, "update_manifest", persist)

    def business(app):
        result = app.worker("workflows/worker.yaml").map([WorkItem("started", {"value": 1})])
        results.append(result)
        return result.outcomes[0].output

    adapter = ApplicationRunInterrupted if control_type is KeyboardInterrupt else ApplicationRunError
    with pytest.raises(control_type if entrypoint == "standalone" else adapter) as caught:
        _run_at_application_seam(entrypoint, batch_project, business, events)
    propagated = caught.value if entrypoint == "standalone" else caught.value.original_error
    assert propagated is control and not hasattr(control, "batch_outcome")
    assert results[0].status == "completed"
    if entrypoint == "runner":
        assert events[-1].event == ("run.interrupted" if control_type is KeyboardInterrupt else "run.failed")
        assert events[-1].phase == "finalization"
    assert probe.active == 0 and probe.closed == [{"value": 1}]
