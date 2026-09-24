"""Pi selection and governance acceptance at the public Application boundary."""
import json
import sys

import pytest
import yaml

from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from tests.pi_test.test_application import model_service, project
from tests.pi_test.test_tools_application import select


def audit(result):
    return [json.loads(line) for line in (result.run.run_dir / 'audit/runtime_events.jsonl').read_text().splitlines()]


def file_probe(file_path: str) -> str:
    """Read one fixture file through a dynamically selected Python tool."""
    from pathlib import Path
    return Path(file_path).read_text()


def test_one_application_uses_official_read_and_selected_python_extension(tmp_path):
    from agentloom.execution.observability import inspect_run

    source = tmp_path / 'facts.py'
    source.write_text('def verified_invoice_total():\n    return 6941\n')
    with model_service(turns=[[('native', 'read', {'path': str(source)}),
                               ('python', 'file_probe', {'file_path': str(source)})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[
            {'name': 'read'},
            {'name': 'file_probe', 'module': __name__, 'function': 'file_probe'},
        ])
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    records = [event['details'] for event in audit(result) if event['kind'] == 'tool']
    assert {(item['record']['tool_name'], item['owner'], item['provider']) for item in records} == {
        ('read', 'runtime', 'pi'), ('file_probe', 'external', 'python')}
    assert all(item['record']['status'] == 'completed' for item in records)
    assert {tool['function']['name'] for tool in requests[0][1]['tools']} == {'read', 'file_probe'}
    assert all('verified_invoice_total' in str(item['record']['output']) for item in records)
    with inspect_run(result.run) as trace:
        calls = {item['tool_name']: item for item in trace.events() if item['kind'] == 'tool'}
        assert set(calls) == {'read', 'file_probe'}
        assert 'verified_invoice_total' in trace.read_text(calls['read']['output_ref'])
        assert 'verified_invoice_total' in trace.read_text(calls['file_probe']['output_ref'])


@pytest.mark.parametrize('scenario', ['excluded', 'missing'])
def test_native_read_preserves_policy_block_and_execution_error(tmp_path, scenario):
    from agentloom.execution.observability import inspect_run

    excluded = tmp_path / 'private.txt'
    excluded.write_text('DENIED-NATIVE-CONTENT-6941')
    path = excluded if scenario == 'excluded' else tmp_path / 'missing.txt'
    with model_service(turns=[[('read-denied', 'read', {'path': str(path)})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'read'}], tool_access_control={'path_validation': [
            {'tools': ['read'], 'exclude_paths': [str(excluded)]}]})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    records = [event['details']['record'] for event in audit(result) if event['kind'] == 'tool']
    assert len(records) == 1
    assert records[0]['status'] == ('blocked' if scenario == 'excluded' else 'error')
    with inspect_run(result.run) as trace:
        calls = [event for event in trace.events() if event['kind'] == 'tool']
        assert len(calls) == 1
        assert calls[0]['status'] == records[0]['status']
    assert 'DENIED-NATIVE-CONTENT-6941' not in json.dumps(requests)
    entries = [json.loads(path.read_text()) for path in (result.run.run_dir / 'native-tools').rglob('*.json')]
    assert len(entries) == 1
    if scenario == 'excluded':
        assert 'authorization_id' not in entries[0]
    else:
        assert entries[0]['state'] == 'committed'
        assert entries[0]['record']['status'] == 'error'


def test_native_trace_write_failure_fails_application(tmp_path, monkeypatch):
    from agentloom.app.run import ApplicationRunError
    from agentloom.execution.observability import TraceRecorder

    source = tmp_path / 'fact.txt'
    source.write_text('COMMITTED-NATIVE-8426')

    def fail_write(_recorder, _event):
        raise OSError('fixture disk full')

    monkeypatch.setattr(TraceRecorder, '_append', fail_write)
    with model_service(turns=[[('native', 'read', {'path': str(source)})]]) as (url, _requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'read'}])
        with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError, match='Could not persist Tool trace'):
            execute_app(app, file_logging=False)


@pytest.mark.parametrize('name', ['read_file', 'grep_search', 'glob_search', 'shell_tool', 'todo_write',
                                 'grep', 'find'])
def test_unmapped_basics_and_specialist_writes_are_rejected_before_application_execution(tmp_path, name):
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': name}])
        with bind_config(load_project_config(tmp_path)), pytest.raises(ValueError):
            execute_app(app, file_logging=False)
    assert not requests


def test_skill_activation_uses_one_platform_catalog_and_no_pi_discovery(tmp_path):
    with model_service(turns=[[('activate-review', 'skill', {'name': 'review'})]]) as (url, requests):
        app = project(tmp_path, url)
        for directory, token in [(tmp_path / 'skills/review', 'PROJECT-CONTENT-NOT-SELECTED'),
                                 (app.parents[1] / 'skills/review', 'APP-SKILL-SELECTED-6941'),
                                 (tmp_path / '.pi/skills/review', 'PI-CONTENT-NOT-SELECTED')]:
            directory.mkdir(parents=True)
            (directory / 'SKILL.md').write_text(f'---\nname: review\ndescription: Inspect the selected review token.\n---\n{token}\n')
        select(app, tools=[{'name': 'skill'}])
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    first = json.dumps(requests[0][1])
    assert 'APP-SKILL-SELECTED-6941' not in first
    assert 'APP-SKILL-SELECTED-6941' in json.dumps(requests[1][1])
    assert 'PI-CONTENT-NOT-SELECTED' not in json.dumps(requests)
    assert 'PROJECT-CONTENT-NOT-SELECTED' not in json.dumps(requests)
    records = [event['details']['record'] for event in audit(result) if event['kind'] == 'tool']
    assert [record['tool_name'] for record in records] == ['skill']


def test_worker_cannot_complete_root_goal_even_with_explicit_goal_function(tmp_path):
    root_turns = [[('worker', 'probe', {'query': 'verify permission'})], [('root-observe', 'get_goal', {})],
                  [('root-complete', 'update_goal', {'status': 'complete', 'evidence': 'Root verified denied Worker completion.'})]]
    with model_service(turns=[[('worker-complete', 'update_goal', {'status': 'complete', 'evidence': 'Worker must not settle root.'})]]) as (worker_url, worker_requests), model_service(turns=root_turns) as (url, requests):
        app = project(tmp_path, url)
        llm = tmp_path / 'config/llm.yaml'
        config = yaml.safe_load(llm.read_text())
        config['model']['worker'] = {**config['model']['test'], 'base_url': worker_url}
        llm.write_text(yaml.safe_dump(config))
        worker = app.parent / 'worker_agents/probe.yaml'
        worker.parent.mkdir()
        worker.write_text(yaml.safe_dump({'name': 'probe', 'agent_runtime': 'pi', 'model_type': 'worker',
            'description': 'Verify permissions.', 'workflow': 'Verify the Goal boundary.', 'toolsets': [],
            'tools': [{'name': 'update_goal', 'module': 'agentloom.tools.goal', 'function': 'update_goal'}],
            'input_schema': {'type': 'object', 'properties': {
                'query': {'type': 'string', 'description': 'Request.'}},
                'required': ['query'], 'additionalProperties': False}}))
        select(app, goal=True, worker_agents=[{'path': 'probe.yaml'}])
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.goal['status'] == 'complete'
    records = [event['details']['record'] for event in audit(result) if event['kind'] == 'tool']
    by_id = {record['call_id']: record for record in records}
    assert by_id['worker-complete']['status'] == 'error'
    assert 'root Supervisor' in by_id['worker-complete']['error']['message']
    assert json.loads(by_id['root-observe']['output'])['status'] == 'active'
    assert json.loads(by_id['root-complete']['output'])['status'] == 'complete'
    terminals = [event for event in audit(result) if event['kind'] == 'terminal' and event['details']['root_agent']]
    assert len(terminals) == 1


def test_cancellation_releases_real_mcp_callback_and_server(tmp_path):
    import os
    from pathlib import Path
    import signal
    import psutil
    from tests.pi_test.test_process_lifecycle import node_launcher, start_cli, until, assert_gone
    mcp_events = tmp_path / 'mcp-events.jsonl'
    with model_service(turns=[[('pending-mcp', 'mcp__facts__slow_lookup', {'query': 'cancel this request'})]]) as (url, requests):
        app = project(tmp_path, url)
        mcp = tmp_path / 'config/mcp.json'
        mcp.write_text(json.dumps({'mcpServers': {'facts': {'command': sys.executable,
            'args': [str(Path(__file__).parents[1] / 'mcp_test/fixtures/stdio_server.py'), str(mcp_events)]}}}))
        select(app, mcp_servers=str(mcp))
        env, pid_file = node_launcher(tmp_path)
        child = start_cli(tmp_path, app, env)
        pid = None
        try:
            until(lambda: mcp_events.exists() and 'slow_started' in mcp_events.read_text())
            pid = int(pid_file.read_text())
            os.kill(child.pid, signal.SIGINT)
            stdout, stderr = child.communicate(timeout=7)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if mcp_events.exists():
                for item in mcp_events.read_text().splitlines():
                    candidate = json.loads(item)['pid']
                    if psutil.pid_exists(candidate) and child.returncode == -signal.SIGKILL:
                        os.kill(candidate, signal.SIGKILL)
    assert child.returncode != 0
    assert json.loads(stdout.splitlines()[-1])['event'] == 'run.interrupted'
    assert len(requests) == 1
    assert_gone(pid)
    assert all(not psutil.pid_exists(json.loads(item)['pid']) for item in mcp_events.read_text().splitlines())


def test_context_refs_remain_retrievable_with_pi_checkpoint_disabled(tmp_path):
    from pathlib import Path
    import re

    def retrieve(request):
        result = next(message['content'] for message in request['messages'] if message['role'] == 'tool')
        match = re.search(r'\[ContextRef (ctx_[a-zA-Z0-9]+)', result)
        assert match is not None, 'Platform output was not stored as a retrievable ContextRef'
        assert 'PLATFORM-CTX-8426' not in result
        return [('retrieve-original', 'loom_retrieve_context', {'ref': match.group(1), 'query': 'TARGET_RECORD', 'offset': 0, 'limit': 5})]

    with model_service(turns=[[('large-output', 'mcp__facts__context_payload', {'query': 'full artifact'})], retrieve]) as (url, requests):
        app = project(tmp_path, url)
        mcp = tmp_path / 'config/mcp.json'
        mcp.write_text(json.dumps({'mcpServers': {'facts': {'command': sys.executable,
            'args': [str(Path(__file__).parents[1] / 'mcp_test/fixtures/stdio_server.py')]}}}))
        select(app, mcp_servers=str(mcp), tools=[{'name': 'loom_retrieve_context'}],
               context_engine={'min_chars': 1000, 'preview_max_chars': 300})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    records = {event['details']['record']['call_id']: event['details']['record'] for event in audit(result) if event['kind'] == 'tool'}
    assert '[ContextRef ' not in records['large-output']['output']
    assert 'PLATFORM-CTX-8426' in records['large-output']['output']
    assert 'PLATFORM-CTX-8426' in records['retrieve-original']['output']
    assert records['retrieve-original']['status'] == 'completed'


def test_invalid_final_platform_arguments_are_rejected_without_execution(tmp_path):
    source = tmp_path / 'facts.py'
    source.write_text('def forbidden_probe_5729():\n    return 1\n')
    hook = tmp_path / 'invalid_hook.py'
    hook.write_text('import json\nprint(json.dumps({"decision":"modify", "modified_input":{"file_path":False}}))\n')
    with model_service(turns=[[('invalid-final', 'file_probe', {'file_path': str(source)})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'file_probe', 'module': __name__, 'function': 'file_probe'}],
               hooks={'PreToolUse': [
                   {'id': 'invalid-final', 'matcher': 'file_probe',
                    'command': f'{sys.executable} {hook}'}]})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    records = [event['details']['record'] for event in audit(result) if event['kind'] == 'tool']
    assert len(records) == 1
    assert records[0]['status'] == 'blocked'
    assert records[0]['input'] == {'file_path': False}
    assert 'forbidden_probe_5729' not in json.dumps(requests)


def test_pi_native_fixed_arguments_are_explicitly_unsupported(tmp_path):
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'read', 'fixed_args': {'path': 'note.txt'}}])
        with bind_config(load_project_config(tmp_path)), pytest.raises(ValueError, match='fixed_args'):
            execute_app(app, file_logging=False)
    assert not requests
