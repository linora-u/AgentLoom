"""Hook effects, metadata scheduling and owned Hook process cancellation in Pi."""
import json
import os
from pathlib import Path
import signal
import sys

import pytest

from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config
from tests.pi_test.test_application import model_service, project
from tests.pi_test.test_tools_application import select
from tests.pi_test.test_process_lifecycle import node_launcher, start_cli, until, assert_gone


def ordered_probe(label: str) -> str:
    """Return the label for mixed serial scheduling verification."""
    return label


def file_probe(file_path: str) -> str:
    """Read one fixture file for platform callback tests."""
    return Path(file_path).read_text()


def test_tool_hook_context_enters_exactly_the_next_internal_model_request(tmp_path):
    source = tmp_path / 'note.txt'
    source.write_text('Hook context source')
    hook = tmp_path / 'context.py'
    hook.write_text('import json\nprint(json.dumps({"decision":"allow", "agent_context":"NEXT-TURN-HOOK-4821"}))\n')
    with model_service(turns=[[('first', 'read', {'path': str(source)})], [('second', 'get_goal', {})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'read'}, {'name': 'get_goal', 'module': 'agentloom.tools.goal', 'function': 'get_goal'}],
               hooks={'PostToolUse': [{'id': 'context', 'matcher': 'read', 'command': f'{sys.executable} {hook}'}]})
        with bind_config(load_project_config(tmp_path)):
            execute_app(app, file_logging=False)
    assert len(requests) == 3
    assert 'NEXT-TURN-HOOK-4821' not in json.dumps(requests[0][1])
    assert json.dumps(requests[1][1]).count('NEXT-TURN-HOOK-4821') == 1
    assert 'NEXT-TURN-HOOK-4821' not in json.dumps(requests[2][1])


def test_native_post_hook_receives_full_result_when_model_gets_reference(tmp_path):
    import shlex

    observed = tmp_path / 'post-result.json'
    hook = tmp_path / 'post.py'
    hook.write_text(
        'import json,sys\nfrom pathlib import Path\n'
        'payload=json.load(sys.stdin)\n'
        'result=payload["tool_response"]["result"]\n'
        f'Path({str(observed)!r}).write_text(json.dumps({{"length":len(result),'
        '"has_tail":"NATIVE-HOOK-4821" in result}))\n'
        'print("{}")\n'
    )
    command = f'{shlex.quote(sys.executable)} -c "print(\'a\'*5000); print(\'NATIVE-HOOK-4821\')"'
    with model_service(turns=[[('large', 'bash', {'command': command})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'bash'}],
               shell_settings={'allowed_commands': ['*'], 'sandbox': {'enabled': False}},
               context_engine={'min_chars': 1000, 'preview_max_chars': 100},
               hooks={'PostToolUse': [{'id': 'capture-full', 'matcher': 'bash',
                                       'command': f'{sys.executable} {hook}'}]})
        with bind_config(load_project_config(tmp_path)):
            execute_app(app, file_logging=False)
    assert '[ContextRef ' in json.dumps(requests[-1][1])
    assert json.loads(observed.read_text()) == {'length': 5018, 'has_tail': True}


def test_unsafe_tool_metadata_serializes_actual_platform_callbacks(tmp_path):
    source = tmp_path / 'source.py'
    source.write_text('def ordered_tool_result():\n    return 1\n')
    trace = tmp_path / 'trace.txt'
    hook = tmp_path / 'order.py'
    hook.write_text('import json,sys,time\nfrom pathlib import Path\np=json.load(sys.stdin)\n'
                    f'with Path({str(trace)!r}).open("a") as f:f.write(sys.argv[1]+"\\n")\n'
                    'time.sleep(0.15 if sys.argv[1]=="begin" else 0)\nprint("{}")\n')
    with model_service(turns=[[('one', 'file_probe', {'file_path': str(source)}),
                               ('two', 'file_probe', {'file_path': str(source)})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'file_probe', 'module': 'tests.pi_test.test_hook_execution_application',
                           'function': 'file_probe'}],
            tool_metadata={'file_probe': {'is_concurrency_safe': False}},
            hooks={event: [{'id': event, 'matcher': 'file_probe', 'command': f'{sys.executable} {hook} {label}'}]
                   for event, label in [('PreToolUse', 'begin'), ('PostToolUse', 'end')]})
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert trace.read_text().splitlines() == ['begin', 'end', 'begin', 'end']
    assert result.output == 'Pi answer'
    assert len(requests) == 2
    schema = requests[0][1]['tools'][0]['function']['parameters']
    assert schema['properties']['file_path']['type'] == 'string'
    assert schema['required'] == ['file_path']


def test_unsafe_tool_serializes_the_entire_mixed_platform_batch(tmp_path):
    source = tmp_path / 'source.py'
    source.write_text('def mixed_order_result():\n    return 1\n')
    trace = tmp_path / 'trace.txt'
    hook = tmp_path / 'mixed_order.py'
    hook.write_text(
        'import json,sys,time\nfrom pathlib import Path\njson.load(sys.stdin)\n'
        f'with Path({str(trace)!r}).open("a") as f:f.write(sys.argv[1]+"\\n")\n'
        'time.sleep(0.1 if sys.argv[1].endswith("begin") else 0)\nprint("{}")\n'
    )
    with model_service(turns=[[('unsafe', 'file_probe', {'file_path': str(source)}),
                               ('safe', 'ordered_probe', {'label': 'safe'})]]) as (url, requests):
        app = project(tmp_path, url)
        select(
            app,
            tools=[
                {'name': 'file_probe', 'module': 'tests.pi_test.test_hook_execution_application',
                 'function': 'file_probe'},
                {'name': 'ordered_probe', 'module': __name__, 'function': 'ordered_probe'},
            ],
            tool_metadata={
                'file_probe': {'is_concurrency_safe': False},
                'ordered_probe': {'is_concurrency_safe': True},
            },
            hooks={
                event: [
                    {'id': f'{event}-probe-file', 'matcher': 'file_probe',
                     'command': f'{sys.executable} {hook} outline-{label}'},
                    {'id': f'{event}-probe', 'matcher': 'ordered_probe',
                     'command': f'{sys.executable} {hook} probe-{label}'},
                ]
                for event, label in [('PreToolUse', 'begin'), ('PostToolUse', 'end')]
            },
        )
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(app, file_logging=False)
    assert trace.read_text().splitlines() == [
        'outline-begin', 'outline-end', 'probe-begin', 'probe-end',
    ]
    assert result.output == 'Pi answer'
    assert len(requests) == 2


@pytest.mark.parametrize('fault', ['keyboard', 'bridge_exit'])
def test_cancel_during_tool_hook_reaps_hook_process_and_prevents_tool_execution(tmp_path, fault):
    marker = tmp_path / 'hook.pid'
    late = tmp_path / 'tool-executed'
    hook = tmp_path / 'waiting_hook.py'
    hook.write_text('import os,time\nfrom pathlib import Path\n'
                    f'Path({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\nprint("{{}}")\n')
    with model_service(turns=[[('pending', 'wait_for_cleanup', {'marker': str(late)})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'wait_for_cleanup', 'module': 'tests.pi_test.test_tools_application', 'function': 'wait_for_cleanup'}],
            hooks={'PreToolUse': [{'id': 'waiting', 'matcher': 'wait_for_cleanup', 'command': f'{sys.executable} {hook}', 'timeout': 120}]})
        env, bridge_marker = node_launcher(tmp_path)
        env['PYTHONPATH'] = str(Path(__file__).parents[2])
        child = start_cli(tmp_path, app, env)
        try:
            until(marker.exists)
            bridge_pid, hook_pid = int(bridge_marker.read_text()), int(marker.read_text())
            os.kill(child.pid if fault == 'keyboard' else bridge_pid,
                    signal.SIGINT if fault == 'keyboard' else signal.SIGKILL)
            stdout, stderr = child.communicate(timeout=7)
            assert_gone(hook_pid)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            for path in [marker, bridge_marker]:
                if path.exists():
                    try: os.kill(int(path.read_text()), signal.SIGKILL)
                    except ProcessLookupError: pass
    assert child.returncode != 0
    assert not late.exists()
    assert json.loads(stdout.splitlines()[-1])['event'] == ('run.interrupted' if fault == 'keyboard' else 'run.failed')


def test_cancelling_a_batch_larger_than_callback_pool_does_not_start_queued_hooks(tmp_path):
    markers = tmp_path / 'hook-pids.jsonl'
    hook = tmp_path / 'batch_hook.py'
    hook.write_text('import os,time\n'
                    f'with open({str(markers)!r},"a") as f:f.write(str(os.getpid())+"\\n")\n'
                    'time.sleep(60)\nprint("{}")\n')
    source = tmp_path / 'source.py'
    source.write_text('def untouched_outline():\n    pass\n')
    turns = [[(f'call-{n}', 'file_probe', {'file_path': str(source)}) for n in range(12)]]
    with model_service(turns=turns) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'file_probe', 'module': 'tests.pi_test.test_hook_execution_application',
                           'function': 'file_probe'}],
            hooks={'PreToolUse': [
                {'id': 'batch-wait', 'matcher': 'file_probe',
                 'command': f'{sys.executable} {hook}', 'timeout': 120}]})
        env, bridge_marker = node_launcher(tmp_path)
        env['PYTHONPATH'] = str(Path(__file__).parents[2])
        child = start_cli(tmp_path, app, env)
        try:
            until(lambda: markers.exists() and len(markers.read_text().splitlines()) == 8)
            os.kill(child.pid, signal.SIGINT)
            stdout, stderr = child.communicate(timeout=15)
            pids = [int(line) for line in markers.read_text().splitlines()]
            assert len(pids) == 8
            for pid in pids:
                assert_gone(pid)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            pids = [int(line) for line in markers.read_text().splitlines()] if markers.exists() else []
            if bridge_marker.exists():
                pids.append(int(bridge_marker.read_text()))
            for pid in pids:
                try: os.kill(pid, signal.SIGKILL)
                except ProcessLookupError: pass
    assert child.returncode != 0
    assert json.loads(stdout.splitlines()[-1])['event'] == 'run.interrupted'
    assert len(requests) == 1
