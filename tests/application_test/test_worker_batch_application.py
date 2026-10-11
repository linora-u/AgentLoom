"""Finite batch behavior through the real Application/Agent invocation seam."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Condition, Event, Thread

import pytest
import yaml
from agentloom.execution.agent_runtime import AgentRuntimeResult, RuntimeCapabilities, RuntimeUsage
from agentloom.execution.concurrency import WorkItem
from jsonschema import ValidationError


class Probe:
    def __init__(self):
        self.condition = Condition()
        self.starts = []
        self.closed = []
        self.active = 0
        self.peak = 0
        self.behavior = lambda payload: payload
        self.close_behavior = lambda payload: None
        self.construct_error = None
        self.root_behavior = None
        self.resource_close = None

    def wait_starts(self, count):
        with self.condition:
            assert self.condition.wait_for(lambda: len(self.starts) >= count, timeout=10), self.starts


@pytest.fixture
def batch_project(tmp_path, monkeypatch):
    from agentloom.app.composition import build_builtin_runtime_registry
    from agentloom.config.config import bind_config, load_project_config
    from agentloom.execution.trace import capture_explicit_execution_context

    def write(path, config):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(config))

    write(
        tmp_path / "config/system.yaml",
        {
            "runtime": {"root_dir": str(tmp_path / "runtime")},
            "checkpoint": {"enabled": False},
            "self_learning": {"enabled": False},
            "logging": {"console_enabled": False},
            "default_toolsets": [],
        },
    )
    write(
        tmp_path / "config/model.yaml",
        {
            "model": {
                "default_model_type": "test",
                "test": {"model": "fixture/model", "adapter": "openai_chat", "api_key": "fixture-secret"},
            }
        },
    )
    application = tmp_path / "applications/batch"
    worker = application / "workflows/worker.yaml"
    write(
        worker,
        {
            "name": "worker",
            "description": "Controlled Worker.",
            "task": "Process supplied input.",
            "agent_runtime": "batch-fixture",
            "tools": [],
            "toolsets": [],
            "input_schema": {
                "type": "object",
                "properties": {"value": {}, "action": {}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    )
    probe = Probe()

    class Runtime:
        runtime_id = "batch-fixture"
        capabilities = RuntimeCapabilities(False, False, True, True)

        def __init__(self, definition):
            self.definition = definition
            self.payload = None
            self.closed = False
            if probe.construct_error is not None:
                raise probe.construct_error

        def run(self, request):
            if "Input data (JSON):\n" not in request.task:
                return AgentRuntimeResult(state="success", output=probe.root_behavior())
            self.payload = json.loads(request.task.split("Input data (JSON):\n", 1)[1])
            execution = capture_explicit_execution_context()
            if probe.resource_close is not None:
                from agentloom.execution.resources import register_resource

                register_resource(
                    "same-resource-key",
                    lambda: probe.resource_close(self.payload, self.definition.instance_id),
                    instance_id=self.definition.instance_id,
                )
            with probe.condition:
                probe.starts.append((self.payload, execution, self.definition, request))
                probe.active += 1
                probe.peak = max(probe.peak, probe.active)
                probe.condition.notify_all()
            return AgentRuntimeResult(
                state="success",
                output=probe.behavior(self.payload),
                usage=RuntimeUsage(input_tokens=3, output_tokens=2, total_tokens=5),
            )

        def snapshot(self):
            return None

        def close(self):
            if self.closed:
                return
            probe.close_behavior(self.payload)
            self.definition.tool_gateway.close()
            self.closed = True
            if self.payload is not None:
                with probe.condition:
                    probe.closed.append(self.payload)
                    probe.active -= 1
                    probe.condition.notify_all()

    registry = build_builtin_runtime_registry()
    registry.register("batch-fixture", capabilities=Runtime.capabilities, factory=Runtime)
    monkeypatch.setattr("agentloom.app.validation.build_builtin_runtime_registry", lambda: registry)
    monkeypatch.setattr("agentloom.app.agent.build_builtin_runtime_registry", lambda: registry)
    original_cwd = Path.cwd()
    try:
        with bind_config(load_project_config(tmp_path)):
            yield tmp_path, application, worker, probe, write
    finally:
        os.chdir(original_cwd)


def test_application_open_owns_one_run_and_returns_ordered_worker_receipts(batch_project):
    from agentloom.app import Application

    root, application, _worker, probe, _write = batch_project
    with Application.open(application, project_root=root, file_logging=False) as app:
        result = app.worker("workflows/worker.yaml").map([WorkItem("a", {"value": 1}), WorkItem("b", {"value": 2})])
        record = app.runtime_context.read_batch_record(result.batch_id)
        assert [item.output for item in result.outcomes] == [{"value": 1}, {"value": 2}]
        assert result.status == "completed"
        assert result.execution.concurrency == 1
        assert all(item.usage["total_tokens"] == 5 for item in result.outcomes)
        assert {item.root_run_id for item in result.outcomes} == {app.run.run_id}
        assert len({item.invocation_id for item in result.outcomes}) == 2
        assert record["input_fingerprint"] == result.input_fingerprint
        manifest_path = app.run.manifest_path
    assert len(probe.closed) == 2
    assert json.loads(manifest_path.read_text())["status"] == "completed"


@contextmanager
def background(call):
    from contextvars import copy_context

    results, errors = [], []
    context = copy_context()

    def run():
        try:
            results.append(context.run(call))
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=run)
    thread.start()
    try:
        yield results, errors, thread
    finally:
        thread.join(timeout=12)
        assert not thread.is_alive(), "batch failed to drain"


@pytest.mark.parametrize("capacity", [1, 2, 20, 32, 65])
def test_capacity_is_attainable_and_never_clipped_or_exceeded(batch_project, capacity):
    from agentloom.app import Application

    root, application, _worker, probe, _write = batch_project
    gate = Event()

    def hold(payload):
        assert gate.wait(timeout=10)
        return payload["value"]

    probe.behavior = hold
    with Application.open(application, project_root=root, file_logging=False) as app:
        with background(
            lambda: app.worker("workflows/worker.yaml").map(
                [WorkItem(str(i), {"value": i}) for i in range(capacity + 2)], concurrency=capacity
            )
        ) as (results, errors, _thread):
            try:
                probe.wait_starts(capacity)
                assert probe.peak == capacity
            finally:
                gate.set()
        assert not errors
        assert [outcome.output for outcome in results[0].outcomes] == list(range(capacity + 2))
    assert probe.peak == capacity


def test_existing_application_run_exposes_the_same_owner_to_batch_tools(batch_project):
    from agentloom.app import current_run_app
    from agentloom.app.runner import execute_app

    root, application, worker, probe, write = batch_project
    workflow = application / "workflows/root.yaml"
    write(
        workflow,
        {
            "name": "batch-root",
            "description": "Orchestrate.",
            "task": "Execute business plan.",
            "agent_runtime": "batch-fixture",
            "tools": [],
            "toolsets": [],
        },
    )
    receipts = []

    def business():
        app = current_run_app()
        result = app.worker("workflows/worker.yaml").map([WorkItem("child", {"value": 7})])
        receipts.append(result)
        return result.outcomes[0].output

    probe.root_behavior = business
    result = execute_app(workflow, file_logging=False)
    assert result.output == {"value": 7}
    assert receipts[0].run_id == result.run.run_id
    assert receipts[0].outcomes[0].root_run_id == result.run.run_id
    assert json.loads(result.run.manifest_path.read_text())["worker_batches"][0]["batch_id"] == receipts[0].batch_id


def test_runner_retains_root_context_engine_until_background_batch_drains(batch_project):
    from contextvars import copy_context

    from agentloom.app import current_run_app
    from agentloom.app.runner import execute_app
    from agentloom.execution.context_engine.runtime import get_active_context_engine

    root, application, _worker, probe, write = batch_project
    workflow = application / "workflows/root.yaml"
    write(
        workflow,
        {
            "name": "batch-root",
            "description": "Orchestrate.",
            "task": "Execute business plan.",
            "agent_runtime": "batch-fixture",
            "tools": [],
            "toolsets": [],
        },
    )
    gate, returned = Event(), Event()
    batches, errors, threads, owners = [], [], [], []

    def use_root_store(payload):
        assert gate.wait(timeout=10)
        get_active_context_engine().capture_tool_result("long result\n" * 400, tool_name="fixture", source="worker")
        return "stored-after-supervisor-return"

    probe.behavior = use_root_store

    def business():
        owner = current_run_app()
        owners.append(owner)
        worker = owner.worker("workflows/worker.yaml")
        context = copy_context()

        def batch():
            try:
                batches.append(context.run(worker.map, [WorkItem("child", {"value": 7})]))
            except BaseException as exc:
                errors.append(exc)

        thread = Thread(target=batch)
        threads.append(thread)
        thread.start()
        probe.wait_starts(1)
        returned.set()
        return "supervisor-finished"

    probe.root_behavior = business
    with background(lambda: execute_app(workflow, file_logging=False)) as (results, runner_errors, runner_thread):
        try:
            assert returned.wait(timeout=10)
            assert runner_thread.is_alive()
            from time import monotonic

            deadline = monotonic() + 10
            while True:
                try:
                    owners[0].worker("workflows/worker.yaml")
                except RuntimeError:
                    break
                assert monotonic() < deadline, "runner did not begin owner drain"
                Event().wait(0.01)
        finally:
            gate.set()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert not errors and not runner_errors
    assert batches[0].outcomes[0].output == "stored-after-supervisor-return"
    assert batches[0].outcomes[0].root_run_id == results[0].run.run_id


def test_runner_owner_baseline_includes_root_engine_for_unbound_caller(batch_project):
    from agentloom.app import current_run_app
    from agentloom.app.runner import execute_app
    from agentloom.execution.context_engine.runtime import get_active_context_engine

    root, application, _worker, probe, write = batch_project
    workflow = application / "workflows/root.yaml"
    write(
        workflow,
        {
            "name": "batch-root",
            "description": "Orchestrate.",
            "task": "Execute business plan.",
            "agent_runtime": "batch-fixture",
            "tools": [],
            "toolsets": [],
        },
    )
    observed, errors = [], []

    def identify(payload):
        observed.append((current_run_app().run.run_id, get_active_context_engine()))
        return "owned"

    probe.behavior = identify
    expected = []

    def business():
        app = current_run_app()
        expected.append((app.run.run_id, get_active_context_engine()))
        worker = app.worker("workflows/worker.yaml")

        def unbound():
            try:
                worker.map([WorkItem("child", {"value": 1})])
            except BaseException as exc:
                errors.append(exc)

        thread = Thread(target=unbound)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()
        return "done"

    probe.root_behavior = business
    result = execute_app(workflow, file_logging=False)
    assert not errors and observed == expected
    assert observed[0][0] == result.run.run_id


def test_exceptional_scope_exit_drains_before_closing_shared_context_engine(batch_project):
    from contextvars import copy_context
    from time import monotonic

    from agentloom.app import Application
    from agentloom.execution.context_engine.runtime import get_active_context_engine

    root, application, _worker, probe, _write = batch_project
    gate, leaving = Event(), Event()
    shared = {"outcomes": []}

    def use_root_store(payload):
        assert gate.wait(timeout=10)
        engine = get_active_context_engine()
        engine.capture_tool_result("long result\n" * 400, tool_name="fixture", source="worker")
        return "stored-after-exit-request"

    probe.behavior = use_root_store

    class UserFailure(Exception):
        pass

    def scope():
        try:
            with Application.open(application, project_root=root, file_logging=False) as app:
                shared["binding"] = app.worker("workflows/worker.yaml")
                context = copy_context()
                thread = Thread(
                    target=lambda: shared["outcomes"].append(
                        context.run(shared["binding"].map, [WorkItem("a", {"value": 1})])
                    )
                )
                thread.start()
                shared["thread"] = thread
                probe.wait_starts(1)
                leaving.set()
                raise UserFailure("business failed")
        finally:
            if "thread" in shared:
                shared["thread"].join(timeout=10)

    with background(scope) as (_results, errors, thread):
        assert leaving.wait(timeout=10)
        deadline = monotonic() + 10
        while monotonic() < deadline:
            try:
                shared["binding"].map([])
            except RuntimeError as exc:
                assert "closed" in str(exc)
                break
        else:
            pytest.fail("owner never stopped registration")
        assert thread.is_alive()
        gate.set()
    assert len(errors) == 1 and isinstance(errors[0], UserFailure)
    assert shared["outcomes"][0].outcomes[0].status == "success"
    assert shared["outcomes"][0].outcomes[0].output == "stored-after-exit-request"


def test_standalone_batch_preserves_worker_checkpoint_association(batch_project):
    from agentloom.app import Application

    root, application, _worker, probe, write = batch_project
    path = root / "config/system.yaml"
    settings = yaml.safe_load(path.read_text())
    settings["checkpoint"] = {"enabled": True, "cleanup_on_success": False}
    write(path, settings)
    with Application.open(application, project_root=root, file_logging=False) as app:
        receipt = app.worker("workflows/worker.yaml").map(
            [WorkItem("a", {"value": 1}), WorkItem("b", {"value": 2})], concurrency=2
        )
        task_tree = app.runtime_context.task_tree_path
        assert all(outcome.status == "success" for outcome in receipt.outcomes)
    tree = json.loads(task_tree.read_text())
    assert tree["status"] == "completed"
    assert len(tree["workers"]["worker"]) == 2
    assert [call["status"] for call in tree["workers"]["worker"]] == ["completed", "completed"]
    assert {call["attempt_run_id"] for call in tree["workers"]["worker"]} == {receipt.run_id}


def test_capacity_above_input_count_runs_only_supplied_items_and_empty_none_are_valid(batch_project):
    from agentloom.app import Application

    root, application, _worker, probe, _write = batch_project
    probe.behavior = lambda payload: None
    with Application.open(application, project_root=root, file_logging=False) as app:
        worker = app.worker("workflows/worker.yaml")
        empty = worker.map([], concurrency=129)
        assert empty.status == "completed" and empty.outcomes == () and probe.starts == []
        result = worker.map([WorkItem("a", {"value": 1}), WorkItem("b", {"value": 2})], concurrency=129)
        assert [o.status for o in result.outcomes] == ["success", "success"]
        assert [o.output for o in result.outcomes] == [None, None]
    assert len(probe.starts) == 2


@pytest.mark.parametrize("kind", ["duplicate", "late_schema", "generator"])
def test_entire_plan_is_validated_before_any_worker_start(batch_project, kind):
    from agentloom.app import Application

    root, application, _worker, probe, _write = batch_project
    items = [WorkItem("a", {"value": 1}), WorkItem("b", {"value": 2})]
    if kind == "duplicate":
        items.append(WorkItem("a", {"value": 3}))
    elif kind == "late_schema":
        items.append(WorkItem("c", {"unrecognized": True}))
    else:
        items = iter(items)
    with Application.open(application, project_root=root, file_logging=False) as app:
        with pytest.raises((ValueError, ValidationError)):
            app.worker("workflows/worker.yaml").map(items, concurrency=4)
    assert probe.starts == []


def test_call_overrides_sources_and_binding_snapshots_are_pinned_per_scope(batch_project):
    from agentloom.app import Application

    root, application, definition, probe, write = batch_project
    system = root / "config/system.yaml"
    values = yaml.safe_load(system.read_text())
    values["worker_execution"] = {"defaults": {"concurrency": 3}}
    write(system, values)
    write(
        application / "config/system.yaml",
        {
            "worker_execution": {
                "defaults": {"concurrency": 5},
                "workers": {"workflows/worker.yaml": {"concurrency": 20}},
            }
        },
    )
    with Application.open(application, project_root=root, file_logging=False) as app:
        worker = app.worker("workflows/worker.yaml")
        before = definition.read_text()
        changed = yaml.safe_load(before)
        changed["task"] = "Changed definition after binding."
        write(definition, changed)
        write(application / "config/system.yaml", {"worker_execution": {"defaults": {"concurrency": 9}}})
        receipts = [
            worker.map([WorkItem("a", {"value": n})], concurrency=value) for n, value in enumerate([4, None, 2])
        ]
        assert [r.execution.concurrency for r in receipts] == [4, 20, 2]
        assert [entry.source for entry in receipts[0].execution.source_chain] == [
            "framework_default",
            "project_system",
            "application_system",
            "application_worker",
            "call_override",
        ]
        assert app.worker("workflows/./worker.yaml") is worker
        assert all("Process supplied input." in start[3].task for start in probe.starts)
        assert len({r.definition_revision for r in receipts}) == 1
        assert "fixture-secret" not in json.dumps(app.runtime_context.read_batch_record(receipts[0].batch_id))
    with Application.open(application, project_root=root, file_logging=False) as app:
        updated = app.worker("workflows/worker.yaml").map([WorkItem("a", {"value": 8})])
    assert updated.execution.concurrency == 9
    assert updated.definition_revision != receipts[0].definition_revision
    assert "Changed definition after binding." in probe.starts[-1][3].task


def test_cached_worker_binding_survives_deleted_or_replaced_definition(batch_project):
    from agentloom.app import Application

    root, application, definition, probe, write = batch_project
    with Application.open(application, project_root=root, file_logging=False) as app:
        worker = app.worker("workflows/worker.yaml")
        definition.unlink()
        assert app.worker("workflows/./worker.yaml") is worker
        worker.map([WorkItem("deleted", {"value": 1})])
        write(definition, {"unrecognized": "replacement is invalid"})
        assert app.worker("workflows/worker.yaml") is worker
        worker.map([WorkItem("replaced", {"value": 2})])
    assert len(probe.starts) == 2
    assert all("Process supplied input." in start[3].task for start in probe.starts)
    with Application.open(application, project_root=root, file_logging=False) as app:
        with pytest.raises(ValueError):
            app.worker("workflows/worker.yaml")


@pytest.mark.parametrize("checkpoint", [False, True])
def test_worker_rebinds_its_owner_from_another_run_or_unbound_thread(batch_project, checkpoint):
    from agentloom.app import Application, current_run_app
    from agentloom.execution import get_current_run_context
    from agentloom.execution.context_engine.runtime import get_active_context_engine

    root, application, _definition, probe, write = batch_project
    config = yaml.safe_load((root / "config/system.yaml").read_text())
    config["checkpoint"]["enabled"] = checkpoint
    write(root / "config/system.yaml", config)
    observed = []

    def identify(payload):
        owner = current_run_app()
        observed.append((owner.run.run_id, get_current_run_context().run_id, get_active_context_engine()))
        return payload["value"]

    probe.behavior = identify
    with Application.open(application, project_root=root, file_logging=False) as first:
        bound = first.worker("workflows/worker.yaml")
        first_engine = get_active_context_engine()
        with Application.open(application, project_root=root, file_logging=False) as second:
            second_engine = get_active_context_engine()
            assert second_engine is not first_engine
            bound.map([WorkItem("cross-owner", {"value": 1})])
            second.worker("workflows/worker.yaml").map([WorkItem("own", {"value": 2})])
            assert observed[0] == (first.run.run_id, first.run.run_id, first_engine)
            assert observed[1] == (second.run.run_id, second.run.run_id, second_engine)
        results, errors = [], []

        def without_context():
            try:
                results.append(bound.map([WorkItem("thread", {"value": 3})]))
            except BaseException as exc:
                errors.append(exc)

        thread = Thread(target=without_context)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive() and not errors
        assert observed[2] == (first.run.run_id, first.run.run_id, first_engine)
        assert {o.root_run_id for result in results for o in result.outcomes} == {first.run.run_id}


def test_nested_execute_app_owns_an_independent_run_and_restores_outer_services(batch_project):
    from agentloom.app import Application, current_run_app
    from agentloom.app.runner import execute_app
    from agentloom.execution.context_engine.runtime import get_active_context_engine

    root, application, _worker, probe, write = batch_project
    workflow = application / "workflows/root.yaml"
    write(
        workflow,
        {
            "name": "batch-root",
            "description": "Orchestrate.",
            "task": "Execute business plan.",
            "agent_runtime": "batch-fixture",
            "tools": [],
            "toolsets": [],
        },
    )
    receipts, engines = [], []

    def business():
        app = current_run_app()
        engines.append(get_active_context_engine())
        receipt = app.worker("workflows/worker.yaml").map([WorkItem("inner", {"value": 1})])
        receipts.append(receipt)
        return receipt.outcomes[0].output

    probe.root_behavior = business
    with Application.open(application, project_root=root, file_logging=False) as outer:
        outer_engine = get_active_context_engine()
        result = execute_app(workflow, file_logging=False)
        assert current_run_app() is outer and get_active_context_engine() is outer_engine
        assert engines[0] is not outer_engine
        assert receipts[0].outcomes[0].root_run_id == result.run.run_id
        assert result.run.run_id != outer.run.run_id


@pytest.mark.parametrize(
    "policy",
    [
        {"workers": {"workflows/missing.yaml": {"concurrency": 2}}},
        {"workers": {"workflows/worker.yaml": {"concurrency": 2}, "workflows/./worker.yaml": {"concurrency": 3}}},
        {"workers": {"../outside.yaml": {"concurrency": 2}}},
        {"defaults": {"concurreny": 2}},
        {"defaults": {"concurrency": True}},
    ],
)
def test_invalid_unused_policy_targets_and_fields_reject_composition(batch_project, policy):
    from agentloom.app import Application

    root, application, _worker, probe, write = batch_project
    write(application / "config/system.yaml", {"worker_execution": policy})
    with pytest.raises((ValueError, FileNotFoundError)):
        with Application.open(application, project_root=root, file_logging=False):
            pytest.fail("invalid composition accepted")
    assert probe.starts == []


def test_valid_unused_worker_policy_is_allowed_and_symlink_alias_is_rejected(batch_project):
    from agentloom.app import Application

    root, application, worker, probe, write = batch_project
    unused = application / "workflows/unused.yaml"
    write(unused, yaml.safe_load(worker.read_text()))
    write(
        application / "config/system.yaml",
        {"worker_execution": {"workers": {"workflows/unused.yaml": {"concurrency": 2}}}},
    )
    with Application.open(application, project_root=root, file_logging=False) as app:
        assert app.worker("workflows/worker.yaml").map([]).execution.concurrency == 1
    alias = application / "workflows/alias.yaml"
    alias.symlink_to(worker)
    write(
        application / "config/system.yaml",
        {"worker_execution": {"workers": {"workflows/alias.yaml": {"concurrency": 2}}}},
    )
    with pytest.raises(ValueError, match="symlink"):
        with Application.open(application, project_root=root, file_logging=False):
            pass
    assert probe.starts == []


def test_fast_later_completion_refills_capacity_before_first_item_and_keeps_order(batch_project):
    from agentloom.app import Application

    root, application, _worker, probe, _write = batch_project
    first = Event()
    fast_observed = Event()
    progress = []

    def work(payload):
        if payload["value"] == 0:
            assert first.wait(timeout=10)
        return payload["value"]

    def observe(event):
        progress.append(event)
        if event.outcome.item_id == "1":
            fast_observed.set()

    probe.behavior = work
    with Application.open(application, project_root=root, file_logging=False) as app:
        with background(
            lambda: app.worker("workflows/worker.yaml").map(
                [WorkItem(str(i), {"value": i}) for i in range(4)], concurrency=2, event_sink=observe
            )
        ) as (results, errors, _thread):
            try:
                assert fast_observed.wait(timeout=10)
                probe.wait_starts(4)
                assert progress[0].outcome.item_id == "1"
            finally:
                first.set()
        assert not errors
        assert [o.output for o in results[0].outcomes] == [0, 1, 2, 3]
        assert [e.sequence for e in progress] == list(range(1, 5))


def test_input_snapshots_and_per_invocation_mutable_copies_do_not_cross_contaminate(batch_project):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkPlan

    root, application, _worker, probe, _write = batch_project
    original = {"value": {"nested": ["original"]}}
    plan = WorkPlan([WorkItem("a", original), WorkItem("b", original)])
    gate = Event()

    def change(payload):
        assert gate.wait(timeout=10)
        seen = payload["value"]["nested"][:]
        payload["value"]["nested"].append("worker-mutated")
        return seen

    probe.behavior = change
    with Application.open(application, project_root=root, file_logging=False) as app:
        with background(lambda: app.worker("workflows/worker.yaml").map(plan, concurrency=2)) as (
            results,
            errors,
            _thread,
        ):
            probe.wait_starts(2)
            original["value"]["nested"][0] = "caller-mutated"
            gate.set()
        assert not errors
        assert [o.output for o in results[0].outcomes] == [("original",), ("original",)]
        assert results[0].input_fingerprint == plan.input_fingerprint
        record = app.runtime_context.read_batch_record(results[0].batch_id)
        assert record["input_fingerprint"] == plan.input_fingerprint
    assert plan.items[0].mutable_input()["value"]["nested"] == ["original"]


@pytest.mark.parametrize("mode", ["collect", "fail_fast"])
def test_ordinary_failures_keep_dispositions_without_whole_worker_replay(batch_project, mode):
    from agentloom.app import Application

    root, application, _worker, probe, _write = batch_project
    side_effects = []

    def work(payload):
        side_effects.append(payload["value"])
        if payload["value"] == 0:
            raise RuntimeError("after side effect")
        return payload["value"]

    probe.behavior = work
    with Application.open(application, project_root=root, file_logging=False) as app:
        result = app.worker("workflows/worker.yaml").map(
            [WorkItem(str(i), {"value": i}) for i in range(4)], concurrency=1, failure_mode=mode
        )
    assert result.status == "failed"
    assert result.outcomes[0].error.message == "after side effect"
    assert result.outcomes[0].invocation_id is not None
    if mode == "collect":
        assert [o.status for o in result.outcomes] == ["failed", "success", "success", "success"]
        assert side_effects == [0, 1, 2, 3]
    else:
        assert [o.status for o in result.outcomes] == ["failed", "canceled", "canceled", "canceled"]
        assert all(o.invocation_id is None and o.cancellation_reason for o in result.outcomes[1:])
        assert side_effects == [0]


def test_stop_waits_for_actual_runtime_and_cleanup_and_never_marks_started_work_canceled(batch_project):
    from agentloom.app import Application

    root, application, _worker, probe, _write = batch_project
    stop, run_gate, cleanup_gate, closing = Event(), Event(), Event(), Event()

    def work(payload):
        assert run_gate.wait(timeout=10)
        return payload["value"]

    def cleanup(payload):
        closing.set()
        assert cleanup_gate.wait(timeout=10)

    probe.behavior, probe.close_behavior = work, cleanup
    with Application.open(application, project_root=root, file_logging=False) as app:
        with background(
            lambda: app.worker("workflows/worker.yaml").map(
                [WorkItem(str(i), {"value": i}) for i in range(3)], concurrency=1, stop_token=stop
            )
        ) as (results, errors, thread):
            probe.wait_starts(1)
            stop.set()
            assert thread.is_alive()
            run_gate.set()
            assert closing.wait(timeout=10)
            assert thread.is_alive() and len(probe.starts) == 1
            cleanup_gate.set()
        assert not errors
    assert results[0].status == "stopped"
    assert [o.status for o in results[0].outcomes] == ["success", "canceled", "canceled"]
    assert results[0].outcomes[0].invocation_id is not None
    assert all(o.invocation_id is None for o in results[0].outcomes[1:])


def test_stop_at_submitted_startup_seam_has_no_fictitious_invocation_identity(batch_project):
    from threading import get_ident

    from agentloom.app import Application

    root, application, _worker, probe, _write = batch_project
    startup, release, requested = Event(), Event(), Event()
    controller = []

    class Token:
        def is_set(self):
            if get_ident() != controller[0]:
                startup.set()
                assert release.wait(timeout=10)
            return requested.is_set()

    def call(app):
        controller.append(get_ident())
        return app.worker("workflows/worker.yaml").map([WorkItem("a", {"value": 1})], stop_token=Token())

    with Application.open(application, project_root=root, file_logging=False) as app:
        with background(lambda: call(app)) as (results, errors, _thread):
            assert startup.wait(timeout=10)
            requested.set()
            release.set()
        assert not errors
    assert probe.starts == []
    assert results[0].status == "stopped"
    assert results[0].outcomes[0].status == "canceled"
    assert results[0].outcomes[0].invocation_id is None


def test_two_batches_have_independent_capacity_stop_tokens_and_records(batch_project):
    from agentloom.app import Application

    root, application, _worker, probe, _write = batch_project
    left_gate, right_gate, left_stop = Event(), Event(), Event()

    def hold(payload):
        gate = left_gate if payload["value"].startswith("left") else right_gate
        assert gate.wait(timeout=10)
        return payload["value"]

    probe.behavior = hold
    with Application.open(application, project_root=root, file_logging=False) as app:
        worker = app.worker("workflows/worker.yaml")
        with background(
            lambda: worker.map(
                [WorkItem(str(i), {"value": f"left{i}"}) for i in range(6)], concurrency=4, stop_token=left_stop
            )
        ) as (left, left_errors, _):
            with background(
                lambda: worker.map([WorkItem(str(i), {"value": f"right{i}"}) for i in range(6)], concurrency=4)
            ) as (right, right_errors, _):
                try:
                    probe.wait_starts(8)
                    assert probe.peak == 8
                    left_stop.set()
                finally:
                    left_gate.set()
                    right_gate.set()
        assert not left_errors and not right_errors
        assert left[0].status == "stopped" and right[0].status == "completed"
        assert sum(o.status == "success" for o in left[0].outcomes) == 4
        references = json.loads(app.run.manifest_path.read_text())["worker_batches"]
        assert {r["batch_id"] for r in references} == {left[0].batch_id, right[0].batch_id}
        assert app.runtime_context.read_batch_record(left[0].batch_id)["status"] == "stopped"
        assert app.runtime_context.read_batch_record(right[0].batch_id)["status"] == "completed"


def test_nested_child_map_uses_independent_window_and_self_close_fails_immediately(batch_project):
    from agentloom.app import Application, current_run_app

    root, application, _worker, probe, _write = batch_project

    def work(payload):
        if payload["value"] == "parent":
            return (
                current_run_app()
                .worker("workflows/worker.yaml")
                .map([WorkItem("child", {"value": "child"})], concurrency=1)
                .outcomes[0]
                .output
            )
        if payload["value"] == "close":
            current_run_app().close()
        return payload["value"]

    probe.behavior = work
    with Application.open(application, project_root=root, file_logging=False) as app:
        worker = app.worker("workflows/worker.yaml")
        result = worker.map([WorkItem("parent", {"value": "parent"})], concurrency=1)
        assert result.outcomes[0].output == "child"
        failure = worker.map([WorkItem("close", {"value": "close"})])
        assert failure.outcomes[0].status == "failed"
        assert "own Application owner" in failure.outcomes[0].error.message
    with pytest.raises(RuntimeError, match="closed"):
        worker.map([])


@pytest.mark.parametrize("control", [False, True])
def test_observer_failure_is_diagnostic_or_control_interrupt_and_started_work_drains(batch_project, control):
    from agentloom.app import Application

    root, application, _worker, probe, _write = batch_project
    observed = []

    def observer(event):
        observed.append(event)
        if control:
            raise KeyboardInterrupt("observer interrupt")
        raise ValueError("observer diagnostic")

    with Application.open(application, project_root=root, file_logging=False) as app:
        worker = app.worker("workflows/worker.yaml")
        if control:
            with pytest.raises(KeyboardInterrupt, match="observer interrupt") as caught:
                worker.map([WorkItem(str(i), {"value": i}) for i in range(4)], concurrency=1, event_sink=observer)
            receipt = caught.value.batch_outcome
            assert [o.status for o in receipt.outcomes] == ["success", "canceled", "canceled", "canceled"]
            assert len(probe.closed) == 1
            assert app.runtime_context.read_batch_record(receipt.batch_id)["status"] == "stopped"
        else:
            receipt = worker.map([WorkItem(str(i), {"value": i}) for i in range(4)], event_sink=observer)
            assert receipt.status == "completed"
            assert len(receipt.observer_errors) == 1
            assert receipt.observer_errors[0].message == "observer diagnostic"
        assert len(observed) == 1


def test_runtime_construction_failure_is_an_ordinary_collectable_item_error(batch_project):
    from agentloom.app import Application

    root, application, _worker, probe, _write = batch_project
    probe.construct_error = RuntimeError("Runtime builder unavailable")
    with Application.open(application, project_root=root, file_logging=False) as app:
        receipt = app.worker("workflows/worker.yaml").map([WorkItem("a", {"value": 1}), WorkItem("b", {"value": 2})])
    assert [o.status for o in receipt.outcomes] == ["failed", "failed"]
    assert all(o.error.message == "Runtime builder unavailable" for o in receipt.outcomes)
    assert all(o.error.category == "runtime_creation" for o in receipt.outcomes)


def test_owned_resource_cleanup_holds_capacity_and_only_closes_its_invocation(batch_project):
    from agentloom.app import Application

    root, application, _worker, probe, _write = batch_project
    cleanup_started, cleanup_gate = Event(), Event()
    closed_owners = []

    def close_resource(payload, identity):
        cleanup_started.set()
        assert cleanup_gate.wait(timeout=10)
        closed_owners.append(identity)

    probe.resource_close = close_resource
    with Application.open(application, project_root=root, file_logging=False) as app:
        with background(
            lambda: app.worker("workflows/worker.yaml").map(
                [WorkItem(str(i), {"value": i}) for i in range(3)], concurrency=1
            )
        ) as (results, errors, thread):
            assert cleanup_started.wait(timeout=10)
            assert len(probe.starts) == 1 and thread.is_alive()
            cleanup_gate.set()
        assert not errors
        assert closed_owners == [o.invocation_id for o in results[0].outcomes]
    assert len(closed_owners) == 3


def test_cleanup_failure_stops_replenishment_and_retains_primary_cause_for_root_retry(batch_project):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkerBatchError

    root, application, _worker, probe, _write = batch_project
    attempts = []

    def close_resource(payload, identity):
        attempts.append(identity)
        if len(attempts) == 1:
            raise OSError("owned handle close failed")

    probe.resource_close = close_resource

    def fail_after_side_effect(payload):
        raise ValueError("primary invocation failure")

    probe.behavior = fail_after_side_effect
    with Application.open(application, project_root=root, file_logging=False) as app:
        with pytest.raises(WorkerBatchError) as caught:
            app.worker("workflows/worker.yaml").map([WorkItem(str(i), {"value": i}) for i in range(3)], concurrency=1)
        assert caught.value.phase == "cleanup"
        assert isinstance(caught.value.original_error, ValueError)
        assert caught.value.outcomes[0].error.message == "primary invocation failure"
        assert caught.value.cleanup_errors[0].message == "owned handle close failed"
        assert [o.status for o in caught.value.outcomes] == ["failed", "canceled", "canceled"]
        assert len(probe.starts) == 1
        assert len(attempts) == 1
    assert len(attempts) == 2 and len(set(attempts)) == 1


def test_runtime_close_failure_keeps_output_and_capacity_until_original_owner_retry(batch_project):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkerBatchError

    root, application, _worker, probe, _write = batch_project
    attempts = []

    def close(payload):
        if payload is not None:
            attempts.append(payload["value"])
            if len(attempts) == 1:
                raise OSError("Runtime close failed")

    probe.close_behavior = close
    with Application.open(application, project_root=root, file_logging=False) as app:
        with pytest.raises(WorkerBatchError) as caught:
            app.worker("workflows/worker.yaml").map([WorkItem("a", {"value": 1}), WorkItem("b", {"value": 2})])
        assert caught.value.phase == "cleanup"
        assert caught.value.outcomes[0].output == {"value": 1}
        assert caught.value.cleanup_errors[0].phase == "runtime_close"
        assert caught.value.original_error.args == ("Runtime close failed",)
        assert [o.status for o in caught.value.outcomes] == ["failed", "canceled"]
        assert probe.active == 1 and not probe.closed and len(probe.starts) == 1
    assert attempts == [1, 1]
    assert probe.active == 0 and probe.closed == [{"value": 1}]


def test_root_cleanup_failure_raises_and_marks_run_failed_after_caught_batch_error(batch_project):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkerBatchError

    root, application, _worker, probe, _write = batch_project
    attempts = []

    def close(payload, identity):
        attempts.append(identity)
        raise OSError("owned handle still open")

    probe.resource_close = close
    with pytest.raises(ExceptionGroup) as caught:
        with Application.open(application, project_root=root, file_logging=False) as app:
            manifest = app.run.manifest_path
            with pytest.raises(WorkerBatchError):
                app.worker("workflows/worker.yaml").map([WorkItem("a", {"value": 1})])
    assert caught.value.exceptions[0].args == ("owned handle still open",)
    assert len(attempts) == 2 and len(set(attempts)) == 1
    record = json.loads(manifest.read_text())
    assert record["status"] == "failed" and app.lifecycle.phase == "cleanup"


def test_owner_close_registration_race_drains_accepted_and_rejects_zero_work(batch_project):
    from agentloom.app import Application

    root, application, _worker, probe, _write = batch_project
    gate = Event()

    def hold(payload):
        assert gate.wait(timeout=10)
        return payload["value"]

    probe.behavior = hold
    receipts, rejected, failures = {}, {}, []
    start = Barrier(9)
    with Application.open(application, project_root=root, file_logging=False) as app:
        worker = app.worker("workflows/worker.yaml")
        with background(lambda: worker.map([WorkItem("accepted", {"value": "accepted"})])) as (
            initial,
            initial_errors,
            initial_thread,
        ):
            probe.wait_starts(1)

            def contender(index):
                start.wait(timeout=10)
                try:
                    receipts[index] = worker.map([WorkItem(str(index), {"value": index})])
                except RuntimeError as exc:
                    rejected[index] = exc
                except BaseException as exc:
                    failures.append(exc)

            callers = [Thread(target=contender, args=(i,)) for i in range(8)]
            for thread in callers:
                thread.start()
            with background(lambda: (start.wait(timeout=10), app.close())) as (_closed, close_errors, close_thread):
                from time import monotonic

                deadline = monotonic() + 10
                try:
                    while True:
                        try:
                            app.worker("workflows/worker.yaml")
                        except RuntimeError:
                            break
                        assert monotonic() < deadline
                        Event().wait(0.01)
                    assert close_thread.is_alive() and initial_thread.is_alive()
                finally:
                    gate.set()
            for thread in callers:
                thread.join(timeout=10)
                assert not thread.is_alive()
        assert not initial_errors and not close_errors and not failures
        assert len(receipts) + len(rejected) == 8
        started_values = {payload["value"] for payload, *_ in probe.starts}
        assert not started_values.intersection(rejected)
        assert all("closing or closed" in str(error) for error in rejected.values())
        assert initial[0].outcomes[0].status == "success"
        for index, receipt in receipts.items():
            outcome = receipt.outcomes[0]
            assert (outcome.status == "success") == (index in started_values)
            assert (outcome.invocation_id is not None) == (index in started_values)
    assert probe.active == 0 and len(probe.closed) == len(probe.starts)


def test_required_batch_persistence_failure_drains_and_exposes_partial_receipt(batch_project, monkeypatch):
    from agentloom.app import Application
    from agentloom.execution import RuntimeContext
    from agentloom.execution.concurrency import WorkerBatchError

    root, application, _worker, probe, _write = batch_project
    original = RuntimeContext.write_batch_record
    writes = []

    def write(context, batch_id, record):
        writes.append(record["status"])
        if len(writes) >= 2:
            raise OSError("required batch disk full")
        return original(context, batch_id, record)

    monkeypatch.setattr(RuntimeContext, "write_batch_record", write)
    with Application.open(application, project_root=root, file_logging=False) as app:
        with pytest.raises(WorkerBatchError) as caught:
            app.worker("workflows/worker.yaml").map([WorkItem(str(i), {"value": i}) for i in range(3)], concurrency=1)
        assert caught.value.phase == "persistence"
        assert isinstance(caught.value.original_error, OSError)
        assert [o.status for o in caught.value.outcomes] == ["success", "canceled", "canceled"]
        assert len(probe.closed) == len(probe.starts) == 1


def test_required_trace_failure_is_infrastructure_instead_of_collectable_item_error(batch_project):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkerBatchError
    from agentloom.execution.observability import TraceStorageError

    root, application, _worker, probe, _write = batch_project

    def failed_evidence(payload):
        raise TraceStorageError("required trace missing")

    probe.behavior = failed_evidence
    with Application.open(application, project_root=root, file_logging=False) as app:
        with pytest.raises(WorkerBatchError) as caught:
            app.worker("workflows/worker.yaml").map(
                [WorkItem("a", {"value": 1}), WorkItem("b", {"value": 2})], concurrency=1
            )
        assert isinstance(caught.value.original_error, TraceStorageError)
        assert [o.status for o in caught.value.outcomes] == ["failed", "canceled"]
    assert len(probe.starts) == 1
