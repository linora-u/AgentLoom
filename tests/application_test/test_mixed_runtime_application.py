"""Ticket 11: mixed execution and curated memory at the Application boundary."""
import json
import re
import sys
from threading import Barrier

import pytest
import yaml
from agentloom.app.runner import execute_app
from agentloom.config.config import bind_config, load_project_config

from tests.application_test import mixed_runtime_support as support
from tests.application_test.mixed_runtime_support import (
    finish,
    model_service,
    project,
    runtime_events,
    tool_messages,
    write_yaml,
)


@pytest.mark.parametrize("supervisor,worker", [("smolagents", "pi"), ("pi", "smolagents")])
def test_mixed_supervisor_receives_the_workers_actual_native_read(tmp_path, supervisor, worker):
    from agentloom.execution.observability import inspect_run

    (tmp_path / 'note.txt').write_text('Repository verification token: SAFFRON-7419\n')

    def program(request):
        messages = tool_messages(request)
        if request['model'] == 'worker':
            if not messages:
                return [('read-note', 'read' if worker == 'pi' else 'read_file',
                         {'path': 'note.txt'} if worker == 'pi' else {'file_path': str(tmp_path / 'note.txt')})]
            assert 'SAFFRON-7419' in messages[-1]['content']
            return finish(request, 'SAFFRON-7419')
        if not messages:
            return [('delegate', 'inspect_note', {'query': 'note.txt'})]
        assert 'SAFFRON-7419' in messages[-1]['content']
        return finish(request, 'Verified SAFFRON-7419')

    with model_service(program) as (url, requests):
        workflow = project(tmp_path, url, supervisor=supervisor, worker=worker)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=False)
    assert result.output == 'Verified SAFFRON-7419'
    assert {request['model'] for request in requests} == {'supervisor', 'worker'}
    events = runtime_events(result)
    reads = [event['details'] for event in events if event['kind'] == 'tool'
             and event['details'].get('record', {}).get('tool_name') == 'read']
    if worker == 'pi':
        assert len(reads) == 1
        assert reads[0]['provider'] == 'pi'
        assert reads[0]['record']['status'] == 'completed'
    else:
        assert any(e['kind'] == 'tool' and e['details'].get('name') == 'read_file'
                   and e['details']['status'] == 'completed' for e in events)
    entries = [json.loads(p.read_text()) for p in (result.run.run_dir / 'native-tools').rglob('*.json')]
    assert len([entry for entry in entries if entry.get('state') == 'committed']) == int(worker == 'pi')
    worker_requests = [r for r in requests if r['model'] == 'worker']
    names = {t['function']['name'] for t in worker_requests[0]['tools']}
    assert ('read' in names, 'read_file' in names) == (worker == 'pi', worker == 'smolagents')
    smol_model = 'supervisor' if supervisor == 'smolagents' else 'worker'
    with inspect_run(result.run) as trace:
        model_requests = [event for event in trace.events() if event['kind'] == 'model_request'
                          and event['runtime'] == 'smolagents']
        model_responses = [event for event in trace.events() if event['kind'] == 'model_response'
                           and event['runtime'] == 'smolagents']
        sent = [request for request in requests if request['model'] == smol_model]
        assert len(model_requests) == len(model_responses) == len(sent)
        for event, actual in zip(model_requests, sent, strict=True):
            saved = json.loads(trace.read_text(event['request_ref']))
            assert event['boundary'] == 'litellm_input'
            assert event['provider_request_complete'] is False
            assert saved['model'] == f"openai/{actual['model']}"
            assert saved['tools'] == actual['tools']
            assert [{key: value for key, value in message.items() if value is not None}
                    for message in saved['messages']] == actual['messages']
        assert {event['model_turn_id'] for event in model_requests} == {
            event['model_turn_id'] for event in model_responses}
        all_requests = [event for event in trace.events() if event['kind'] == 'model_request']
        assert all(event['provider_request_complete'] is False for event in all_requests)
        assert [event['run_step_number'] for event in all_requests] == list(range(1, len(all_requests) + 1))
        worker_request = next(event for event in all_requests
                              if event['kind'] == 'model_request' and event['runtime'] == worker)
        step = trace.inspect_step(worker_request['run_step_number'])
        assert step['agent_id'] == worker_request['agent_id']
        assert [event['kind'] for event in step['events']].count('model_request') == 1
        assert [event['kind'] for event in step['events']].count('model_response') == 1
        tool = next(event for event in step['events'] if event['kind'] == 'tool')
        assert tool['tool_name'] == ('read' if worker == 'pi' else 'read_file')
        assert 'SAFFRON-7419' in step['payloads'][tool['model_ref']]
        sizes = sorted(trace.reference_metadata(ref)['size'] for ref in step['payload_refs'])
        assert len(sizes) >= 2 and sizes[0] > 0
        inline_budget = sizes[1]
        bounded = trace.inspect_step(worker_request['run_step_number'], max_inline_bytes=inline_budget)
        assert sum(len(value.encode('utf-8')) for value in bounded['payloads'].values()) <= inline_budget


@pytest.mark.parametrize('worker', ['pi', 'smolagents'])
def test_small_native_or_platform_result_is_redacted_before_model_and_trace(tmp_path, worker):
    from agentloom.execution.observability import inspect_run

    (tmp_path / 'note.txt').write_text('api_key=fixture-secret\nstatus=ok\n')

    def program(request):
        messages = tool_messages(request)
        if request['model'] == 'worker':
            if not messages:
                return [('read-note', 'read' if worker == 'pi' else 'read_file',
                         {'path': 'note.txt'} if worker == 'pi' else {'file_path': str(tmp_path / 'note.txt')})]
            content = messages[-1]['content']
            assert 'fixture-secret' not in content
            assert 'api_key=[REDACTED]' in content
            return finish(request, 'redacted')
        return [('delegate', 'inspect_note', {'query': 'note.txt'})] if not messages else finish(request, 'verified')

    with model_service(program) as (url, requests):
        workflow = project(tmp_path, url, supervisor='pi', worker=worker)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=False)
    assert result.output == 'verified'
    worker_messages = tool_messages(next(request for request in requests if request['model'] == 'worker' and tool_messages(request)))
    with inspect_run(result.run) as trace:
        tool = next(event for event in trace.events() if event['kind'] == 'tool' and event['tool_name'] in {'read', 'read_file'})
        assert trace.read_text(tool['model_ref']) == worker_messages[-1]['content']


@pytest.mark.parametrize('runtime', ['pi', 'smolagents'])
def test_failed_platform_tool_error_matches_model_trace_and_log(tmp_path, runtime):
    from agentloom.execution.observability import inspect_run

    def program(request):
        messages = tool_messages(request)
        if not messages:
            return [('secret-failure', 'fail_with_secret', {'value': 'sample'})]
        assert 'fixture-secret' not in messages[-1]['content']
        assert 'api_key=[REDACTED]' in messages[-1]['content']
        return finish(request, 'handled')

    with model_service(program) as (url, requests):
        workflow = project(tmp_path, url, supervisor=runtime, worker=runtime)
        definition = yaml.safe_load(workflow.read_text())
        definition['worker_agents'] = []
        definition['tools'] = [{
            'name': 'fail_with_secret',
            'module': 'tests.application_test.test_platform_tool_application',
            'function': 'fail_with_secret',
        }]
        write_yaml(workflow, definition)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=True)
    assert result.output == 'handled'
    with inspect_run(result.run) as trace:
        tool = next(event for event in trace.events() if event['kind'] == 'tool'
                    and event['tool_name'] == 'fail_with_secret')
        visible = trace.read_text(tool['model_ref'])
    assert visible == tool_messages(requests[1])[-1]['content']
    assert result.run.log_path is not None
    assert f'Observations: {visible}' in result.run.log_path.read_text()


def test_pi_structured_platform_result_matches_trace_and_observations(tmp_path):
    from agentloom.execution.observability import inspect_run

    def program(request):
        messages = tool_messages(request)
        if not messages:
            return [('structured-result', 'structured_with_secret', {'value': 'sample'})]
        content = messages[-1]['content']
        assert json.loads(content) == {
            'api_key': '[REDACTED]', 'status': 'ok', 'value': 'sample',
        }
        return finish(request, 'handled')

    with model_service(program) as (url, requests):
        workflow = project(tmp_path, url, supervisor='pi', worker='pi')
        definition = yaml.safe_load(workflow.read_text())
        definition['worker_agents'] = []
        definition['tools'] = [{
            'name': 'structured_with_secret',
            'module': 'tests.application_test.test_platform_tool_application',
            'function': 'structured_with_secret',
        }]
        write_yaml(workflow, definition)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=True)
    assert result.output == 'handled'
    with inspect_run(result.run) as trace:
        tool = next(event for event in trace.events() if event['kind'] == 'tool'
                    and event['tool_name'] == 'structured_with_secret')
        visible = trace.read_text(tool['model_ref'])
    assert visible == tool_messages(requests[1])[-1]['content']
    assert result.run.log_path is not None
    assert f'Observations: {visible}' in result.run.log_path.read_text()


@pytest.mark.parametrize('runtime', ['pi', 'smolagents'])
def test_blocked_tool_result_matches_model_and_trace(tmp_path, runtime):
    from agentloom.execution.observability import inspect_run

    hook = tmp_path / 'block_tool.py'
    hook.write_text(
        'import json\nprint(json.dumps({"decision":"block",'
        '"reason":"api_key=fixture-secret"}))\n'
    )

    def program(request):
        messages = tool_messages(request)
        if not messages:
            return [('blocked-call', 'trace_payload', {'value': 'sample'})]
        content = messages[-1]['content']
        payload = json.loads(content)
        assert payload['status'] == 'blocked'
        assert payload['error']['message'] == 'api_key=[REDACTED]'
        return finish(request, 'handled')

    with model_service(program) as (url, requests):
        workflow = project(tmp_path, url, supervisor=runtime, worker=runtime)
        definition = yaml.safe_load(workflow.read_text())
        definition['worker_agents'] = []
        definition['tools'] = [{
            'name': 'trace_payload',
            'module': 'tests.application_test.test_platform_tool_application',
            'function': 'trace_payload',
        }]
        definition['hooks'] = {'PreToolUse': [{
            'id': 'block-tool', 'matcher': 'trace_payload',
            'command': f'{sys.executable} {hook}',
        }]}
        write_yaml(workflow, definition)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=True)
    assert result.output == 'handled'
    with inspect_run(result.run) as trace:
        tool = next(event for event in trace.events() if event['kind'] == 'tool'
                    and event['tool_name'] == 'trace_payload')
        visible = trace.read_text(tool['model_ref'])
    assert visible == tool_messages(requests[1])[-1]['content']
    assert result.run.log_path is not None
    assert f'Observations: {visible}' in result.run.log_path.read_text()


@pytest.mark.parametrize('supervisor,worker', [('pi', 'pi'), ('smolagents', 'pi'), ('pi', 'smolagents')])
def test_steps_and_tool_observations_reach_terminal_and_runtime_log(tmp_path, capsys, supervisor, worker):
    (tmp_path / 'note.txt').write_text('Visible result: MARIGOLD-8372\n')

    def program(request):
        messages = tool_messages(request)
        if request['model'] == 'worker':
            if not messages:
                return [('read-note', 'read' if worker == 'pi' else 'read_file',
                         {'path': 'note.txt'} if worker == 'pi' else {'file_path': str(tmp_path / 'note.txt')})]
            return finish(request, 'MARIGOLD-8372')
        if not messages:
            return [('delegate', 'inspect_note', {'query': 'note.txt'})]
        return finish(request, 'Verified MARIGOLD-8372')

    with model_service(program) as (url, _requests):
        workflow = project(tmp_path, url, supervisor=supervisor, worker=worker)
        system_path = tmp_path / 'config/system.yaml'
        system = yaml.safe_load(system_path.read_text())
        system['logging'] = {'console_enabled': True, 'file_enabled': True}
        write_yaml(system_path, system)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=True)
    terminal = capsys.readouterr().out
    assert result.output == 'Verified MARIGOLD-8372'
    assert result.run.log_path is not None
    log = result.run.log_path.read_text()
    assert terminal.count('New run') == log.count('New run') == 2
    for number in range(1, 5):
        assert sum(f'Step {number}' in line for line in terminal.splitlines() if '━' in line) == 1
        assert sum(f'Step {number}' in line for line in log.splitlines() if '━' in line) == 1
    for text in ('Calling tool:', 'Observations:', 'MARIGOLD-8372'):
        assert text in terminal and text in log
    assert terminal.count('Duration') == log.count('Duration') == 4
    assert 'Input tokens:' in terminal and 'Input tokens:' in log
    assert terminal.count('Final answer: Verified MARIGOLD-8372') == 1
    assert log.count('Final answer: Verified MARIGOLD-8372') == 1


@pytest.mark.parametrize('level', ['INFO', 'ERROR'])
def test_text_cli_prints_final_answer_once_with_pi_steps(tmp_path, level):
    from agentloom.__main__ import main
    from click.testing import CliRunner

    (tmp_path / 'note.txt').write_text('CLI token: DAHLIA-6104\n')

    def program(request):
        messages = tool_messages(request)
        if request['model'] == 'worker':
            return [('read-note', 'read', {'path': 'note.txt'})] if not messages else 'DAHLIA-6104'
        return [('delegate', 'inspect_note', {'query': 'note.txt'})] if not messages else 'Verified DAHLIA-6104'

    with model_service(program) as (url, _requests):
        workflow = project(tmp_path, url, supervisor='pi', worker='pi')
        system_path = tmp_path / 'config/system.yaml'
        system = yaml.safe_load(system_path.read_text())
        system['logging'] = {'console_enabled': True, 'file_enabled': True, 'level': level}
        write_yaml(system_path, system)
        with bind_config(load_project_config(tmp_path)):
            result = CliRunner().invoke(main, ['run', str(workflow)])
    assert result.exit_code == 0, result.output
    assert result.stdout.count('Verified DAHLIA-6104') == 1
    if level == 'INFO':
        assert result.stdout.count('Final answer: Verified DAHLIA-6104') == 1
        assert 'Step 1' in result.stdout
        logs = list((tmp_path / 'runtime/runs/mixed').glob('*/logs/runtime.log'))
        assert len(logs) == 1
        assert logs[0].read_text().count('Final answer: Verified DAHLIA-6104') == 1


@pytest.mark.parametrize('runtime', ['pi', 'smolagents'])
def test_debug_log_contains_the_models_reply_for_both_runtimes(tmp_path, runtime):
    (tmp_path / 'note.txt').write_text('Reply token: AZALEA-4920\n')

    def program(request):
        messages = tool_messages(request)
        if request['model'] == 'worker':
            if not messages:
                return [('read-note', 'read' if runtime == 'pi' else 'read_file',
                         {'path': 'note.txt'} if runtime == 'pi' else {'file_path': str(tmp_path / 'note.txt')})]
            return finish(request, 'AZALEA-4920')
        return [('delegate', 'inspect_note', {'query': 'note.txt'})] if not messages else finish(request, 'Verified AZALEA-4920')

    with model_service(program) as (url, _requests):
        workflow = project(tmp_path, url, supervisor=runtime, worker=runtime)
        system_path = tmp_path / 'config/system.yaml'
        system = yaml.safe_load(system_path.read_text())
        system['logging'] = {'console_enabled': False, 'file_enabled': True, 'level': 'DEBUG'}
        write_yaml(system_path, system)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=True)
    assert result.run.log_path is not None
    log = result.run.log_path.read_text()
    assert 'Model response:' in log
    assert 'AZALEA-4920' in log


def test_smol_supervisor_trace_records_stop_rejection_and_acceptance(tmp_path):
    from agentloom.execution.observability import inspect_run

    marker = tmp_path / 'stop-count'
    hook = tmp_path / 'stop_once.py'
    hook.write_text(
        'import json\nfrom pathlib import Path\n'
        f'p=Path({str(marker)!r})\n'
        'count=int(p.read_text()) if p.exists() else 0\n'
        'p.write_text(str(count+1))\n'
        'print(json.dumps({"decision":"block","reason":"continue once"}'
        ' if count==0 else {"decision":"allow"}))\n'
    )

    def program(request):
        return finish(request, 'accepted-answer-8426')

    with model_service(program) as (url, requests):
        workflow = project(tmp_path, url, supervisor='smolagents', worker='pi')
        definition = yaml.safe_load(workflow.read_text())
        definition['hooks'] = {'Stop': [{'id': 'stop-once', 'command': f'{sys.executable} {hook}'}]}
        write_yaml(workflow, definition)
        system_path = tmp_path / 'config/system.yaml'
        system = yaml.safe_load(system_path.read_text())
        system['logging'] = {'console_enabled': False, 'file_enabled': True}
        write_yaml(system_path, system)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=True)
    assert result.output == 'accepted-answer-8426'
    assert len(requests) >= 2
    with inspect_run(result.run) as trace:
        decisions = [event for event in trace.events()
                     if event['kind'] == 'hook_decision' and event['event'] == 'Stop']
        assert [json.loads(trace.read_text(event['decision_ref']))['result']['decision']
                for event in decisions] == ['block', 'allow']
    assert result.run.log_path is not None
    log = result.run.log_path.read_text()
    assert 'Stop blocked: continue once' in log
    assert log.count('Final answer: accepted-answer-8426') == 1


@pytest.mark.parametrize('supervisor,worker', [('smolagents', 'pi'), ('pi', 'smolagents')])
def test_parallel_and_repeated_workers_have_separate_sessions_and_hook_owners(tmp_path, supervisor, worker):
    support.parallel_gate = Barrier(2)
    for label in ['LEFT', 'RIGHT', 'REPEAT']:
        (tmp_path / f'{label}.txt').write_text(f'{label}-TOKEN-2391\n')
    observations = []

    def program(request):
        messages = tool_messages(request)
        if request['model'] == 'worker':
            task = '\n'.join(str(m['content']) for m in request['messages'] if m['role'] == 'user')
            label = next(label for label in ['LEFT', 'RIGHT', 'REPEAT'] if f'{label}.txt' in task)
            if not messages:
                assert all(f'{other}-TOKEN-2391' not in json.dumps(request) for other in ['LEFT', 'RIGHT', 'REPEAT'] if other != label)
                return [('same-provider-call', 'read' if worker == 'pi' else 'read_file',
                    {'path': f'{label}.txt'} if worker == 'pi' else {'file_path': str(tmp_path / f'{label}.txt')})]
            if len(messages) == 1:
                assert f'{label}-TOKEN-2391' in messages[0]['content']
                return [('same-probe-call', 'inspect_invocation', {'label': f'{label}-TOKEN-2391', 'synchronize': label != 'REPEAT'})]
            observation = json.loads(messages[-1]['content'])
            assert 'instance_id' in observation, observation
            observations.append(observation)
            return finish(request, json.dumps(observation))
        if not messages:
            return [('left', 'inspect_note', {'query': 'LEFT.txt'}), ('right', 'inspect_note', {'query': 'RIGHT.txt'})]
        if len(messages) == 2:
            assert 'LEFT-TOKEN-2391' in str(messages) and 'RIGHT-TOKEN-2391' in str(messages)
            return [('repeat', 'inspect_note', {'query': 'REPEAT.txt'})]
        assert 'REPEAT-TOKEN-2391' in messages[-1]['content']
        return finish(request, 'Three isolated real Workers completed')

    with model_service(program) as (url, _requests):
        workflow = project(tmp_path, url, supervisor=supervisor, worker=worker)
        path = workflow.parent / 'worker_agents/inspect.yaml'
        config = yaml.safe_load(path.read_text())
        config['tools'].append({'name': 'inspect_invocation', 'module': support.__name__, 'function': 'inspect_invocation'})
        write_yaml(path, config)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=False)
    assert result.output == 'Three isolated real Workers completed'
    assert len(observations) == 3
    assert len({o['instance_id'] for o in observations}) == 3
    assert len({o['local_run_id'] for o in observations}) == 3
    assert all(o['hook_run_id'] == o['local_run_id'] for o in observations)
    assert {o['root_run_id'] for o in observations} == {result.run.run_id}
    assert {o['run_id'] for o in observations} == {result.run.run_id}
    assert {o['application_id'] for o in observations} == {'mixed'}
    assert {o['task_id'] for o in observations} == {result.run.task_id}
    entries = [json.loads(p.read_text()) for p in (result.run.run_dir / 'native-tools').rglob('*.json')]
    committed = [entry for entry in entries if entry.get('state') == 'committed']
    assert len(committed) == (3 if worker == 'pi' else 0)
    assert len({entry['request']['identity']['instance_id'] for entry in committed}) == len(committed)
    if worker == 'pi':
        identities = [entry['request']['identity'] for entry in committed]
        assert len({identity['native_session_id'] for identity in identities} - {None, ''}) == 3
        assert {identity['call_id'] for identity in identities} == {'same-provider-call'}
        assert {identity['instance_id'] for identity in identities} == {o['instance_id'] for o in observations}
        assert all(identity['run_id'] == result.run.run_id and identity['task_id'] == result.run.task_id for identity in identities)


@pytest.mark.parametrize('supervisor,worker', [('smolagents', 'pi'), ('pi', 'smolagents')])
def test_contextref_from_worker_retains_original_after_source_changes(tmp_path, supervisor, worker):
    original = ''.join(f'def release_item_{i}():\n    return {i}\n\n' for i in range(30000)) + 'def TARGET_RECORD_CORIANDER_5287():\n    return 5287\n'
    source = tmp_path / 'large-note.py'
    source.write_text(original)
    refs = []

    def program(request):
        messages = tool_messages(request)
        if request['model'] == 'worker':
            if not messages:
                return [('large-read', 'read_context_fixture', {'file_path': str(source)})]
            match = re.search(r'ctx_[0-9a-f]{32}', messages[-1]['content'])
            assert match is not None
            ref = match.group()
            refs.append(ref)
            source.write_text('Changed after Worker read: WRONG-NEW-CONTENT\n')
            return finish(request, ref)
        if not messages:
            return [('delegate', 'inspect_note', {'query': 'large-note.py'})]
        if len(messages) == 1:
            match = re.search(r'ctx_[0-9a-f]{32}', messages[0]['content'])
            assert match is not None
            ref = match.group()
            return [('retrieve', 'loom_retrieve_context', {'ref': ref, 'query': 'TARGET_RECORD', 'limit': 3})]
        if 'CORIANDER_5287' not in messages[-1]['content']:
            next_offset = re.search(r'next_offset=(\d+)', messages[-1]['content'])
            assert next_offset is not None
            return [(f'retrieve-{len(messages)}', 'loom_retrieve_context',
                     {'ref': refs[0], 'query': 'TARGET_RECORD', 'offset': int(next_offset.group(1)), 'limit': 3})]
        assert 'WRONG-NEW-CONTENT' not in messages[-1]['content']
        return finish(request, 'CORIANDER_5287')

    with model_service(program) as (url, _requests):
        workflow = project(tmp_path, url, supervisor=supervisor, worker=worker)
        definition = yaml.safe_load(workflow.read_text())
        worker_path = workflow.parent / 'worker_agents/inspect.yaml'
        worker_definition = yaml.safe_load(worker_path.read_text())
        worker_definition['tools'] = [
            {
                'name': 'read_context_fixture',
                'module': 'tests.application_test.mixed_runtime_support',
                'function': 'read_context_fixture',
            },
        ]
        write_yaml(worker_path, worker_definition)
        definition['tools'] = []
        write_yaml(workflow, definition)
        system_path = tmp_path / 'config/system.yaml'
        system = yaml.safe_load(system_path.read_text())
        system['context_engine'] = {'min_chars': 1000, 'preview_max_chars': 300}
        write_yaml(system_path, system)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=True)
    assert result.output == 'CORIANDER_5287'
    assert len(refs) == 1
    from agentloom.execution.observability import inspect_run

    with inspect_run(result.run) as trace:
        metadata = trace.reference_metadata(refs[0])
        original = trace.read_text(refs[0])
        assert metadata['tool_name'] == 'read_context_fixture'
        assert 'TARGET_RECORD_CORIANDER_5287' in original
        assert 'release_item_0' in original and 'release_item_29999' in original
        assert 'WRONG-NEW-CONTENT' not in original
        page = trace.search_page(refs[0], 'TARGET_RECORD', limit=1)
        assert page.matches == [] and page.next_offset is not None
        page = trace.search_page(refs[0], 'TARGET_RECORD', offset=page.next_offset, limit=1)
        assert len(page.matches) == 1
        assert 'CORIANDER_5287' in page.matches[0][1]
        tool = next(event for event in trace.events()
                    if event['kind'] == 'tool' and event['tool_name'] == 'read_context_fixture')
        model_visible = trace.read_text(tool['model_ref'])
    assert result.run.log_path is not None
    assert f'Observations: {model_visible}' in result.run.log_path.read_text()


@pytest.mark.parametrize('supervisor,worker', [('smolagents', 'pi'), ('pi', 'smolagents')])
def test_only_root_can_complete_goal_after_mixed_worker_evidence(tmp_path, supervisor, worker):
    (tmp_path / 'note.txt').write_text('Goal verification token: GOAL-6257\n')

    def program(request):
        messages = tool_messages(request)
        if request['model'] == 'worker':
            if not messages:
                return [('worker-complete', 'update_goal', {'status': 'complete', 'evidence': 'Worker cannot complete root'})]
            if len(messages) == 1:
                assert 'root Supervisor' in messages[-1]['content']
                return [('goal-read', 'read' if worker == 'pi' else 'read_file',
                    {'path': 'note.txt'} if worker == 'pi' else {'file_path': str(tmp_path / 'note.txt')})]
            assert 'GOAL-6257' in messages[-1]['content']
            return finish(request, 'GOAL-6257')
        if not messages:
            return [('delegate', 'inspect_note', {'query': 'note.txt'})]
        if len(messages) == 1:
            assert 'GOAL-6257' in messages[-1]['content']
            return [('root-observe', 'get_goal', {})]
        if len(messages) == 2:
            assert 'active' in messages[-1]['content']
            return [('root-complete', 'update_goal', {'status': 'complete', 'evidence': 'Root verified GOAL-6257 from the mixed Worker'})]
        return finish(request, 'Verified GOAL-6257')

    with model_service(program) as (url, _requests):
        workflow = project(tmp_path, url, supervisor=supervisor, worker=worker)
        definition = yaml.safe_load(workflow.read_text())
        definition['goal'] = True
        write_yaml(workflow, definition)
        worker_path = workflow.parent / 'worker_agents/inspect.yaml'
        definition = yaml.safe_load(worker_path.read_text())
        definition['tools'].append({'name': 'update_goal', 'module': 'agentloom.tools.goal', 'function': 'update_goal'})
        write_yaml(worker_path, definition)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=True)
    assert result.goal is not None and result.goal['status'] == 'complete'
    assert 'GOAL-6257' in str(result.goal['evidence'])
