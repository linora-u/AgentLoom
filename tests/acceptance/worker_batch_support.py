"""Finite business fixture and physical verifiers shared by real Runtime acceptance."""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import psutil

from tests.application_test.mixed_runtime_support import finish, tool_messages, write_yaml

TOKENS = ('BATCH-ALPHA-8149', 'BATCH-BETA-3952', 'BATCH-GAMMA-6471', 'BATCH-DELTA-9027')
_STATE = None


class FixtureState:
    def __init__(self, root: Path, *, mode='success', count=4, capacities=(1, 20)):
        self.root = root
        self.mode = mode
        self.count = count
        self.capacities = capacities
        self.lock = threading.RLock()
        self.receipts = []
        self.resources = []
        self.progress = []
        self.first_gate = None
        self.first_ready = threading.Event()
        self.ownership = []
        self.stop = threading.Event()
        self.started = threading.Event()
        self.release = threading.Event()
        self.baseline_threads = {t.ident for t in threading.enumerate()}


@contextmanager
def configure(state):
    global _STATE
    prior, _STATE = _STATE, state
    try:
        yield state
    finally:
        _STATE = prior


def inspect_batch_item(item_id: str, fact: str) -> str:
    """Record an actual fixture read and own a real listener for this invocation.

    Args:
        item_id: Stable business input identity.
        fact: Exact business fact read from the requested fixture.
    """
    from agentloom.execution import get_current_run_context
    from agentloom.execution.resources import register_resource
    from agentloom.execution.trace import capture_explicit_execution_context

    state = _STATE
    assert state is not None
    context = get_current_run_context(required=True)
    execution = capture_explicit_execution_context()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(32)
    record = {'item_id': item_id, 'fact': fact, 'port': listener.getsockname()[1],
              'run_id': context.run_id, 'task_id': execution.task_id,
              'instance_id': execution.agent_id, 'root_run_id': execution.root_run_id,
              'local_run_id': execution.local_run_id,
              'hook_run_id': execution.hook_run.local_run_id}
    register_resource('batch-physical-listener', listener.close, instance_id=execution.agent_id)
    with state.lock:
        state.resources.append(record)
        with (state.root / 'physical-resources.jsonl').open('a') as stream:
            stream.write(json.dumps(record) + '\n')
        state.started.set()
    if state.mode == 'stop':
        assert state.release.wait(45), 'test fixture never released started Tool'
    elif state.first_gate is not None:
        if item_id == 'item-0':
            state.first_ready.set()
            assert state.first_gate.wait(45), 'later completions did not free capacity'
        else:
            assert state.first_ready.wait(45), 'first fixture did not acquire its resource'
    return json.dumps(record)


def run_fixed_plan() -> str:
    """Execute the fixed business plan through the current Application's typed Worker."""
    from agentloom.app import current_run_app
    from agentloom.execution.concurrency import WorkItem, WorkPlan

    state = _STATE
    assert state is not None
    app = current_run_app()
    worker = app.worker('workflows/worker.yaml')
    plan = WorkPlan(tuple(WorkItem(f'item-{i}', {'item_id': f'item-{i}', 'fact': token,
        'file_path': str(state.root / f'item-{i}.txt')}) for i, token in enumerate(TOKENS[:state.count])))
    for capacity in state.capacities:
        state.first_gate = threading.Event() if state.mode == 'success' and capacity > 1 and state.count > 2 else None
        state.first_ready = threading.Event()

        def progress(event):
            state.progress.append(event.to_dict())
            if state.first_gate is not None and event.outcome.item_id == f'item-{state.count - 1}':
                try:
                    first = next(r for r in reversed(state.resources) if r['item_id'] == 'item-0')
                    last = next(r for r in reversed(state.resources) if r['item_id'] == event.outcome.item_id)
                    calls = [json.loads(line) for line in (state.root / 'mcp-events.jsonl').read_text().splitlines()]
                    first_mcp = next(c['pid'] for c in reversed(calls) if c.get('query') == 'item-0')
                    assert port_open(first['port']) and psutil.pid_exists(first_mcp)
                    assert not port_open(last['port'])
                    state.ownership.append({'active_item': 'item-0', 'active_mcp_pid': first_mcp,
                        'active_port': first['port'], 'completed_port': last['port'], 'observed': True})
                finally:
                    state.first_gate.set()

        receipt = worker.map(plan, concurrency=capacity, event_sink=progress,
            stop_token=state.stop, failure_mode='fail_fast' if state.mode == 'fail_fast' else 'collect')
        state.receipts.append(receipt.to_dict())
    write_evidence(state)
    return json.dumps({'run_id': app.run.run_id,
        'batch_ids': [r['batch_id'] for r in state.receipts],
        'reduction': reduction(state.receipts[-1])})


def reduction(receipt):
    return [json.loads(o['output'])['fact'] for o in receipt['outcomes'] if o['status'] == 'success']


def write_evidence(state):
    (state.root / 'fixture-evidence.json').write_text(json.dumps({
        'batches': state.receipts, 'resources': state.resources, 'progress': state.progress,
        'ownership': state.ownership}, indent=2))


def prepare(root: Path, runtime: str, *, url=None, profile='worker', model_settings=None, count=4):
    root.mkdir(parents=True, exist_ok=True)
    application = root / 'applications/batch_acceptance'
    write_yaml(root / 'config/system.yaml', {
        'runtime': {'root_dir': str(root / 'runtime')}, 'self_learning': {'enabled': False},
        'default_toolsets': [], 'logging': {'console_enabled': False},
        'checkpoint': {'enabled': True, 'cleanup_on_success': False}, 'redaction': {'enabled': True},
    })
    if model_settings is None:
        base = {'adapter': 'openai_chat', 'base_url': url, 'api_key': 'fixture-key',
                'context_window': 32768, 'max_output_tokens': 2000, 'num_retries': 0,
                'timeout': 30, 'requests_per_minute': 2000000, 'supports_structured_output': True}
        profiles = {role: {**base, 'model': f'openai/{role}'} for role in ('supervisor', 'worker')}
    else:
        profiles = {profile: model_settings}
    write_yaml(root / 'config/model.yaml', {'model': {'default_model_type': profile, **profiles}})
    (root / 'config/model.yaml').chmod(0o600)
    worker = {'name': 'batch_worker', 'agent_runtime': runtime,
        'model_type': 'worker' if model_settings is None else profile,
        'description': 'Read exactly one fixed business input and return its evidence.',
        'task': ('Read file_path using the read Tool, call mcp__facts__lookup(query=item_id), '
                 'then inspect_batch_item(item_id=item_id, fact=<exact file content>). '
                 'Return the exact JSON string supplied by inspect_batch_item, with no prose or markdown.'),
        'tools': [{'name': 'read' if runtime == 'pi' else 'read_file'},
            {'name': 'inspect_batch_item', 'module': 'tests.acceptance.worker_batch_support', 'function': 'inspect_batch_item'}],
        'toolsets': [], 'input_schema': {'type': 'object', 'properties': {
            k: {'type': 'string'} for k in ('item_id', 'fact', 'file_path')},
            'required': ['item_id', 'fact', 'file_path'], 'additionalProperties': False},
        'mcp_servers': {'path': 'config/mcp.json'}}
    if runtime == 'smolagents':
        worker['runtime_options'] = {'max_steps': 8, 'smart_summary': False, 'todo_mode': 'off'}
    mcp_path = application / 'config/mcp.json'
    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_path.write_text(json.dumps({'mcpServers': {'facts': {'command': sys.executable,
        'args': [str(Path(__file__).parents[1] / 'mcp_test/fixtures/stdio_server.py'), str(root / 'mcp-events.jsonl')]}}}))
    write_yaml(application / 'workflows/worker.yaml', worker)
    workflow = application / 'workflows/root.yaml'
    supervisor = {'name': 'batch_acceptance', 'agent_runtime': runtime,
        'model_type': 'supervisor' if model_settings is None else profile,
        'description': 'Execute one finite business plan.',
        'task': 'Call run_fixed_plan exactly once. Return its exact JSON string with no prose or markdown.',
        'tools': [{'name': 'run_fixed_plan', 'module': 'tests.acceptance.worker_batch_support', 'function': 'run_fixed_plan'}], 'toolsets': []}
    if runtime == 'smolagents':
        supervisor['runtime_options'] = {'max_steps': 4, 'smart_summary': False, 'todo_mode': 'off'}
    write_yaml(workflow, supervisor)
    for index, token in enumerate(TOKENS[:count]):
        (root / f'item-{index}.txt').write_text(token + '\n')
    return workflow


def input_payload(request):
    for message in request['messages']:
        content = message.get('content')
        if isinstance(content, list):
            content = '\n'.join(part.get('text', '') for part in content)
        if isinstance(content, str) and 'Input data (JSON):\n' in content:
            return json.JSONDecoder().raw_decode(content.split('Input data (JSON):\n', 1)[1])[0]
    raise AssertionError('Worker request lacks real input payload')


def model_program(runtime, *, fail_item=None):
    def program(request):
        messages = tool_messages(request)
        if request['model'] == 'supervisor':
            return [('plan', 'run_fixed_plan', {})] if not messages else finish(request, messages[-1]['content'])
        payload = input_payload(request)
        inspected = [m for m in messages if m.get('name') == 'inspect_batch_item' or '"instance_id"' in str(m.get('content'))]
        if inspected:
            if payload['item_id'] == fail_item:
                from tests.application_test.mixed_runtime_support import ProviderFailure
                return ProviderFailure(400, 'fixed fixture failure after physical side effect')
            return finish(request, inspected[-1]['content'])
        if not messages:
            return [('read', 'read' if runtime == 'pi' else 'read_file',
                     {'path': payload['file_path']} if runtime == 'pi' else {'file_path': payload['file_path']}),
                    ('mcp', 'mcp__facts__lookup', {'query': payload['item_id']})]
        assert payload['fact'] in str(messages)
        assert '42' in str(messages), 'real MCP reply not observed'
        return [('inspect', 'inspect_batch_item', {'item_id': payload['item_id'], 'fact': payload['fact']})]
    return program


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.2):
            return True
    except OSError:
        return False


@contextmanager
def capture_bridges(root):
    """Observe real bridge OS identities without mocking Pi's transport."""
    from agentloom.runtimes.pi.environment import build_pi_subprocess_env
    from agentloom.runtimes.pi.install import find_node
    binary = find_node(build_pi_subprocess_env())
    marker = root / 'bridge-processes.jsonl'
    launcher = root / 'bin/node'
    launcher.parent.mkdir(parents=True)
    launcher.write_text(f'''#!{sys.executable}
import json,os,sys
if sys.argv[1:] != ['--version'] and sys.argv[1:3] != ['-p','process.versions.modules']:
 fd=os.open({str(marker)!r},os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
 os.write(fd,(json.dumps({{'pid':os.getpid(),'cwd':os.getcwd()}})+'\\n').encode())
 os.close(fd)
os.execv({binary!r},[{binary!r},*sys.argv[1:]])
''')
    launcher.chmod(0o755)
    original = os.environ['PATH']
    os.environ['PATH'] = str(launcher.parent) + os.pathsep + original
    try:
        yield marker
    finally:
        os.environ['PATH'] = original


def resource_exit(root, resources, baseline_threads=()):
    mcp_path = root / 'mcp-events.jsonl'
    mcp = [json.loads(line) for line in mcp_path.read_text().splitlines()] if mcp_path.exists() else []
    marker = root / 'bridge-processes.jsonl'
    bridges = [json.loads(line) for line in marker.read_text().splitlines()] if marker.exists() else []
    pids = sorted({entry['pid'] for entry in mcp + bridges})
    live_pids = [pid for pid in pids if psutil.pid_exists(pid)]
    directories = [entry['cwd'] for entry in bridges if Path(entry['cwd']).name.startswith('agentloom-pi-')]
    open_ports = [r['port'] for r in resources if port_open(r['port'])]
    threads = [t.name for t in threading.enumerate() if t.ident not in baseline_threads
               and t.name.startswith(('pi-reader-', 'pi-callback-', 'asyncio-portal-'))]
    assert not live_pids, f'owned processes remain: {live_pids}'
    assert not open_ports, f'owned listeners remain: {open_ports}'
    assert not threads, f'owned callback threads remain: {threads}'
    assert all(not Path(path).exists() for path in directories), 'private bridge directory remains'
    return {'pids': pids, 'bridge_pids': [p['pid'] for p in bridges], 'mcp_pids': sorted({p['pid'] for p in mcp}),
            'live_pids': live_pids, 'private_directories': directories, 'open_ports': open_ports,
            'remaining_owned_threads': threads}


def verify_run(result, state, *, expected_success=True):
    from agentloom.execution import RuntimeHome
    from agentloom.execution.observability import inspect_run
    receipts = state.receipts
    assert len(receipts) == len(state.capacities), 'fixed business plan executed more than once'
    assert [r['execution']['concurrency'] for r in receipts] == list(state.capacities)
    manifest = json.loads(result.run.manifest_path.read_text())
    assert manifest['status'] == 'completed'
    assert {r['batch_id'] for r in manifest['worker_batches']} == {r['batch_id'] for r in receipts}
    context = RuntimeHome(state.root / 'runtime').context(application_id=result.run.application_id,
        task_id=result.run.task_id, run_id=result.run.run_id)
    all_ids = []
    started_ids = []
    for receipt in receipts:
        assert not receipt['observer_errors'], 'acceptance verifier observer failed'
        assert context.read_batch_record(receipt['batch_id'])['status'] == receipt['status']
        assert [o['item_id'] for o in receipt['outcomes']] == [f'item-{i}' for i in range(state.count)]
        if expected_success:
            assert receipt['status'] == 'completed'
            assert reduction(receipt) == list(TOKENS[:state.count])
        for outcome in receipt['outcomes']:
            if outcome['invocation_id'] is not None:
                started_ids.append(outcome['invocation_id'])
            if outcome['status'] != 'success':
                continue
            value = json.loads(outcome['output'])
            assert value['item_id'] == outcome['item_id']
            assert value['instance_id'] == outcome['invocation_id']
            assert value['root_run_id'] == outcome['root_run_id'] == result.run.run_id
            assert value['hook_run_id'] == value['local_run_id'] == outcome['local_run_id']
            assert value['task_id'] == outcome['task_id'] == result.run.task_id
            assert outcome['usage']['total_tokens'] > 0
            all_ids.append(outcome['invocation_id'])
    assert len(set(started_ids)) == len(started_ids), 'reused invocation identity'
    physical_calls = Counter(r['instance_id'] for r in state.resources)
    assert set(physical_calls).issubset(started_ids), 'physical Tool marker lacks a known invocation'
    assert all(count == 1 for count in physical_calls.values()), 'physical Tool executed more than once in an invocation'
    assert all(physical_calls[identity] == 1 for identity in all_ids), 'successful invocation lacks its physical Tool marker'
    assert len({r['input_fingerprint'] for r in receipts}) == 1
    assert len({r['definition_revision'] for r in receipts}) == 1
    assert len({r['config_revision'] for r in receipts}) == 1
    with inspect_run(result.run) as trace:
        events = trace.events()
        assert any(e['kind'] == 'model_request' for e in events)
        for receipt in receipts:
            for outcome in receipt['outcomes']:
                if outcome['status'] != 'success':
                    continue
                tools = [e for e in events if e['kind'] == 'tool' and e['status'] == 'completed'
                         and e['agent_id'] == outcome['local_run_id']]
                read = next(e for e in tools if e['tool_name'] in {'read', 'read_file'})
                mcp = next(e for e in tools if e['tool_name'] == 'mcp__facts__lookup')
                value = json.loads(outcome['output'])
                assert value['fact'] in trace.read_text(read['output_ref'])
                read_input = json.loads(trace.read_text(read['input_ref']))
                assert Path(read_input.get('path', read_input.get('file_path'))) == state.root / f"{outcome['item_id']}.txt"
                assert json.loads(trace.read_text(mcp['input_ref']))['query'] == outcome['item_id']
                assert '42' in trace.read_text(mcp['output_ref'])
                actual = next(r for r in state.resources if r['instance_id'] == outcome['invocation_id'])
                assert value == actual, 'model output differs from actual invocation-owned Tool evidence'
    checkpoints = json.loads(context.task_tree_path.read_text())
    assert len(checkpoints['workers']['batch_worker']) == len(started_ids)
    if expected_success:
        assert all(c['status'] == 'completed' for c in checkpoints['workers']['batch_worker'])
    exit_proof = resource_exit(state.root, state.resources, state.baseline_threads)
    return {'run_id': result.run.run_id, 'run_dir': str(result.run.run_dir),
            'batches': receipts, 'reduction': reduction(receipts[-1]), 'resource_exit': exit_proof,
            'owner_isolation': state.ownership}


def scan_secrets(root, secrets):
    scanned = 0
    for path in root.rglob('*'):
        if not path.is_file() or path.name == 'model.yaml':
            continue
        raw = path.read_bytes()
        assert not any(secret and secret.encode() in raw for secret in secrets), f'credential found in {path}'
        scanned += 1
    return {'scanned_files': scanned, 'credential_matches': 0}
