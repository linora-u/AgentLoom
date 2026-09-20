"""Ticket 11: mixed execution and curated memory at the Application boundary."""
import json
import re
from threading import Barrier

import pytest
import yaml

from agentloom.application.runner import execute_app
from agentloom.configuration.config import bind_config, load_project_config
from tests.application_test import mixed_runtime_support as support
from tests.application_test.mixed_runtime_support import finish, model_service, project, runtime_events, tool_messages, write_yaml


@pytest.mark.parametrize("supervisor,worker", [("smolagents", "pi"), ("pi", "smolagents")])
def test_mixed_supervisor_receives_the_workers_actual_native_read(tmp_path, supervisor, worker):
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
            if worker == 'smolagents':
                assert observation['status'] == 'completed'
                observation = json.loads(observation['output'])
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


@pytest.mark.parametrize('supervisor,worker', [('smolagents', 'pi'), ('pi', 'smolagents')])
def test_contextref_from_worker_retains_original_after_source_changes(tmp_path, supervisor, worker):
    original = ''.join(f'def release_item_{i}():\n    return {i}\n\n' for i in range(180)) + 'def TARGET_RECORD_CORIANDER_5287():\n    return 5287\n'
    source = tmp_path / 'large-note.py'
    source.write_text(original)
    refs = []

    def program(request):
        messages = tool_messages(request)
        if request['model'] == 'worker':
            if not messages:
                return [('large-outline', 'get_file_outline', {'file_path': str(source), 'max_items_per_section': 250})]
            match = re.search(r'ctx_[0-9a-f]{16}', messages[-1]['content'])
            assert match is not None
            ref = match.group()
            refs.append(ref)
            source.write_text('Changed after Worker read: WRONG-NEW-CONTENT\n')
            return finish(request, ref)
        if not messages:
            return [('delegate', 'inspect_note', {'query': 'large-note.py'})]
        if len(messages) == 1:
            match = re.search(r'ctx_[0-9a-f]{16}', messages[0]['content'])
            assert match is not None
            ref = match.group()
            return [('retrieve', 'loom_retrieve_context', {'ref': ref, 'query': 'TARGET_RECORD', 'limit': 3})]
        assert 'CORIANDER_5287' in messages[-1]['content']
        assert 'WRONG-NEW-CONTENT' not in messages[-1]['content']
        return finish(request, 'CORIANDER_5287')

    with model_service(program) as (url, _requests):
        workflow = project(tmp_path, url, supervisor=supervisor, worker=worker)
        definition = yaml.safe_load(workflow.read_text())
        worker_path = workflow.parent / 'worker_agents/inspect.yaml'
        worker_definition = yaml.safe_load(worker_path.read_text())
        worker_definition['tools'] = [{'name': 'get_file_outline'}]
        write_yaml(worker_path, worker_definition)
        definition['tools'] = [{'name': 'loom_retrieve_context'}]
        write_yaml(workflow, definition)
        system_path = tmp_path / 'config/system.yaml'
        system = yaml.safe_load(system_path.read_text())
        system['context_engine'] = {'min_chars': 1000, 'preview_max_chars': 300}
        write_yaml(system_path, system)
        with bind_config(load_project_config(tmp_path)):
            result = execute_app(workflow, file_logging=True)
    assert result.output == 'CORIANDER_5287'
    assert len(refs) == 1
    entries = [json.loads(p.read_text()) for p in (tmp_path / 'runtime').rglob(f'{refs[0]}.json')]
    assert len(entries) == 1
    assert entries[0]['tool_name'] == 'get_file_outline'
    assert 'TARGET_RECORD_CORIANDER_5287' in entries[0]['original']
    assert all(f'release_item_{i}' in entries[0]['original'] for i in range(180))
    assert 'WRONG-NEW-CONTENT' not in entries[0]['original']


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
