"""Recovery via Application/CLI, official Pi sessions and independent disk evidence."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sys

import pytest
import yaml

from agentloom.application.run import ApplicationRunError
from agentloom.application.runner import execute_app
from agentloom.configuration.config import bind_config, load_project_config
from tests.pi_test.test_application import model_service, project
from tests.pi_test.test_tools_application import select
from tests.pi_test.test_process_lifecycle import (
    assert_gone,
    node_launcher,
    sdk_node,
    start_cli,
    until,
)


def checkpoints(root):
    return [(p, json.loads(p.read_text())) for p in root.rglob('checkpoint.json')
            if 'runtime_checkpoint' in json.loads(p.read_text())]


def enable(app, **kwargs):
    system = app.parents[3] / 'config/system.yaml'
    config = yaml.safe_load(system.read_text())
    config['checkpoint'] = {'enabled': True, 'cleanup_on_success': False}
    system.write_text(yaml.safe_dump(config))
    select(app, **kwargs)


def audit(run):
    return [json.loads(line) for line in (run.run_dir / 'audit/runtime_events.jsonl').read_text().splitlines()]


def _process_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_resume_new_run_preserves_committed_read_and_native_session(tmp_path):
    (tmp_path / 'proof.txt').write_text('original-proof-392')
    with model_service(turns=[[('read-proof', 'read', {'path': 'proof.txt'})]], fail_requests={2: 500}) as (url, requests):
        app = project(tmp_path, url)
        enable(app, tools=[{'name': 'read'}])
        with bind_config(load_project_config(tmp_path)):
            with pytest.raises(ApplicationRunError) as error:
                execute_app(app, file_logging=False)
            first = error.value.run
            before = list(tmp_path.rglob('native-tools/*.json'))
            assert len(before) == 1
            result = execute_app(app, resume_task_id=first.task_id, file_logging=False)
        assert result.output == 'Pi answer'
        assert result.run.task_id == first.task_id and result.run.run_id != first.run_id
        assert list(tmp_path.rglob('native-tools/*.json')) == before
        assert len(requests) == 3
        resumed = requests[-1][1]['messages']
        assert any(m['role'] == 'tool' and 'original-proof-392' in str(m['content']) for m in resumed)
        assert [e['details']['state'] for e in audit(result.run) if e['kind'] == 'terminal'] == ['success']
        assert any(e['kind'] == 'run' and e['details'].get('resumed') for e in audit(result.run))


def fault_launcher(root, boundary):
    """Kill the genuine SDK process at the external JSONL boundary, once."""
    binary = sdk_node()
    marker = root / 'fault.json'
    launcher = root / 'bin/node'
    launcher.parent.mkdir()
    launcher.write_text(f'''#!{sys.executable}
import subprocess,sys,threading,json,os
from pathlib import Path
if sys.argv[1:]==['--version'] or sys.argv[1:3]==['-p','process.versions.modules']:os.execv({binary!r},[{binary!r},*sys.argv[1:]])
p=subprocess.Popen([{binary!r},*sys.argv[1:]],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
def stop(value):
 Path({str(marker)!r}).write_text(json.dumps({{'pid':p.pid,'frame':value}}));p.kill()
def forward():
 try:
  for line in sys.stdin.buffer:
   value=json.loads(line)
   if value.get('kind')=='response' and (({boundary!r}=='after_commit' and (value.get('payload') or {{}}).get('method')=='tool_settle') or ({boundary!r}=='platform_commit' and (value.get('payload') or {{}}).get('method')=='platform_invoke') and (value.get('payload') or {{}}).get('record', {{}}).get('tool_name')=='inspect_note'):
    stop(value);return
   p.stdin.write(line);p.stdin.flush()
 except BrokenPipeError:pass
threading.Thread(target=forward,daemon=True).start()
for line in p.stdout:
 value=json.loads(line);method=(value.get('payload') or {{}}).get('method')
 if value.get('kind')=='request' and (({boundary!r}=='before_dispatch' and method=='tool_dispatch') or ({boundary!r}=='before_settle' and method=='tool_settle')):
  stop(value);break
 sys.stdout.buffer.write(line);sys.stdout.buffer.flush()
p.wait()
os._exit(p.returncode if p.returncode>=0 else 1)
''')
    launcher.chmod(0o755)
    return {**os.environ, 'PATH': str(launcher.parent) + os.pathsep + os.environ['PATH']}, marker


@pytest.mark.parametrize('boundary', ['after_commit', 'before_settle', 'before_dispatch'])
def test_real_process_crash_reconciles_without_repeating_effect(tmp_path, boundary):
    with model_service(turns=[[('effect', 'bash', {'command': 'printf x >> counter.txt'})]]) as (url, requests):
        app = project(tmp_path, url)
        enable(app, tools=[{'name': 'bash'}], shell_settings={
            'allowed_commands': ['printf'], 'allowed_operators': ['*'], 'sandbox': {'enabled': False}})
        env, marker = fault_launcher(tmp_path, boundary)
        child = start_cli(tmp_path, app, env)
        try:
            stdout, stderr = child.communicate(timeout=30)
        finally:
            if child.poll() is None:
                child.kill(); child.wait()
        assert child.returncode != 0, stdout + stderr
        assert_gone(json.loads(marker.read_text())['pid'])
        task = next(tmp_path.rglob('task_tree.json'))
        task_id = json.loads(task.read_text())['task_id']
        original = list(tmp_path.rglob('native-tools/*.json'))
        assert len(original) == 1
        receipt = json.loads(original[0].read_text())
        assert receipt['state'] == {'after_commit': 'committed', 'before_settle': 'uncertain', 'before_dispatch': 'cancelled'}[boundary]
        with bind_config(load_project_config(tmp_path)):
            if boundary == 'before_settle':
                with pytest.raises(ApplicationRunError, match='uncertain') as error:
                    execute_app(app, resume_task_id=task_id, file_logging=False)
                assert [e['details']['state'] for e in audit(error.value.run) if e['kind'] == 'terminal'] == ['failed']
            else:
                assert execute_app(app, resume_task_id=task_id, file_logging=False).output == 'Pi answer'
        assert list(tmp_path.rglob('native-tools/*.json')) == original
        if boundary == 'before_dispatch':
            assert not (tmp_path / 'counter.txt').exists()
            assert 'did not execute' in json.dumps(requests[-1][1])
        else:
            assert (tmp_path / 'counter.txt').read_text() == 'x'
        assert len(requests) == (1 if boundary == 'before_settle' else 2)


def test_new_run_can_reuse_provider_call_id_at_new_native_position(tmp_path):
    (tmp_path / 'proof.txt').write_text('same-id-new-run')
    call = [('reused-id', 'read', {'path': 'proof.txt'})]
    with model_service(turns=[call, None, call], fail_requests={2: 500}) as (url, requests):
        app = project(tmp_path, url)
        enable(app, tools=[{'name': 'read'}])
        with bind_config(load_project_config(tmp_path)):
            with pytest.raises(ApplicationRunError) as error:
                execute_app(app, file_logging=False)
            result = execute_app(app, resume_task_id=error.value.run.task_id, file_logging=False)
        assert result.output == 'Pi answer'
        receipts = [json.loads(p.read_text()) for p in tmp_path.rglob('native-tools/*.json')]
        assert len(receipts) == 2
        identities = [r['request']['identity'] for r in receipts]
        assert {i['call_id'] for i in identities} == {'reused-id'}
        assert len({i['native_session_id'] for i in identities}) == 1
        assert len({(i['run_id'], i['instance_id'], i['native_parent_id']) for i in identities}) == 2
        assert len(requests) == 4


def test_same_run_can_reuse_provider_call_id_at_new_native_position(tmp_path):
    (tmp_path / 'proof.txt').write_text('same-id-same-run')
    call = [('reused-id', 'read', {'path': 'proof.txt'})]
    with model_service(turns=[call, call]) as (url, requests):
        app = project(tmp_path, url)
        enable(app, tools=[{'name': 'read'}])
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert result.output == 'Pi answer'
    receipts = [json.loads(p.read_text()) for p in tmp_path.rglob('native-tools/*.json')]
    assert len(receipts) == 2
    identities = [receipt['request']['identity'] for receipt in receipts]
    assert {identity['call_id'] for identity in identities} == {'reused-id'}
    assert len({identity['run_id'] for identity in identities}) == 1
    assert len({identity['instance_id'] for identity in identities}) == 1
    assert len({identity['native_session_id'] for identity in identities}) == 1
    assert len({identity['native_parent_id'] for identity in identities}) == 2
    assert len(requests) == 3


@pytest.mark.parametrize('damage', ['sdk', 'runtime', 'bridge_version', 'state_version', 'task', 'digest', 'symlink', 'parent', 'arguments', 'result_details', 'missing_journal'])
def test_incompatible_or_unaligned_recovery_stops_before_model_or_tools(tmp_path, damage):
    (tmp_path / 'proof.txt').write_text('unaltered-proof')
    with model_service(turns=[[('read-proof', 'read', {'path': 'proof.txt'})]], fail_requests={2: 500}) as (url, requests):
        app = project(tmp_path, url)
        enable(app, tools=[{'name': 'read'}])
        with bind_config(load_project_config(tmp_path)):
            with pytest.raises(ApplicationRunError) as first:
                execute_app(app, file_logging=False)
            [(path, data)] = checkpoints(tmp_path)
            envelope = data['runtime_checkpoint']
            artifact = path.parent / 'pi/sessions' / (envelope['payload']['artifact'] + '.json')
            if damage == 'sdk': envelope['runtime_version'] = '0.0.0'
            elif damage == 'runtime': envelope['runtime_id'] = 'smolagents'
            elif damage == 'bridge_version': envelope['payload']['bridge_version'] = 999
            elif damage == 'state_version': envelope['state_schema_version'] = 999
            elif damage == 'task': envelope['task_id'] = 'another-task'
            elif damage == 'digest': artifact.write_text('{}')
            elif damage == 'symlink':
                target = artifact.with_suffix('.original')
                artifact.rename(target)
                artifact.symlink_to(target)
            elif damage == 'missing_journal':
                next(tmp_path.rglob('native-tools/*.json')).unlink()
            else:
                bundle = json.loads(artifact.read_text())
                if damage == 'parent': bundle['calls'][0]['identity']['native_parent_id'] = 'wrong-parent'
                elif damage == 'arguments': bundle['calls'][0]['arguments']['path'] = 'wrong.txt'
                else:
                    next(e['message'] for e in bundle['session']['entries']
                         if e.get('message', {}).get('role') == 'toolResult')['details'] = {'forged': True}
                raw = json.dumps(bundle).encode()
                digest = hashlib.sha256(raw).hexdigest()
                artifact.with_name(digest + '.json').write_bytes(raw)
                envelope['payload']['artifact'] = digest
            path.write_text(json.dumps(data))
            with pytest.raises(ApplicationRunError) as failure:
                execute_app(app, resume_task_id=first.value.run.task_id, file_logging=False)
            assert len(requests) == 2
            assert (tmp_path / 'proof.txt').read_text() == 'unaltered-proof'
            assert [e['details']['state'] for e in audit(failure.value.run) if e['kind'] == 'terminal'] == ['failed']


@pytest.mark.parametrize('stale', [False, True])
def test_restored_read_authorizes_write_only_while_file_version_matches(tmp_path, stale):
    target = tmp_path / 'proof.txt'
    target.write_text('original-proof')
    with model_service(turns=[ [('read-proof', 'read', {'path': 'proof.txt'})], None,
                               [('write-proof', 'write', {'path': 'proof.txt', 'content': 'updated-proof'})] ],
                       fail_requests={2: 500}) as (url, requests):
        app = project(tmp_path, url)
        enable(app, tools=[{'name': 'read'}, {'name': 'write'}])
        with bind_config(load_project_config(tmp_path)):
            with pytest.raises(ApplicationRunError) as first:
                execute_app(app, file_logging=False)
            if stale: target.write_text('external-change')
            result = execute_app(app, resume_task_id=first.value.run.task_id, file_logging=False)
        assert result.output == 'Pi answer'
        assert target.read_text() == ('external-change' if stale else 'updated-proof')
        records = [e['details']['record'] for e in audit(result.run) if e['kind'] == 'tool']
        assert records[0]['status'] == ('blocked' if stale else 'completed')
        assert len(requests) == 4


@pytest.mark.parametrize('worker', ['pi', 'smolagents'])
def test_committed_worker_result_is_appended_without_invoking_worker_again(tmp_path, worker):
    from tests.application_test.mixed_runtime_support import project as mixed_project, model_service as mixed_service, tool_messages, finish
    (tmp_path / 'note.txt').write_text('worker-proof-627')

    def program(request):
        messages = tool_messages(request)
        if request['model'] == 'worker':
            if not messages:
                return [('read-note', 'read' if worker == 'pi' else 'read_file',
                         {'path': 'note.txt'} if worker == 'pi' else {'file_path': str(tmp_path / 'note.txt')})]
            assert 'worker-proof-627' in str(messages)
            return finish(request, 'worker-proof-627')
        if not messages:
            return [('delegate', 'inspect_note', {'query': 'note.txt'})]
        assert 'worker-proof-627' in str(messages)
        return 'Verified worker-proof-627'

    with mixed_service(program) as (url, requests):
        app = mixed_project(tmp_path, url, supervisor='pi', worker=worker)
        enable(app)
        env, marker = fault_launcher(tmp_path, 'platform_commit')
        child = start_cli(tmp_path, app, env)
        try:
            stdout, stderr = child.communicate(timeout=40)
        finally:
            if child.poll() is None: child.kill(); child.wait()
        assert child.returncode != 0, stdout + stderr
        assert_gone(json.loads(marker.read_text())['pid'])
        task_id = json.loads(next(tmp_path.rglob('task_tree.json')).read_text())['task_id']
        worker_calls = [r for r in requests if r['model'] == 'worker']
        assert len(worker_calls) == 2
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, resume_task_id=task_id, file_logging=False)
        assert result.output == 'Verified worker-proof-627'
        assert [r for r in requests if r['model'] == 'worker'] == worker_calls
        assert len([r for r in requests if r['model'] == 'supervisor']) == 2


@pytest.mark.parametrize('worker', ['pi', 'smolagents'])
@pytest.mark.parametrize('worker_result', ['worker-window-proof-739', ''])
def test_worker_completion_before_platform_receipt_recovers_without_reexecution(
    tmp_path, worker, worker_result,
):
    from tests.application_test.mixed_runtime_support import (
        finish,
        model_service as mixed_service,
        project as mixed_project,
        tool_messages,
    )
    (tmp_path / 'note.txt').write_text('worker-window-proof-739')

    def program(request):
        messages = tool_messages(request)
        if request['model'] == 'worker':
            if not messages:
                return [('read-note', 'read' if worker == 'pi' else 'read_file',
                         {'path': 'note.txt'} if worker == 'pi'
                         else {'file_path': str(tmp_path / 'note.txt')})]
            return finish(request, worker_result)
        if not messages:
            return [('delegate', 'inspect_note', {'query': 'note.txt'})]
        if worker_result:
            assert worker_result in str(messages)
        else:
            assert any(message.get('content') == '' for message in messages)
        return 'Verified worker-window-proof-739'

    with mixed_service(program) as (url, requests):
        app = mixed_project(tmp_path, url, supervisor='pi', worker=worker)
        enable(app)
        env, bridge_marker = node_launcher(tmp_path)
        marker = tmp_path / 'fault.json'
        faultsite = tmp_path / 'faultsite'
        faultsite.mkdir()
        (faultsite / 'sitecustomize.py').write_text(
            'import json,os\n'
            'from pathlib import Path\n'
            'from agentloom.runtimes.pi.checkpoint import PiCheckpointStore\n'
            '_commit=PiCheckpointStore.commit_platform\n'
            'def crash(self,identity,record):\n'
            " if record.tool_name=='inspect_note':\n"
            f"  Path({str(marker)!r}).write_text(json.dumps({{'pid':os.getpid(),'tool':record.tool_name}}));os._exit(91)\n"
            ' return _commit(self,identity,record)\n'
            'PiCheckpointStore.commit_platform=crash\n'
        )
        env['PYTHONPATH'] = str(faultsite)
        child = start_cli(tmp_path, app, env)
        try:
            stdout, stderr = child.communicate(timeout=40)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        assert child.returncode != 0, stdout + stderr
        assert json.loads(marker.read_text())['tool'] == 'inspect_note'
        bridge_pid = int(bridge_marker.read_text())
        until(lambda: _process_gone(bridge_pid))
        assert_gone(bridge_pid)
        task_id = json.loads(next(tmp_path.rglob('task_tree.json')).read_text())['task_id']
        worker_requests = [request for request in requests if request['model'] == 'worker']
        assert len(worker_requests) == 2
        [(receipt_path, receipt)] = [
            (path, json.loads(path.read_text()))
            for path in tmp_path.rglob('pi/platform/*.json')
        ]
        assert receipt['state'] == 'executing'

        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, resume_task_id=task_id, file_logging=False)

        assert result.output == 'Verified worker-window-proof-739'
        assert [request for request in requests if request['model'] == 'worker'] == worker_requests
        assert json.loads(receipt_path.read_text())['state'] == 'committed'
        assert len([request for request in requests if request['model'] == 'supervisor']) == 2


def test_checkpoint_storage_failure_prevents_first_tool_side_effect(tmp_path):
    def damage_storage(number, request):
        if number == 1:
            sessions = next(tmp_path.rglob('pi/sessions'))
            sessions.rename(sessions.with_name('sessions-original'))
            sessions.write_text('not a directory')

    with model_service(turns=[[('effect', 'bash', {'command': 'printf x >> counter.txt'})]], on_request=damage_storage) as (url, requests):
        app = project(tmp_path, url)
        enable(app, tools=[{'name': 'bash'}], shell_settings={
            'allowed_commands': ['printf'], 'allowed_operators': ['*'], 'sandbox': {'enabled': False}})
        with bind_config(load_project_config(tmp_path)), pytest.raises(ApplicationRunError) as failure:
            execute_app(app, file_logging=False)
        assert len(requests) == 1
        assert not (tmp_path / 'counter.txt').exists()
        assert [e['details']['state'] for e in audit(failure.value.run) if e['kind'] == 'terminal'] == ['failed']
        assert all(json.loads(p.read_text()).get('state') in {'authorized', 'cancelled'}
                   for p in tmp_path.rglob('native-tools/*.json'))
