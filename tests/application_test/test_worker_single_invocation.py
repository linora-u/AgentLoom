"""Single invocation behavior at the public Application interface."""
from __future__ import annotations

import json
import inspect
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread

import pytest
from agentloom.app import Application
from agentloom.app.factory import YamlConfiguredAgent
from agentloom.execution.concurrency import WorkItem
from jsonschema import ValidationError
from test_support.worker_batch import batch_project as _batch_project

batch_project = _batch_project


def test_single_invocation_uses_real_runtime_and_host_factory(batch_project):
    root, application, _worker, probe, _write = batch_project
    created = []

    def factory(definition, logger):
        created.append(definition)
        return YamlConfiguredAgent(config=definition, logger=logger)

    with Application.open(application, project_root=root, file_logging=False) as app:
        worker = app.worker("workflows/worker.yaml")
        with pytest.raises(ValidationError):
            worker.invoke(WorkItem("invalid", {"unexpected": 1}), agent_factory=factory)
        assert not created and not probe.starts
        result = worker.invoke(WorkItem("single", {"value": 7}), agent_factory=factory)
        assert result.output == {"value": 7}
        assert result.status == "success" and result.usage["total_tokens"] == 5
        assert result.root_run_id == app.run.run_id
        assert result.invocation_id is not None
        assert probe.closed == [{"value": 7}] and probe.active == 0
        manifest = app.run.manifest_path
    assert len(created) == 1
    assert json.loads(manifest.read_text())["status"] == "completed"


def test_single_invocation_preserves_original_control_and_receipt(batch_project):
    root, application, _worker, probe, _write = batch_project
    signal = KeyboardInterrupt("single invocation control")

    def work(payload):
        raise signal

    probe.behavior = work
    with pytest.raises(KeyboardInterrupt) as caught:
        with Application.open(application, project_root=root, file_logging=False) as app:
            app.worker("workflows/worker.yaml").invoke(WorkItem("control", {"value": 1}))
    assert caught.value is signal
    assert signal.worker_outcome.status == "failed"
    assert signal.worker_outcome.invocation_id is not None
    assert probe.closed == [{"value": 1}] and probe.active == 0


def test_single_invocation_retains_error_even_when_exception_is_false(batch_project):
    root, application, _worker, probe, _write = batch_project

    class FalseFailure(ValueError):
        def __bool__(self):
            return False

    def work(payload):
        raise FalseFailure("the invocation failed")

    probe.behavior = work
    with Application.open(application, project_root=root, file_logging=False) as app:
        result = app.worker("workflows/worker.yaml").invoke(WorkItem("failed", {"value": 1}))
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.error_type == "FalseFailure"
    assert result.error.message == "the invocation failed"
    assert probe.closed == [{"value": 1}] and probe.active == 0


def test_failed_runtime_result_preserves_measured_usage_through_agent_proxy(batch_project, monkeypatch):
    from dataclasses import replace
    from agentloom.app.agent import build_builtin_runtime_registry

    root, application, _, probe, _ = batch_project
    runtime = build_builtin_runtime_registry().validate("batch-fixture").factory
    run = runtime.run

    def fail(self, request):
        return replace(run(self, request), state="failed")

    monkeypatch.setattr(runtime, "run", fail)
    with Application.open(application, project_root=root, file_logging=False) as app:
        outcome = app.worker("workflows/worker.yaml").invoke(WorkItem("failed", {"value": 1}))
    assert outcome.status == "failed"
    assert outcome.error.error_type == "AgentRuntimeError"
    assert dict(outcome.usage) == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5,
                                  "cached_input_tokens": 0, "reasoning_tokens": 0}
    assert probe.closed == [{"value": 1}] and probe.active == 0


@pytest.mark.parametrize("phase", ["after_registration", "before_context_restore"])
def test_control_at_activity_boundary_preserves_close_and_original_signal(batch_project, phase):
    # A regressed close must fail the test with a finite watchdog, rather than
    # leave the full suite hanging in Application finalization.
    marker = "AGENTLOOM_SINGLE_INVOCATION_SIGNAL_CASE"
    if os.environ.get(marker) != phase:
        project_root = Path(__file__).resolve().parents[2]
        child = subprocess.run(
            [sys.executable, "-m", "pytest",
             f"{Path(__file__).resolve()}::test_control_at_activity_boundary_preserves_close_and_original_signal[{phase}]",
             "-q", "-o", "addopts="],
            cwd=project_root,
            env={**os.environ, marker: phase,
                 "PYTHONPATH": os.pathsep.join(filter(None, [str(project_root), os.environ.get("PYTHONPATH")]))},
            capture_output=True, text=True, timeout=20,
        )
        assert child.returncode == 0, child.stdout + child.stderr
        return

    from agentloom.app import BoundWorker

    root, application, _worker, probe, _write = batch_project
    source, first_line = inspect.getsourcelines(BoundWorker.invoke)
    markers = ("_ACTIVE_BATCH_OWNER.set(owner)",) if phase == "after_registration" else (
        "_ACTIVE_BATCH_OWNER.set(previous_owner)", "_ACTIVE_BATCH_OWNER.reset(active)"
    )
    target = next(first_line + index for index, text in enumerate(source)
                  if any(part in text for part in markers))
    original = KeyboardInterrupt("activity boundary control")
    fired = False

    def trace(frame, event, arg):
        nonlocal fired
        if (not fired and event == "line" and frame.f_code is BoundWorker.invoke.__code__
                and frame.f_lineno == target):
            fired = True
            raise original
        return trace

    with Application.open(application, project_root=root, file_logging=False) as app:
        previous_trace = sys.gettrace()
        try:
            sys.settrace(trace)
            with pytest.raises(KeyboardInterrupt) as caught:
                app.worker("workflows/worker.yaml").invoke(WorkItem("signal", {"value": 1}))
            assert caught.value is original and fired
        finally:
            sys.settrace(previous_trace)
        app.close()
    if phase == "after_registration":
        assert probe.starts == [] and probe.closed == []
    else:
        assert len(probe.starts) == 1 and probe.closed == [{"value": 1}]
        assert original.worker_outcome.status == "success"
    assert probe.active == 0


def test_application_close_waits_for_single_invocation_and_rejects_new_work(batch_project):
    root, application, _worker, probe, _write = batch_project
    release, closed = Event(), Event()
    outcomes, errors = [], []

    def work(payload):
        assert release.wait(timeout=10)
        return payload

    probe.behavior = work
    with Application.open(application, project_root=root, file_logging=False) as app:
        worker = app.worker("workflows/worker.yaml")

        def invoke():
            try:
                outcomes.append(worker.invoke(WorkItem("old", {"value": 1})))
            except BaseException as error:
                errors.append(error)

        def close():
            try:
                app.close()
                closed.set()
            except BaseException as error:
                errors.append(error)

        task = Thread(target=invoke)
        closing = Thread(target=close)
        try:
            task.start()
            probe.wait_starts(1)
            closing.start()
            assert not closed.wait(timeout=0.1)
            with pytest.raises(RuntimeError, match="closing or closed"):
                worker.invoke(WorkItem("new", {"value": 2}))
        finally:
            release.set()
            task.join(timeout=10)
            if closing.ident is not None:
                closing.join(timeout=10)
        assert not task.is_alive() and not closing.is_alive()
        assert closed.is_set() and not errors
        assert [outcome.item_id for outcome in outcomes] == ["old"]
        assert probe.closed == [{"value": 1}] and probe.active == 0
