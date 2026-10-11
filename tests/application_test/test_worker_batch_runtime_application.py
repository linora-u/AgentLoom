"""The finite batch contract through genuine Pi/smol Application invocations."""
import json
from contextvars import copy_context
from threading import Thread

import pytest
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config

from tests.application_test.mixed_runtime_support import model_service


@pytest.mark.parametrize('project,application,worker,override,expected,source', [
    (None, None, None, None, 1, 'framework_default'),
    (2, None, None, None, 2, 'project_system'),
    (2, 3, None, None, 3, 'application_system'),
    (2, 3, 4, None, 4, 'application_worker'),
    (2, 3, 4, 5, 5, 'call_override'),
])
def test_application_receipt_reports_each_winning_configuration_source(
    tmp_path, project, application, worker, override, expected, source,
):
    import yaml
    from agentloom.app import Application

    from tests.acceptance.worker_batch_support import prepare
    from tests.application_test.mixed_runtime_support import write_yaml

    prepare(tmp_path, 'smolagents', url='http://127.0.0.1:9/v1')
    system = tmp_path / 'config/system.yaml'
    settings = yaml.safe_load(system.read_text())
    if project is not None:
        settings['worker_execution'] = {'defaults': {'concurrency': project}}
    write_yaml(system, settings)
    policy = {}
    if application is not None:
        policy['defaults'] = {'concurrency': application}
    if worker is not None:
        policy['workers'] = {'workflows/worker.yaml': {'concurrency': worker}}
    application_root = tmp_path / 'applications/batch_acceptance'
    write_yaml(application_root / 'config/system.yaml', {'worker_execution': policy})
    with bind_config(load_project_config(tmp_path)):
        with Application.open(application_root, project_root=tmp_path, file_logging=False) as app:
            receipt = app.worker('workflows/worker.yaml').map([], concurrency=override)
            assert (receipt.execution.concurrency, receipt.execution.source) == (expected, source)
            assert receipt.status == 'completed' and receipt.outcomes == ()
            record = app.runtime_context.read_batch_record(receipt.batch_id)
            assert record['execution']['source'] == source
            assert record['execution']['concurrency'] == expected
    assert not (tmp_path / 'mcp-events.jsonl').exists(), 'empty batch constructed invocation resources'


@pytest.mark.parametrize('runtime', ['pi', 'smolagents'])
def test_fixed_business_plan_survives_capacity_changes_with_real_runtime_ownership(tmp_path, runtime):
    from tests.acceptance.worker_batch_support import (
        FixtureState,
        capture_bridges,
        configure,
        model_program,
        prepare,
        scan_secrets,
        verify_run,
    )
    with model_service(model_program(runtime)) as (url, requests):
        workflow = prepare(tmp_path, runtime, url=url)
        state = FixtureState(tmp_path)
        with capture_bridges(tmp_path), configure(state), bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=False)
        proof = verify_run(result, state)
    assert proof['reduction'] == ['BATCH-ALPHA-8149', 'BATCH-BETA-3952', 'BATCH-GAMMA-6471', 'BATCH-DELTA-9027']
    assert [b['execution']['concurrency'] for b in proof['batches']] == [1, 20]
    assert proof['resource_exit']['open_ports'] == []
    assert requests and {request['model'] for request in requests} == {'supervisor', 'worker'}
    assert 'fixture-key' not in json.dumps(proof)
    assert scan_secrets(tmp_path, ['fixture-key'])['credential_matches'] == 0


@pytest.mark.parametrize('runtime', ['pi', 'smolagents'])
@pytest.mark.parametrize('mode', ['collect', 'fail_fast'])
def test_real_provider_failure_keeps_dispositions_and_never_replays_side_effect(tmp_path, runtime, mode):
    from tests.acceptance.worker_batch_support import (
        FixtureState,
        capture_bridges,
        configure,
        model_program,
        prepare,
        verify_run,
    )
    with model_service(model_program(runtime, fail_item='item-1')) as (url, _requests):
        workflow = prepare(tmp_path, runtime, url=url)
        state = FixtureState(tmp_path, mode=mode, capacities=(1,))
        with capture_bridges(tmp_path), configure(state), bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=False)
        verify_run(result, state, expected_success=False)
    receipt = state.receipts[0]
    assert receipt['status'] == 'failed'
    assert [o['status'] for o in receipt['outcomes']] == (
        ['success', 'failed', 'success', 'success'] if mode == 'collect'
        else ['success', 'failed', 'canceled', 'canceled'])
    assert receipt['outcomes'][1]['error']['error_type'] == 'AgentRuntimeError'
    assert receipt['outcomes'][1]['error']['message']
    assert sum(r['item_id'] == 'item-1' for r in state.resources) == 1
    assert all(o['invocation_id'] is None for o in receipt['outcomes'] if o['status'] == 'canceled')


@pytest.mark.parametrize('runtime', ['pi', 'smolagents'])
def test_real_synchronous_tool_stop_drains_actual_started_work_and_resources(tmp_path, runtime):
    from time import monotonic, sleep

    from tests.acceptance.worker_batch_support import (
        FixtureState,
        capture_bridges,
        configure,
        model_program,
        port_open,
        prepare,
        verify_run,
    )

    results, errors = [], []
    with model_service(model_program(runtime)) as (url, _requests):
        workflow = prepare(tmp_path, runtime, url=url)
        state = FixtureState(tmp_path, mode='stop', capacities=(2,))
        with capture_bridges(tmp_path), configure(state), bind_config(load_project_config(tmp_path)):
            context = copy_context()
            def run():
                try:
                    results.append(context.run(execute_app, workflow, file_logging=False))
                except BaseException as error:
                    errors.append(error)
            thread = Thread(target=run)
            thread.start()
            try:
                deadline = monotonic() + 35
                while len(state.resources) < 2 and monotonic() < deadline:
                    sleep(.02)
                assert len(state.resources) == 2
                assert all(port_open(r['port']) for r in state.resources)
                state.stop.set()
                assert thread.is_alive() and not results
            finally:
                state.release.set()
                thread.join(45)
                assert not thread.is_alive(), 'actual synchronous Tool failed to drain'
        assert not errors
        verify_run(results[0], state, expected_success=False)
    receipt = state.receipts[0]
    assert receipt['status'] == 'stopped'
    assert [o['status'] for o in receipt['outcomes']] == ['success', 'success', 'canceled', 'canceled']


def test_executor_rejection_is_structured_at_the_public_application_seam(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkerBatchError, WorkItem

    from tests.acceptance.worker_batch_support import prepare

    prepare(tmp_path, 'smolagents', url='http://127.0.0.1:9/v1')
    failure = RuntimeError('external executor rejected admission')
    def reject(*_args, **_kwargs):
        raise failure
    with bind_config(load_project_config(tmp_path)):
        with Application.open(tmp_path / 'applications/batch_acceptance', project_root=tmp_path, file_logging=False) as app:
            worker = app.worker('workflows/worker.yaml')
            monkeypatch.setattr(ThreadPoolExecutor, 'submit', reject)
            with pytest.raises(WorkerBatchError) as caught:
                worker.map([WorkItem('a', {'item_id': 'a', 'fact': 'known', 'file_path': 'a.txt'})])
            assert caught.value.phase == 'executor'
            assert caught.value.original_error is failure
            assert caught.value.outcomes[0].status == 'canceled'
            assert caught.value.outcomes[0].invocation_id is None


@pytest.mark.parametrize('runtime', ['pi', 'smolagents'])
def test_real_cli_terminal_receipt_agrees_with_map_and_physical_exit(tmp_path, runtime):
    import subprocess
    import sys
    from pathlib import Path

    from tests.acceptance.worker_batch_support import model_program, resource_exit

    child_root = tmp_path / 'child'
    with model_service(model_program(runtime)) as (url, _requests):
        campaign = Path(__file__).parents[1] / 'acceptance/worker_batch_validation.py'
        result = subprocess.run([sys.executable, str(campaign), '--child', '--runtimes', runtime,
            '--profile', 'worker', '--items', '2', '--concurrency', '2', '--fixture-url', url,
            '--output', str(child_root)], capture_output=True, text=True, timeout=90)
    (tmp_path / 'cli.stdout').write_text(result.stdout)
    (tmp_path / 'cli.stderr').write_text(result.stderr)
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r['event'] for r in records] == ['run.started', 'run.completed']
    report = json.loads((child_root / 'report.json').read_text())
    evidence = json.loads((child_root / 'fixture-evidence.json').read_text())
    resource_exit(child_root, evidence['resources'])
    assert report['proof']['reduction'] == ['BATCH-ALPHA-8149', 'BATCH-BETA-3952']
    assert 'fixture-key' not in result.stdout + result.stderr


@pytest.mark.parametrize('runtime', ['pi', 'smolagents'])
def test_cli_campaign_retains_failed_batch_even_when_root_completes(tmp_path, runtime):
    import subprocess
    import sys
    from pathlib import Path

    from tests.acceptance.worker_batch_support import model_program, resource_exit

    child_root = tmp_path / 'child'
    with model_service(model_program(runtime, fail_item='item-1')) as (url, _requests):
        campaign = Path(__file__).parents[1] / 'acceptance/worker_batch_validation.py'
        result = subprocess.run([sys.executable, str(campaign), '--child', '--runtimes', runtime,
            '--profile', 'worker', '--items', '2', '--concurrency', '2', '--fixture-url', url,
            '--output', str(child_root)], capture_output=True, text=True, timeout=90)
    (tmp_path / 'cli.stdout').write_text(result.stdout)
    (tmp_path / 'cli.stderr').write_text(result.stderr)
    assert result.returncode != 0
    failure = json.loads((child_root / 'failure-report.json').read_text())
    assert failure['failure_phase'] == 'batch_verification'
    assert failure['terminal']['event'] == 'run.completed'
    assert failure['batches'][0]['status'] == 'failed'
    assert [o['status'] for o in failure['batches'][0]['outcomes']] == ['success', 'failed']
    assert failure['resource_exit']['live_pids'] == []
    assert failure['resource_exit']['remaining_owned_threads'] == []
    assert failure['secret_scan']['credential_matches'] == 0
    evidence = json.loads((child_root / 'fixture-evidence.json').read_text())
    resource_exit(child_root, evidence['resources'])
    assert not (child_root / 'report.json').exists(), 'failed batch was reported as campaign success'
