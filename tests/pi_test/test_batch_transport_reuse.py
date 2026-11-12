"""Real Pi processes must be reused without sharing task history or tool bindings."""
from __future__ import annotations

import yaml
from agentloom.app.application import Application
from agentloom.app.tool_factory import bind_tool_factory_bindings
from agentloom.config.config import bind_config, load_project_config
from agentloom.execution.concurrency import WorkItem
from agentloom.runtimes.pi.transport import PiTransport

from tests.pi_test.test_application import model_service, project
from tests.pi_test.test_tool_factory_application import configure


def record_processes(monkeypatch):
    processes = []
    original = PiTransport.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        processes.append(self.process)

    monkeypatch.setattr(PiTransport, '__init__', initialize)
    return processes


def test_map_reuses_process_with_fresh_history_and_dynamic_tool_schema(tmp_path, monkeypatch):
    from agentloom.runtimes.pi import install

    probes = []
    original_probe = install._node_abi

    def probe(*args, **kwargs):
        probes.append(True)
        return original_probe(*args, **kwargs)

    monkeypatch.setattr(install, "_node_abi", probe)
    processes = record_processes(monkeypatch)
    values = [11, 22, 33, 44, 55, 66]

    def answer(request):
        value = request['tools'][0]['function']['parameters']['properties']['value']['enum'][0]
        return [(f'call-{value}', 'save_value', {'value': value})]

    saved = []
    with model_service(turns=[answer] * len(values)) as (url, requests):
        path = project(tmp_path, url)
        configure(path)
        definition = yaml.safe_load(path.read_text())
        definition["runtime_options"]["max_steps"] = 1
        path.write_text(yaml.safe_dump(definition))
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            with bind_tool_factory_bindings({'host': saved}):
                result = app.worker('workflows/root.yaml').map(
                    [WorkItem(str(value), {'value': value}) for value in values], concurrency=2,
                )
            assert all(outcome.status == 'success' for outcome in result.outcomes), result.outcomes
            assert [o.output for o in result.outcomes] == [{'value': v} for v in values]
            assert all(p.poll() is not None for p in processes), 'map must retire its own native processes'
    assert len(processes) <= 2, 'a batch must reuse Pi processes across task waves'
    assert len(probes) == 1, 'installation identity is checked once per batch'
    assert sorted(saved) == values
    assert len(requests) == len(values)
    for _, payload, _ in requests:
        assert not any(message['role'] in {'assistant', 'tool'} for message in payload['messages'])
        schema = payload['tools'][0]['function']['parameters']
        assert schema['properties']['value']['enum'][0] in values
    # Reusing a physical process must preserve each task's logical invocation identity.
    ids = [o.local_run_id for o in result.outcomes]
    assert len(set(ids)) == len(values)
    import json
    events = [json.loads(line) for line in
              (app.run.run_dir / 'audit/runtime_events.jsonl').read_text().splitlines()]
    logical_ids = {o.local_run_id: o.invocation_id for o in result.outcomes}
    for event in events:
        details = event['details']
        if details.get('local_run_id') in logical_ids and 'instance_id' in details:
            assert details['instance_id'] == logical_ids[details['local_run_id']]
    assert not result.infrastructure_errors and not result.observer_errors
    assert all(not o.cleanup_errors for o in result.outcomes)


def test_map_preserves_stop_continuation_and_next_task_starts_fresh(tmp_path, monkeypatch):
    processes = record_processes(monkeypatch)
    with model_service(outputs=['first attempt', 'accepted', 'next task']) as (url, requests):
        path = project(tmp_path, url)
        definition = yaml.safe_load(path.read_text())
        definition['runtime_options']['max_stop_attempts'] = 2
        definition['hooks'] = {'Stop': [{'id': 'require-accepted', 'command':
            "python3 -c 'import json,sys; p=json.load(sys.stdin); "
            "v=p.get(\"tool_input\",{}).get(\"final_answer\"); "
            "print(json.dumps({\"decision\":\"block\" if v==\"first attempt\" else \"allow\", "
            "\"reason\":\"Respond accepted.\"}))'"}]}
        path.write_text(yaml.safe_dump(definition))
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            result = app.worker('workflows/root.yaml').map([WorkItem('one', {}), WorkItem('two', {})], concurrency=1)
    assert all(o.status == 'success' for o in result.outcomes), result.outcomes
    assert [o.output for o in result.outcomes] == ['accepted', 'next task']
    assert len(processes) == 1
    assert any(m['role'] == 'assistant' for m in requests[1][1]['messages'])
    assert not any(m['role'] == 'assistant' for m in requests[2][1]['messages'])


def test_failed_task_is_not_retried_and_healthy_later_tasks_reuse(tmp_path, monkeypatch):
    processes = record_processes(monkeypatch)
    with model_service(fail_count=1) as (url, requests):
        path = project(tmp_path, url)
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            result = app.worker('workflows/root.yaml').map(
                [WorkItem(str(i), {}) for i in range(3)], concurrency=1,
            )
    assert [o.status for o in result.outcomes] == ['failed', 'success', 'success']
    assert len(processes) == 2, 'failed transport retires; healthy tasks reuse a fresh process'
    assert len(requests) == 3, 'process reuse must not retry a failed model invocation'
    assert all(p.poll() is not None for p in processes)


def test_cancelled_map_does_not_start_queued_tasks_and_closes_pool(tmp_path, monkeypatch):
    from threading import Event

    processes = record_processes(monkeypatch)
    stop = Event()
    with model_service() as (url, requests):
        path = project(tmp_path, url)
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            result = app.worker('workflows/root.yaml').map(
                [WorkItem(str(i), {}) for i in range(6)], concurrency=1, stop_token=stop,
                event_sink=lambda event: stop.set(),
            )
            run_dir = app.run.run_dir
    assert [o.status for o in result.outcomes] == ['success'] + ['canceled'] * 5
    assert len(requests) == len(processes) == 1
    assert all(p.poll() is not None for p in processes)
    receipts = list((run_dir / 'native_resources').glob('*/exited.json'))
    import json
    assert receipts and all(json.loads(p.read_text())['physical_exit_confirmed'] for p in receipts)


def test_process_reuse_is_bounded_to_each_map_not_direct_invocations(tmp_path, monkeypatch):
    processes = record_processes(monkeypatch)
    with model_service() as (url, requests):
        path = project(tmp_path, url)
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            worker = app.worker('workflows/root.yaml')
            for _ in range(2):
                result = worker.map([WorkItem(str(i), {}) for i in range(3)], concurrency=1)
                assert all(o.status == 'success' for o in result.outcomes)
            assert worker.invoke(WorkItem('direct', {})).status == 'success'
    assert len(processes) == 3
    assert len(requests) == 7
    assert all(p.poll() is not None for p in processes)


def test_native_shell_workers_retain_individual_process_cleanup(tmp_path, monkeypatch):
    from tests.pi_test.test_tools_application import select

    processes = record_processes(monkeypatch)
    with model_service() as (url, requests):
        path = project(tmp_path, url)
        select(path, tools=[{'name': 'bash'}])
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            result = app.worker('workflows/root.yaml').map(
                [WorkItem(str(i), {}) for i in range(2)], concurrency=1,
            )
    assert all(o.status == 'success' for o in result.outcomes), result.outcomes
    assert len(requests) == len(processes) == 2
    assert all(p.poll() is not None for p in processes)


def test_failed_preflight_is_shared_without_retry_and_next_map_checks_again(tmp_path, monkeypatch):
    from agentloom.runtimes.pi import install

    original = install._node_abi
    probes = []

    def fail(*args, **kwargs):
        probes.append(True)
        raise RuntimeError('Pi Node.js ABI probe timed out after 5 seconds: fixture')

    with model_service() as (url, requests):
        path = project(tmp_path, url)
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            worker = app.worker('workflows/root.yaml')
            monkeypatch.setattr(install, '_node_abi', fail)
            result = worker.map([WorkItem(str(i), {}) for i in range(4)], concurrency=2)
            assert len(probes) == 1 and requests == []
            assert all(o.status == 'failed' and o.error.category == 'configuration' for o in result.outcomes)
            assert all('ABI probe timed out' in o.error.message for o in result.outcomes)
            monkeypatch.setattr(install, '_node_abi', original)
            result = worker.map([WorkItem('explicit-new-batch', {})], concurrency=1)
            assert result.outcomes[0].status == 'success'
    assert len(requests) == 1


def test_active_process_cannot_change_invocation_identity(tmp_path, monkeypatch):
    import pytest
    from agentloom.execution.agent_runtime import AgentRuntimeError

    transports = []
    original = PiTransport.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        transports.append(self)

    monkeypatch.setattr(PiTransport, '__init__', initialize)
    rejected = []

    def on_request(number, request):
        transport = transports[0]
        identity = transport.instance_id
        with pytest.raises(AgentRuntimeError, match='not idle'):
            transport.begin_invocation('another-active-task')
        assert transport.instance_id == identity
        rejected.append(number)

    with model_service(on_request=on_request) as (url, requests):
        path = project(tmp_path, url)
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            result = app.worker('workflows/root.yaml').map(
                [WorkItem('first', {}), WorkItem('second', {})], concurrency=1,
            )
    assert all(o.status == 'success' for o in result.outcomes)
    assert rejected == [1, 2] and len(transports) == 1


def test_closed_runtime_cannot_execute_in_a_later_tasks_borrowed_process(tmp_path, monkeypatch):
    import pytest
    from agentloom.execution.agent_runtime import AgentRuntimeError, AgentRuntimeRequest
    from agentloom.runtimes.pi.runtime import PiRuntime

    runtimes = []
    original = PiRuntime.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        runtimes.append(self)

    monkeypatch.setattr(PiRuntime, '__init__', initialize)

    def on_request(number, request):
        if number == 2:
            assert runtimes[0].transport.process.pid == runtimes[1].transport.process.pid
            with pytest.raises(AgentRuntimeError, match='invocation is closed'):
                runtimes[0].run(AgentRuntimeRequest(task='stale invocation must not run'))

    with model_service(on_request=on_request) as (url, requests):
        path = project(tmp_path, url)
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            result = app.worker('workflows/root.yaml').map(
                [WorkItem('first', {}), WorkItem('second', {})], concurrency=1,
            )
    assert all(o.status == 'success' for o in result.outcomes)
    assert len(requests) == 2


def test_nested_application_in_observer_cannot_borrow_another_runs_process(tmp_path, monkeypatch):
    processes = record_processes(monkeypatch)
    observed = []
    with model_service() as (url, requests):
        path = project(tmp_path, url)
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            def observe(event):
                if observed:
                    return
                observed.append(event)
                with Application.open(path.parents[1], project_root=tmp_path) as nested:
                    result = nested.worker('workflows/root.yaml').invoke(WorkItem('nested', {}))
                    assert result.status == 'success'
                assert len(processes) == 2 and processes[-1].poll() is not None
                assert processes[0].poll() is None

            result = app.worker('workflows/root.yaml').map(
                [WorkItem('first', {}), WorkItem('second', {})], concurrency=1, event_sink=observe,
            )
    assert all(o.status == 'success' for o in result.outcomes), result.outcomes
    assert not result.observer_errors
    assert len(requests) == 3 and len(processes) == 2
    assert all(p.poll() is not None for p in processes)


def test_first_call_of_a_new_logical_runtime_never_continues_a_previous_task(tmp_path, monkeypatch):
    from dataclasses import replace

    from agentloom.runtimes.pi.runtime import PiRuntime

    original = PiRuntime.run

    def run(self, request):
        return original(self, replace(request, continue_session=True))

    monkeypatch.setattr(PiRuntime, 'run', run)
    processes = record_processes(monkeypatch)
    with model_service() as (url, requests):
        path = project(tmp_path, url)
        with bind_config(load_project_config(tmp_path)), Application.open(path.parents[1], project_root=tmp_path) as app:
            result = app.worker('workflows/root.yaml').map(
                [WorkItem('first', {}), WorkItem('second', {})], concurrency=1,
            )
    assert all(o.status == 'success' for o in result.outcomes), result.outcomes
    assert len(requests) == 2 and len(processes) == 1
    assert all(not any(m['role'] == 'assistant' for m in payload['messages']) for _, payload, _ in requests)
