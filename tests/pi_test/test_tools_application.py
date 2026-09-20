"""Application acceptance through the real Pi SDK, with controlled model turns."""
import json
import sys
from threading import Barrier

import yaml
import pytest

from agentloom.application.runner import execute_app
from agentloom.configuration.config import bind_config, load_project_config
from tests.pi_test.test_application import model_service, project


_batch_gate = None


def parallel_probe(label: str) -> str:
    """Return invocation identity after both independent calls have started.

    Args:
        label: Label identifying this member of the batch.
    """
    from agentloom.runtime import get_current_run_context
    from agentloom.runtime.trace import capture_explicit_execution_context
    context = get_current_run_context(required=True)
    execution = capture_explicit_execution_context()
    assert execution.hook_run is not None
    _batch_gate.wait(timeout=4)
    return json.dumps({'label': label, 'run_id': context.run_id, 'task_id': execution.task_id,
                       'instance_id': execution.agent_id, 'hook_run_id': execution.hook_run.local_run_id})


def wait_for_cleanup(marker: str) -> str:
    """Wait on a run-owned resource until Application cancellation closes it.

    Args:
        marker: Path where this application reports that the wait has started.
    """
    from pathlib import Path
    from threading import Event
    from agentloom.runtime.resources import register_resource
    released = Event()

    def close():
        Path(marker + '.closed').write_text('closed')
        released.set()

    register_resource('pi-acceptance-wait', close)
    Path(marker).write_text('waiting')
    if not released.wait(timeout=30):
        raise RuntimeError('Run-owned resource was not closed')
    return 'closed'


def select(app, **values):
    config = yaml.safe_load(app.read_text())
    config.update(values)
    app.write_text(yaml.safe_dump(config))


def test_selected_official_read_commits_before_model_continues(tmp_path):
    (tmp_path / "note.txt").write_text("Native read evidence: saffron-19\n")
    with model_service(turns=[[('read-1', 'read', {'path': 'note.txt'})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{"name": "read"}])
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.output == "Pi answer"
    assert len(requests) == 2
    assert [tool['function']['name'] for tool in requests[0][1]['tools']] == ['read']
    tool_messages = [message for message in requests[1][1]['messages'] if message['role'] == 'tool']
    assert len(tool_messages) == 1
    assert 'saffron-19' in tool_messages[0]['content']
    entries = [json.loads(path.read_text()) for path in (result.run.run_dir / 'native-tools').rglob('*.json')]
    receipts = [entry for entry in entries if entry.get('state') == 'committed']
    assert len(receipts) == 1
    assert receipts[0]['request']['tool']['provider'] == 'pi'
    assert receipts[0]['request']['identity']['call_id'] == 'read-1'


def test_platform_outline_repairs_raw_input_and_returns_to_same_pi_application(tmp_path):
    source = tmp_path / 'source.py'
    source.write_text('def saffron_entrypoint(value):\n    return value + 19\n')
    hook = tmp_path / 'repair.py'
    hook.write_text('import json, sys, time\np = json.load(sys.stdin)\ntime.sleep(0.02)\n'
                    'assert p["tool_input"]["file_path"] == 19\n'
                    f'print(json.dumps({{"decision":"modify", "modified_input":{{"file_path":{str(source)!r}}}}}))\n')
    with model_service(turns=[[('outline-1', 'get_file_outline', {'file_path': 19})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'get_file_outline'}], hooks={'PreToolUse': [
            {'id': 'repair', 'matcher': 'get_file_outline', 'command': f'{sys.executable} {hook}'}]})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.output == 'Pi answer'
    assert len(requests) == 2
    assert [tool['function']['name'] for tool in requests[0][1]['tools']] == ['get_file_outline']
    messages = requests[1][1]['messages']
    assert 'saffron_entrypoint' in next(message['content'] for message in messages if message['role'] == 'tool')
    call = next(message['tool_calls'][0] for message in messages if message.get('tool_calls'))
    assert json.loads(call['function']['arguments'])['file_path'] == str(source)
    events = [json.loads(line) for line in (result.run.run_dir / 'audit/runtime_events.jsonl').read_text().splitlines()]
    records = [event['details']['record'] for event in events if event['kind'] == 'tool']
    assert len(records) == 1
    assert records[0]['call_id'] == 'outline-1'
    assert records[0]['input'] == {'file_path': str(source)}
    assert records[0]['status'] == 'completed'
    assert 'saffron_entrypoint' in records[0]['output']


def test_parallel_platform_batch_keeps_run_and_hook_context_without_deadlock(tmp_path):
    global _batch_gate
    _batch_gate = Barrier(2)
    with model_service(turns=[[('left-1', 'parallel_probe', {'label': 'left'}),
                               ('right-1', 'parallel_probe', {'label': 'right'})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, concurrency=2, tools=[{'name': 'parallel_probe', 'module': __name__, 'function': 'parallel_probe'}])
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    outputs = {m['tool_call_id']: json.loads(m['content']) for m in requests[1][1]['messages'] if m['role'] == 'tool'}
    assert set(outputs) == {'left-1', 'right-1'}
    assert outputs['left-1']['label'] == 'left'
    assert outputs['right-1']['label'] == 'right'
    assert {output['run_id'] for output in outputs.values()} == {result.run.run_id}
    assert {output['task_id'] for output in outputs.values()} == {result.run.task_id}
    assert len({output['instance_id'] for output in outputs.values()}) == 1
    assert len({output['hook_run_id'] for output in outputs.values()}) == 1


@pytest.mark.parametrize('repair', [True, False])
def test_native_hook_repairs_before_sdk_validation_or_records_rejection(tmp_path, repair):
    (tmp_path / 'note.txt').write_text('Only a validated read sees saffron-23')
    hook = tmp_path / 'repair_native.py'
    final_path = 'note.txt' if repair else False
    hook.write_text('import json, sys, time\np=json.load(sys.stdin)\ntime.sleep(0.02)\n'
                    'assert p["tool_input"]["path"] == 19\n'
                    f'print(json.dumps({{"decision":"modify", "modified_input":{{"path":{final_path!r}}}}}))\n')
    with model_service(turns=[[('native-repair', 'read', {'path': 19})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'read'}], hooks={'PreToolUse': [
            {'id': 'repair-native', 'matcher': 'read', 'command': f'{sys.executable} {hook}'}]})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    entries = [json.loads(path.read_text()) for path in (result.run.run_dir / 'native-tools').rglob('*.json')]
    assert len(entries) == 1
    entry = entries[0]
    assert entry['request']['raw_arguments'] == {'path': 19}
    if repair:
        assert entry['state'] == 'committed'
        assert entry['record']['input'] == {'path': 'note.txt'}
        assert 'saffron-23' in json.dumps(requests[1][1])
    else:
        assert entry['rejection']['status'] == 'blocked'
        assert 'authorization_id' not in entry
        assert 'saffron-23' not in json.dumps(requests[1][1])


def test_pi_final_waits_for_root_goal_completion_and_emits_one_terminal(tmp_path):
    with model_service(turns=[None, [('goal-read', 'get_goal', {})],
                               [('goal-finish', 'update_goal', {'status': 'complete', 'evidence': 'Verified saffron Application result.'})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, goal={'enabled': True}, runtime_options={'max_stop_attempts': 3})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.output == 'Pi answer'
    assert result.goal['status'] == 'complete'
    assert len(requests) == 4
    assert any('Verified saffron' in str(m) for m in requests[3][1]['messages'] if m['role'] == 'tool')
    events = [json.loads(line) for line in (result.run.run_dir / 'audit/runtime_events.jsonl').read_text().splitlines()]
    assert [event['details']['state'] for event in events if event['kind'] == 'terminal'] == ['success']


def test_pi_supervisor_runs_two_independent_pi_workers_with_callbacks(tmp_path):
    global _batch_gate
    _batch_gate = Barrier(2)
    worker_turns = [[('shared-call', 'parallel_probe', {'label': 'worker-left'})],
                    [('shared-call', 'parallel_probe', {'label': 'worker-right'})]]
    with model_service(turns=worker_turns) as (worker_url, worker_requests), model_service(
        turns=[[('worker-left', 'probe', {'query': 'left'}), ('worker-right', 'probe', {'query': 'right'})]]
    ) as (url, requests):
        app = project(tmp_path, url)
        model_path = tmp_path / 'config/llm.yaml'
        config = yaml.safe_load(model_path.read_text())
        config['model']['worker'] = {**config['model']['test'], 'base_url': worker_url}
        model_path.write_text(yaml.safe_dump(config))
        worker = app.parent / 'worker_agents/probe.yaml'
        worker.parent.mkdir()
        worker.write_text(yaml.safe_dump({'name': 'probe', 'agent_runtime': 'pi', 'model_type': 'worker',
            'description': 'Read the requested fact.', 'workflow': 'Call parallel_probe then return the fact.',
            'tools': [{'name': 'parallel_probe', 'module': __name__, 'function': 'parallel_probe'}], 'toolsets': [],
            'agent_function_schema': {'description': 'Read a fact.', 'inputs': {'query': {'description': 'Requested fact.'}},
                                      'output': {'description': 'The requested fact.'}}}))
        select(app, concurrency=2, worker_agents=[{'path': 'probe.yaml'}])
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.output == 'Pi answer'
    assert len(requests) == 2
    assert len(worker_requests) == 4
    observations = [json.loads(message['content']) for _, payload, _ in worker_requests
                    for message in payload['messages'] if message['role'] == 'tool']
    assert {observation['label'] for observation in observations} == {'worker-left', 'worker-right'}
    assert len({observation['instance_id'] for observation in observations}) == 2
    assert len({observation['hook_run_id'] for observation in observations}) == 2
    assert {observation['run_id'] for observation in observations} == {result.run.run_id}
    events = [json.loads(line) for line in (result.run.run_dir / 'audit/runtime_events.jsonl').read_text().splitlines()]
    worker_records = [event['details'] for event in events if event['kind'] == 'tool'
                      and event['details']['record']['tool_name'] == 'parallel_probe']
    assert len(worker_records) == 2
    assert len({record['instance_id'] for record in worker_records}) == 2


def test_completed_goal_cannot_hide_stop_rejection(tmp_path):
    from agentloom.application.run import ApplicationRunError
    hook = tmp_path / 'stop.py'
    hook.write_text('import json\nprint(json.dumps({"decision":"block", "reason":"Delivery rejected by Stop gate"}))\n')
    with model_service(turns=[[('complete-before-stop', 'update_goal', {'status': 'complete', 'evidence': 'Checked the work.'})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, goal=True, runtime_options={'max_stop_attempts': 2}, hooks={'Stop': [
            {'id': 'reject-final', 'command': f'{sys.executable} {hook}'}]})
        with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError, match='Stop gate remained blocked'):
            execute_app(app, file_logging=False)


@pytest.mark.parametrize('fault', ['keyboard', 'bridge_exit'])
def test_application_cancels_and_reaps_a_pending_platform_callback(tmp_path, fault):
    import os
    from pathlib import Path
    import signal
    from tests.pi_test.test_process_lifecycle import assert_gone, node_launcher, start_cli, until
    marker = tmp_path / 'callback-started'
    with model_service(turns=[[('pending-platform', 'wait_for_cleanup', {'marker': str(marker)})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'wait_for_cleanup', 'module': 'tests.pi_test.test_tools_application',
                           'function': 'wait_for_cleanup'}])
        env, pid_file = node_launcher(tmp_path)
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
        child = start_cli(tmp_path, app, env)
        pid = None
        try:
            until(marker.exists)
            pid = int(pid_file.read_text())
            os.kill(child.pid if fault == 'keyboard' else pid,
                    signal.SIGINT if fault == 'keyboard' else signal.SIGKILL)
            stdout, stderr = child.communicate(timeout=8)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    assert child.returncode != 0
    assert Path(str(marker) + '.closed').read_text() == 'closed'
    records = [json.loads(line) for line in stdout.splitlines()]
    assert records[-1]['event'] == ('run.interrupted' if fault == 'keyboard' else 'run.failed')
    assert len(requests) == 1
    assert_gone(pid)
