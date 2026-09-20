"""Curated memory crosses runtimes through the existing review and Run APIs."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from agentloom.application.runner import execute_app
from agentloom.configuration.config import bind_config, load_project_config
from agentloom.self_learning.persistence.evidence_gate import SQLiteEvidenceGate
from agentloom.self_learning.persistence.memory_store import MemoryStore
from agentloom.self_learning.persistence.ledger import SelfLearningLedger
from agentloom.self_learning.persistence.review_engine import ReviewEngine
from agentloom.self_learning.review_orchestration import ReviewOrchestrator
from tests.application_test.mixed_runtime_support import finish, model_service, project, tool_messages, write_yaml


FACT = 'Release export format is PIPE.'
POLICY_MODULE = 'applications.mixed_runtime_validation.agent_tools.release_policy'


@pytest.fixture(autouse=True)
def isolated_application_modules():
    # The production loader deliberately binds applications to one project
    # per interpreter. Pytest reuses its interpreter for distinct tmp projects.
    saved = {name: module for name, module in sys.modules.items()
             if name == 'applications' or name.startswith('applications.')}
    for name in saved:
        sys.modules.pop(name)
    try:
        yield
    finally:
        for name in list(sys.modules):
            if name == 'applications' or name.startswith('applications.'):
                sys.modules.pop(name)
        sys.modules.update(saved)


def memory_config():
    # Existing review configuration, local to the verification workspace.
    return {'enabled': True, 'review': {'enabled': True,
        'application': {'review_model': 'summary', 'trigger': {'mode': 'manual'},
                        'approval': {'fact': 'manual', 'experience': 'manual'}},
        'project': {'review_model': 'summary', 'trigger': {'mode': 'manual'},
                    'approval': {'fact': 'manual', 'experience': 'manual'}}}}


def configure_memory(root: Path, workflow: Path):
    system_path = root / 'config/system.yaml'
    system = yaml.safe_load(system_path.read_text())
    system['self_learning'] = memory_config()
    write_yaml(system_path, system)
    source = Path(__file__).resolve().parents[2] / 'applications/mixed_runtime_validation'
    shutil.copytree(source, root / 'applications/mixed_runtime_validation', dirs_exist_ok=True)
    policy = root / 'applications/mixed_runtime_validation/fixtures/release-policy.json'
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text('{"export_format":"PIPE"}')
    definition = yaml.safe_load(workflow.read_text())
    definition.pop('worker_agents')
    definition.update(tools=[{'name': 'read_file'}, {'name': 'read_release_policy',
        'module': POLICY_MODULE, 'function': 'read_release_policy'}],
        workflow='Read the release policy file and verify its export format using read_release_policy.')
    write_yaml(workflow, definition)
    return policy


@pytest.mark.parametrize('approval_timing', ['between_runs', 'during_next_root'])
def test_smol_evidence_is_reviewed_then_used_by_a_new_pi_run(tmp_path, approval_timing):
    phase = 'learn'
    policy_path = None

    def program(request):
        if request['model'] == 'summary':
            context = json.loads(request['messages'][-1]['content'])
            return json.dumps({'candidates': [{'kind': 'fact', 'memory_key': 'release-export-format',
                'payload': {'text': FACT}, 'provenance': context['allowed_provenance']}]})
        messages = tool_messages(request)
        if phase == 'learn':
            if not messages:
                return [('source-read', 'read_file', {'file_path': str(policy_path)})]
            if len(messages) == 1:
                assert 'PIPE' in messages[-1]['content']
                return [('policy-check', 'read_release_policy', {})]
            assert FACT in messages[-1]['content']
            return finish(request, 'Verified release policy')
        if phase == 'frozen':
            assert FACT not in json.dumps(request['messages'])
            if request['model'] == 'worker':
                return finish(request, 'Frozen snapshot retained')
            if not messages:
                approve()
                return [('frozen-worker', 'inspect_note', {'query': 'Check the inherited memory snapshot only.'})]
            return finish(request, 'Frozen snapshot retained')
        if phase in {'other_before_promotion', 'other_after_promotion'}:
            if not messages:
                assert (FACT in json.dumps(request['messages'])) == (phase == 'other_after_promotion')
                return [('project-memory', 'memory', {'action': 'list', 'scope': 'project'}),
                        ('application-memory', 'memory', {'action': 'list', 'scope': 'app'})]
            assert (FACT in str(messages)) == (phase == 'other_after_promotion')
            return finish(request, 'Scope checked')
        assert FACT in json.dumps(request['messages'])
        if not messages:
            return [('recall', 'memory', {'action': 'list', 'scope': 'app'})]
        assert FACT in messages[-1]['content']
        return finish(request, FACT)

    with model_service(program) as (url, requests):
        workflow = project(tmp_path, url, supervisor='smolagents', worker='pi')
        policy_path = configure_memory(tmp_path, workflow)
        with bind_config(load_project_config(tmp_path)):
            first = execute_app(workflow, file_logging=True)
            db = tmp_path / 'runtime/self_learning.db'
            config = {'application_id': 'mixed', 'self_learning': memory_config()}
            store = MemoryStore(db, agent_config=config)
            engine = ReviewEngine(db, evidence_gate=SQLiteEvidenceGate(db))
            assert store.list('app', scope_id='mixed') == []
            batch = ReviewOrchestrator(engine=engine, agent_config=config).run_review('application', 'mixed')
            assert len(batch.candidates) == 1
            candidate = batch.candidates[0]
            assert candidate.state == 'pending_pre_review'
            assert store.list('app', scope_id='mixed') == []
            def approve():
                applied = engine.apply_decisions('application', 'mixed', [{
                    'candidate_id': candidate.candidate_id, 'revision': candidate.revision, 'action': 'approve'}])
                assert applied['results'][0]['state'] == 'active_confirmed'

            if approval_timing == 'between_runs':
                approve()
            assert {p['root_run_id'] for p in candidate.provenance} == {first.run.run_id}
            # The next runtime cannot reread the source or inherit native history.
            policy_path.unlink()
            definition = yaml.safe_load(workflow.read_text())
            if approval_timing == 'during_next_root':
                definition.update(agent_runtime='pi', tools=[], worker_agents=[{'path': 'inspect.yaml'}],
                                  workflow='Ask the Worker to inspect only the inherited snapshot.')
                write_yaml(workflow, definition)
                worker_path = workflow.parent / 'worker_agents/inspect.yaml'
                worker_definition = yaml.safe_load(worker_path.read_text())
                worker_definition.update(agent_runtime='smolagents', tools=[])
                write_yaml(worker_path, worker_definition)
                phase = 'frozen'
                frozen = execute_app(workflow, file_logging=True)
                assert frozen.output == 'Frozen snapshot retained'
                definition.pop('worker_agents')
            definition.update(agent_runtime='pi', tools=[{'name': 'memory'}],
                              workflow='Use the approved memory to report the release export format.')
            write_yaml(workflow, definition)
            phase = 'recall'
            second = execute_app(workflow, file_logging=True)
            recalled = SelfLearningLedger(db).completed_review_context(second.run.run_id, tool_result_limit=100)
            assert recalled is not None
            assert any(row['tool_name'] == 'memory' and FACT in row['output_json']
                       for row in recalled['tool_results']), 'Injected memory must not taint the next Run ledger'

            other = tmp_path / 'applications/other/workflows/root.yaml'
            write_yaml(other, {**definition, 'name': 'other', 'workflow': 'Inspect both memory scopes.'})
            phase = 'other_before_promotion'
            assert execute_app(other, file_logging=True).output == 'Scope checked'
            assert store.list('app', scope_id='other') == []
            current = engine.status('application', 'mixed')['candidates'][0]
            promoted = engine.apply_decisions('application', 'mixed', [{
                'candidate_id': current['candidate_id'], 'revision': current['revision'], 'action': 'promote_project'}])
            assert promoted['applied'] == 1
            phase = 'other_after_promotion'
            assert execute_app(other, file_logging=True).output == 'Scope checked'
            assert store.list('app', scope_id='other') == []
            assert [item['content'] for item in store.list('project')] == [FACT]
    assert second.output == FACT
    assert second.run.run_id != first.run.run_id
    assert second.run.application_id == first.run.application_id == 'mixed'
    assert len([r for r in requests if r['model'] == 'summary']) == 1
