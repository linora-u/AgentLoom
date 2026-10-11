"""Compatibility adapters retain legacy receipts inside the real Run owner."""
import json
import warnings
from copy import deepcopy
from pathlib import Path
from threading import Event

import pytest
import yaml
from agentloom.app import Application
from agentloom.app.definition import load_agent_definition
from agentloom.app.factory import YamlConfiguredAgent

from tests.application_test.test_worker_batch_application import batch_project as _batch_project

batch_project = _batch_project


def test_legacy_batch_preserves_captured_schema_and_caller_payload(batch_project):
    root, application, worker, probe, _write = batch_project
    captured = load_agent_definition(worker)
    captured['input_schema']['properties'].update({'_note': {}, 'task_id': {}})
    tasks = [{'value': {'nested': [1]}, '_note': 'caller data', 'task_id': 'business'}]
    original = deepcopy(tasks)
    with Application.open(application, project_root=root, file_logging=False) as app:
        tool = YamlConfiguredAgent(config=captured, logger=app.logger).agent_as_tool()
        results = tool.batch(tasks)
        assert results[0].result == original[0]
        assert results[0].task_id == 'business'
        assert tasks == original
        assert probe.starts[0][1].root_run_id == app.run.run_id


def test_repo_map_executes_its_business_plan_through_typed_worker(batch_project):
    from applications.repo_map.agent_tools.paths import repo_map_docs_root
    from applications.repo_map.agent_tools.pipeline_agent_tools import run_analysis_loop

    root, application, _worker, probe, write = batch_project
    production = Path(__file__).resolve().parents[2] / 'applications/repo_map/workflows/worker_agents/dir_architecture_analysis.yaml'
    config = yaml.safe_load(production.read_text())
    config.pop('concurrency', None)
    config.pop('system_prompt', None)
    config.update(agent_runtime='batch-fixture', model_type='test')
    write(application / 'workflows/worker_agents/dir_architecture_analysis.yaml', config)
    output = root / 'repo-map-output'
    (output / 'data').mkdir(parents=True)
    (output / 'data/analysis_progress.json').write_text(json.dumps({
        'src': {'rank': 1, 'status': 'pending'}, '(root)': {'rank': 2, 'status': 'pending'}}))
    docs = repo_map_docs_root(output)
    (docs / 'src').mkdir(parents=True)
    (docs / 'src/index.md').write_text('# src')
    (docs / 'index.md').write_text('# root')
    probe.behavior = lambda payload: '# analysis ' + payload['dir_path']
    with Application.open(application, project_root=root, file_logging=False), warnings.catch_warnings():
        warnings.simplefilter('error', DeprecationWarning)
        summary = run_analysis_loop(str(output))
    assert 'Analysis loop complete' in summary
    assert [payload['dir_path'] for payload, *_ in probe.starts] == ['src', '(root)']
    assert (docs / 'src/analysis.md').read_text() == '# analysis src'
    assert '# analysis src' in probe.starts[1][0]['children_analyses']


def test_goal_audit_uses_typed_map_and_keeps_external_report_schema(batch_project):
    from applications.goal_mode_validation.agent_tools.report_tools import run_goal_audit_batch

    root, application, _worker, probe, write = batch_project
    production = Path(__file__).resolve().parents[2] / 'applications/goal_mode_validation/workflows/worker_agents/contract_auditor.yaml'
    config = yaml.safe_load(production.read_text())
    config.pop('concurrency', None)
    config.update(agent_runtime='batch-fixture', model_type='test')
    write(application / 'workflows/worker_agents/contract_auditor.yaml', config)
    tasks = [{'task_id': 'first', 'query': 'Audit configuration and input contracts.'},
             {'task_id': 'second', 'query': 'Audit cleanup and structured receipt contracts.'}]
    probe.behavior = lambda payload: 'evidence for ' + payload['task_id']
    with Application.open(application, project_root=root, file_logging=False), warnings.catch_warnings():
        warnings.simplefilter('error', DeprecationWarning)
        result = json.loads(run_goal_audit_batch('contract', json.dumps(tasks), concurrency=2))
    assert [entry['task_id'] for entry in result] == ['first', 'second']
    assert [entry['result'] for entry in result] == ['evidence for first', 'evidence for second']
    assert {entry['status'] for entry in result} == {'completed'}
    assert all(set(entry) == {'task_id', 'status', 'duration_seconds', 'result', 'error'} for entry in result)


def test_parallel_launcher_keeps_order_and_duplicate_business_ids(batch_project):
    from agentloom.execution.concurrency import ParallelAgentExecutor

    root, application, _worker, _probe, _write = batch_project
    tasks = [{'task_id': 'same', 'value': 1}, {'task_id': 'same', 'value': 2}]
    original = deepcopy(tasks)
    with Application.open(application, project_root=root, file_logging=False):
        results = ParallelAgentExecutor(max_workers=2).execute_batch(tasks, lambda **payload: payload)
    assert [result.result for result in results] == original
    assert [result.task_id for result in results] == ['same', 'same']
    assert tasks == original


def test_parallel_factory_launcher_returns_unstarted_dispositions(batch_project):
    from agentloom.app.factory import YamlAgentFactory

    root, application, worker, probe, _write = batch_project
    captured = load_agent_definition(worker)
    captured['input_schema']['properties']['task_id'] = {}
    stop = Event()
    stop.set()
    with Application.open(application, project_root=root, file_logging=False):
        results = YamlAgentFactory.run_agents_parallel(captured,
            [{'task_id': 'one', 'value': 1}, {'task_id': 'two', 'value': 2}],
            max_workers=2, stop_token=stop)
    assert [(result.task_id, result.status) for result in results] == [('one', 'skipped'), ('two', 'skipped')]
    assert all(result.error for result in results)
    assert probe.starts == []


def test_direct_executor_retains_worker_cleanup_failures(batch_project):
    from agentloom.execution.concurrency import ParallelAgentExecutor, WorkerBatchError

    root, application, worker, probe, _write = batch_project
    def close_fails(_payload):
        raise RuntimeError('owned close failed')
    probe.close_behavior = close_fails
    failures = []
    with pytest.raises(BaseExceptionGroup):
        with Application.open(application, project_root=root, file_logging=False) as app:
            tool = YamlConfiguredAgent(config=load_agent_definition(worker), logger=app.logger).agent_as_tool()
            try:
                ParallelAgentExecutor(max_workers=1).execute_batch([{'value': 1}], tool)
            except WorkerBatchError as error:
                failures.append(error)
    assert len(failures) == 1
    assert failures[0].phase == 'cleanup'
    assert failures[0].cleanup_errors


def test_legacy_progress_observes_fast_completion_but_results_keep_input_order(batch_project):
    root, application, worker, probe, _write = batch_project
    release = Event()
    progress = []
    def behavior(payload):
        if payload['value'] == 1:
            assert release.wait(timeout=10)
        return payload['value']
    def on_progress(done, total, result):
        progress.append((done, total, result.result))
        if result.result == 2:
            release.set()
    probe.behavior = behavior
    try:
        with Application.open(application, project_root=root, file_logging=False) as app:
            tool = YamlConfiguredAgent(config=load_agent_definition(worker), logger=app.logger).agent_as_tool()
            results = tool.batch([{'value': 1}, {'value': 2}], concurrency=2, on_progress=on_progress)
        assert [result.result for result in results] == [1, 2]
        assert progress == [(1, 2, 2), (2, 2, 1)]
    finally:
        release.set()


def test_pathless_inline_batch_owns_one_compatibility_run(batch_project):
    from agentloom.app.factory import YamlAgentFactory

    _root, _application, worker, probe, _write = batch_project
    captured = load_agent_definition(worker)
    captured.pop('_yaml_file_path')
    results = YamlAgentFactory.run_agents_parallel(captured, [{'value': 1}, {'value': 2}], max_workers=2)
    assert [result.status for result in results] == ['completed', 'completed']
    assert len({context.root_run_id for _, context, *_ in probe.starts}) == 1
    assert len(probe.closed) == 2
    assert len({definition.instance_id for _, _, definition, _ in probe.starts}) == 2


def test_legacy_auto_rejects_before_worker_start(batch_project):
    root, application, worker, probe, _write = batch_project
    with Application.open(application, project_root=root, file_logging=False) as app:
        tool = YamlConfiguredAgent(config=load_agent_definition(worker), logger=app.logger).agent_as_tool()
        with pytest.raises(ValueError, match='positive integer'):
            tool.batch([{'value': 1}], concurrency='auto')
    assert probe.starts == []
