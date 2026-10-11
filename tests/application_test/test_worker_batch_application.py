"""Finite batch behavior through the real Application/Agent invocation seam."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from threading import Condition, Event, Lock, Thread

import pytest
import yaml

from agentloom.execution.agent_runtime import AgentRuntimeResult, RuntimeCapabilities, RuntimeUsage
from agentloom.execution.concurrency import WorkItem


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

    write(tmp_path / 'config/system.yaml', {
        'runtime': {'root_dir': str(tmp_path / 'runtime')},
        'checkpoint': {'enabled': False}, 'self_learning': {'enabled': False},
        'logging': {'console_enabled': False}, 'default_toolsets': [],
    })
    write(tmp_path / 'config/model.yaml', {'model': {'default_model_type': 'test',
        'test': {'model': 'fixture/model', 'adapter': 'openai_chat', 'api_key': 'fixture-secret'}}})
    application = tmp_path / 'applications/batch'
    worker = application / 'workflows/worker.yaml'
    write(worker, {'name': 'worker', 'description': 'Controlled Worker.', 'task': 'Process supplied input.',
        'agent_runtime': 'batch-fixture', 'tools': [], 'toolsets': [],
        'input_schema': {'type': 'object', 'properties': {'value': {}, 'action': {}},
                         'required': ['value'], 'additionalProperties': False}})
    probe = Probe()

    class Runtime:
        runtime_id = 'batch-fixture'
        capabilities = RuntimeCapabilities(False, False, True, True)

        def __init__(self, definition):
            self.definition = definition
            self.payload = None
            if probe.construct_error is not None:
                raise probe.construct_error

        def run(self, request):
            if 'Input data (JSON):\n' not in request.task:
                return AgentRuntimeResult(state='success', output=probe.root_behavior())
            self.payload = json.loads(request.task.split('Input data (JSON):\n', 1)[1])
            execution = capture_explicit_execution_context()
            with probe.condition:
                probe.starts.append((self.payload, execution, self.definition, request))
                probe.active += 1
                probe.peak = max(probe.peak, probe.active)
                probe.condition.notify_all()
            return AgentRuntimeResult(state='success', output=probe.behavior(self.payload),
                usage=RuntimeUsage(input_tokens=3, output_tokens=2, total_tokens=5))

        def snapshot(self):
            return None

        def close(self):
            try:
                probe.close_behavior(self.payload)
                self.definition.tool_gateway.close()
            finally:
                if self.payload is not None:
                    with probe.condition:
                        probe.closed.append(self.payload)
                        probe.active -= 1
                        probe.condition.notify_all()

    registry = build_builtin_runtime_registry()
    registry.register('batch-fixture', capabilities=Runtime.capabilities, factory=Runtime)
    monkeypatch.setattr('agentloom.app.validation.build_builtin_runtime_registry', lambda: registry)
    monkeypatch.setattr('agentloom.app.agent.build_builtin_runtime_registry', lambda: registry)
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
        result = app.worker('workflows/worker.yaml').map([WorkItem('a', {'value': 1}), WorkItem('b', {'value': 2})])
        record = app.runtime_context.read_batch_record(result.batch_id)
        assert [item.output for item in result.outcomes] == [{'value': 1}, {'value': 2}]
        assert result.status == 'completed'
        assert result.execution.concurrency == 1
        assert all(item.usage['total_tokens'] == 5 for item in result.outcomes)
        assert {item.root_run_id for item in result.outcomes} == {app.run.run_id}
        assert len({item.invocation_id for item in result.outcomes}) == 2
        assert record['input_fingerprint'] == result.input_fingerprint
        manifest_path = app.run.manifest_path
    assert len(probe.closed) == 2
    assert json.loads(manifest_path.read_text())['status'] == 'completed'


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
        assert not thread.is_alive(), 'batch failed to drain'


@pytest.mark.parametrize('capacity', [1, 2, 20, 32, 65])
def test_capacity_is_attainable_and_never_clipped_or_exceeded(batch_project, capacity):
    from agentloom.app import Application
    root, application, _worker, probe, _write = batch_project
    gate = Event()
    def hold(payload):
        assert gate.wait(timeout=10)
        return payload['value']
    probe.behavior = hold
    with Application.open(application, project_root=root, file_logging=False) as app:
        with background(lambda: app.worker('workflows/worker.yaml').map(
            [WorkItem(str(i), {'value': i}) for i in range(capacity + 2)], concurrency=capacity)) as (results, errors, _thread):
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
    workflow = application / 'workflows/root.yaml'
    write(workflow, {'name': 'batch-root', 'description': 'Orchestrate.', 'task': 'Execute business plan.',
        'agent_runtime': 'batch-fixture', 'tools': [], 'toolsets': []})
    receipts = []
    def business():
        app = current_run_app()
        result = app.worker('workflows/worker.yaml').map([WorkItem('child', {'value': 7})])
        receipts.append(result)
        return result.outcomes[0].output
    probe.root_behavior = business
    result = execute_app(workflow, file_logging=False)
    assert result.output == {'value': 7}
    assert receipts[0].run_id == result.run.run_id
    assert receipts[0].outcomes[0].root_run_id == result.run.run_id
    assert json.loads(result.run.manifest_path.read_text())['worker_batches'][0]['batch_id'] == receipts[0].batch_id


def test_exceptional_scope_exit_drains_before_closing_shared_context_engine(batch_project):
    from agentloom.app import Application
    from agentloom.execution.context_engine.runtime import get_active_context_engine
    from contextvars import copy_context
    from time import monotonic
    root, application, _worker, probe, _write = batch_project
    gate, leaving = Event(), Event()
    shared = {'outcomes': []}
    def use_root_store(payload):
        assert gate.wait(timeout=10)
        engine = get_active_context_engine()
        engine.capture_tool_result('long result\n' * 400, tool_name='fixture', source='worker')
        return 'stored-after-exit-request'
    probe.behavior = use_root_store
    class UserFailure(Exception):
        pass
    def scope():
        try:
            with Application.open(application, project_root=root, file_logging=False) as app:
                shared['binding'] = app.worker('workflows/worker.yaml')
                context = copy_context()
                thread = Thread(target=lambda: shared['outcomes'].append(context.run(shared['binding'].map, [WorkItem('a', {'value': 1})])))
                thread.start()
                shared['thread'] = thread
                probe.wait_starts(1)
                leaving.set()
                raise UserFailure('business failed')
        finally:
            if 'thread' in shared:
                shared['thread'].join(timeout=10)
    with background(scope) as (_results, errors, thread):
        assert leaving.wait(timeout=10)
        deadline = monotonic() + 10
        while monotonic() < deadline:
            try:
                shared['binding'].map([])
            except RuntimeError as exc:
                assert 'closed' in str(exc)
                break
        else:
            pytest.fail('owner never stopped registration')
        assert thread.is_alive()
        gate.set()
    assert len(errors) == 1 and isinstance(errors[0], UserFailure)
    assert shared['outcomes'][0].outcomes[0].status == 'success'
    assert shared['outcomes'][0].outcomes[0].output == 'stored-after-exit-request'
