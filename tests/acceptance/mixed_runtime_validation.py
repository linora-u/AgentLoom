"""Opt-in, isolated real-provider campaign for Ticket 11 Applications."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
APP = 'mixed_runtime_validation'
FACT = 'Release export format is PIPE.'
CASES = tuple(f'{mode}_{direction}' for mode in ['read', 'parallel', 'goal', 'context']
              for direction in ['smol_to_pi', 'pi_to_smol']) + ('memory_handoff',)


def dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


def write_yaml(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False))


def review_settings(profile: str) -> dict:
    return {'enabled': True, 'review': {'enabled': True,
        **{scope: {'review_model': profile, 'trigger': {'mode': 'manual'},
                   'approval': {'fact': 'manual', 'experience': 'manual'}}
           for scope in ['application', 'project']}}}


def evidence(result, workspace: Path) -> dict:
    from agentloom.self_learning.persistence.ledger import SelfLearningLedger
    context = SelfLearningLedger(workspace / 'runtime/self_learning.db').completed_review_context(
        result.run.run_id, tool_result_limit=100)
    assert context is not None, 'Missing completed Run ledger'
    return {'run_id': result.run.run_id, 'run_dir': str(result.run.run_dir), 'output': result.output,
            'manifest': str(result.run.manifest_path), 'ledger': context,
            'goal': dict(result.goal) if result.goal else None}


def run_memory(workspace: Path, profile: str, workflow: Path) -> list[dict]:
    from agentloom.application.runner import execute_app
    from agentloom.self_learning.persistence.evidence_gate import SQLiteEvidenceGate
    from agentloom.self_learning.persistence.memory_store import MemoryStore
    from agentloom.self_learning.persistence.review_engine import ReviewEngine
    from agentloom.self_learning.review_orchestration import ReviewOrchestrator
    policy = workspace / f'applications/{APP}/fixtures/release-policy.json'
    definition = {'name': 'memory_handoff', 'agent_runtime': 'smolagents', 'model_type': profile,
        'checkpoint': {'enabled': False}, 'toolsets': [], 'tools': [{'name': 'read_file'},
        {'name': 'read_release_policy', 'module': f'applications.{APP}.agent_tools.release_policy', 'function': 'read_release_policy'}],
        'description': 'Verify one durable release fact.',
        'workflow': f'Read {policy} with read_file, then call read_release_policy to verify the export format. Report the exact fact returned by that tool. Do not propose or approve memory yourself.'}
    write_yaml(workflow, definition)
    first = execute_app(workflow, file_logging=True)
    proofs = [evidence(first, workspace)]
    tools = {row['tool_name'] for row in proofs[0]['ledger']['tool_results']}
    assert {'read_file', 'read_release_policy'} <= tools
    db = workspace / 'runtime/self_learning.db'
    config = {'application_id': APP, 'self_learning': review_settings(profile)}
    store = MemoryStore(db, agent_config=config)
    engine = ReviewEngine(db, evidence_gate=SQLiteEvidenceGate(db))
    assert not store.list('app', scope_id=APP)
    batch = ReviewOrchestrator(engine=engine, agent_config=config).run_review('application', APP)
    dump(workspace / 'review-batch.json', batch.to_dict())
    candidates = [candidate for candidate in batch.candidates if candidate.payload == {'text': FACT}]
    assert len(candidates) == 1, 'Review model did not retain the exact trusted domain fact'
    candidate = candidates[0]
    assert candidate.state == 'pending_pre_review'
    assert not store.list('app', scope_id=APP)
    applied = engine.apply_decisions('application', APP, [{'candidate_id': candidate.candidate_id,
        'revision': candidate.revision, 'action': 'approve'}])
    dump(workspace / 'review-approval.json', applied)
    assert applied['results'][0]['state'] == 'active_confirmed'
    assert {p['root_run_id'] for p in candidate.provenance} == {first.run.run_id}
    policy.unlink()
    definition.update(agent_runtime='pi', tools=[{'name': 'memory'}, {'name': 'session_search'}],
        workflow='Use memory(action="list", scope="app") and session_search(query="Release export format", scope="current_app"). State the exact approved release format fact and mention the prior verified run. The source file is no longer available.')
    write_yaml(workflow, definition)
    second = execute_app(workflow, file_logging=True)
    proofs.append(evidence(second, workspace))
    assert 'PIPE' in second.output
    rows = proofs[-1]['ledger']['tool_results']
    assert {'memory', 'session_search'} <= {row['tool_name'] for row in rows}
    assert any(row['tool_name'] == 'memory' and FACT in str(row['output_json']) for row in rows)
    assert any(row['tool_name'] == 'session_search' and first.run.run_id in str(row['output_json']) for row in rows)
    assert first.run.run_id != second.run.run_id
    dump(workspace / 'approved-memory.json', store.list('app', scope_id=APP))
    return proofs


def run_mixed(case: str, workspace: Path, profile: str) -> list[dict]:
    from agentloom.application.runner import execute_app
    mode, direction = case.split('_', 1)
    workflow = workspace / f'applications/{APP}/workflows/{direction}.yaml'
    definition = yaml.safe_load(workflow.read_text())
    definition.update(model_type=profile, concurrency=3)
    worker_path = workflow.parent / 'worker_agents' / definition['worker_agents'][0]['path']
    worker = yaml.safe_load(worker_path.read_text())
    worker['model_type'] = profile
    native = 'read' if worker['agent_runtime'] == 'pi' else 'read_file'
    fixtures = workspace / f'applications/{APP}/fixtures'
    tokens = ['MIXED-ALPHA-8149']
    if mode == 'parallel':
        tokens = ['MIXED-ALPHA-8149', 'MIXED-BETA-3952', 'MIXED-REPEAT-6471']
        for name, token in zip(['left', 'right', 'repeat'], tokens):
            (fixtures / f'{name}.txt').write_text(f'Verification token: {token}\n')
        definition['workflow'] = (f'Call inspect_note twice in one parallel batch, query={fixtures / "left.txt"} and query={fixtures / "right.txt"}. '
            f'After both finish, call inspect_note a third time with query={fixtures / "repeat.txt"}. Report all three actual file tokens.')
    elif mode == 'goal':
        definition['goal'] = True
        definition['workflow'] += ' After receiving the verified token, call get_goal and complete the root Goal with update_goal(status="complete", evidence=<actual Worker result>); then deliver the token.'
    elif mode == 'context':
        tokens = ['CORIANDER_5287']
        source = fixtures / 'release_catalog.py'
        source.write_text(''.join(f'def release_item_{i}():\n    return {i}\n\n' for i in range(180)) + 'def TARGET_RECORD_CORIANDER_5287():\n    return 5287\n')
        worker['tools'] = [{'name': 'get_file_outline'}]
        worker['workflow'] = 'Call get_file_outline(file_path=query, max_items_per_section=250). Return the exact ContextRef identifier from its result, without retrieving it yourself.'
        definition['tools'] = [{'name': 'loom_retrieve_context'}]
        definition['workflow'] = (f'Call inspect_note(query="{source}"). Using the Worker ContextRef, call loom_retrieve_context(ref=<actual ref>, query="TARGET_RECORD", limit=3). Report the function name returned by retrieval.')
    write_yaml(worker_path, worker)
    write_yaml(workflow, definition)
    result = execute_app(workflow, file_logging=True)
    proof = evidence(result, workspace)
    rows = proof['ledger']['tool_results']
    assert all(token in result.output for token in tokens), 'Missing independently known file token'
    if mode == 'context':
        assert {'get_file_outline', 'loom_retrieve_context'} <= {row['tool_name'] for row in rows}
        entries = [json.loads(p.read_text()) for p in (workspace / 'runtime').rglob('ctx_*.json')]
        assert any(entry.get('tool_name') == 'get_file_outline' and 'TARGET_RECORD_CORIANDER_5287' in entry['original'] for entry in entries)
        assert any(row['tool_name'] == 'loom_retrieve_context' and 'CORIANDER_5287' in str(row['output_json']) for row in rows)
    else:
        reads = [row for row in rows if row['tool_name'] == native]
        assert len(reads) >= len(tokens)
        assert all(any(token in str(row['output_json']) for row in reads) for token in tokens)
        if mode == 'parallel':
            assert len({row['run_id'] for row in reads}) >= 3, 'Repeated Worker reused its local Run'
        if native == 'read':
            commits = [json.loads(p.read_text()) for p in (result.run.run_dir / 'native-tools').rglob('*.json')]
            committed = [entry for entry in commits if entry.get('state') == 'committed']
            assert len(committed) >= len(tokens)
            assert all(entry['request']['tool']['provider'] == 'pi' for entry in committed)
        if mode == 'goal':
            assert result.goal and result.goal['status'] == 'complete' and tokens[0] in result.goal['evidence']
    return [proof]


def child(case: str, workspace: Path, profile: str) -> None:
    workspace.mkdir(parents=True, mode=0o700, exist_ok=False)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip())
    attempt = {'case': case, 'profile': profile, 'revision': revision, 'dirty': dirty, 'started_at': datetime.now(UTC).isoformat()}
    dump(workspace / 'attempt.json', attempt)
    shutil.copytree(ROOT / f'applications/{APP}', workspace / f'applications/{APP}')
    config = workspace / 'config'
    config.mkdir(mode=0o700)
    shutil.copyfile(ROOT / 'config/llm.yaml', config / 'llm.yaml')
    (config / 'llm.yaml').chmod(0o600)
    write_yaml(config / 'system.yaml', {'runtime': {'root_dir': str(workspace / 'runtime')},
        'self_learning': review_settings(profile), 'default_toolsets': [], 'checkpoint': {'enabled': False},
        'lsp_servers': {'enabled': False}, 'logging': {'console_enabled': False},
        'context_engine': {'min_chars': 1000, 'preview_max_chars': 300}})
    from agentloom.configuration.config import bind_config, load_project_config
    with bind_config(load_project_config(workspace)):
        proofs = (run_memory(workspace, profile, workspace / f'applications/{APP}/workflows/memory.yaml')
                  if case == 'memory_handoff' else run_mixed(case, workspace, profile))
    dump(workspace / 'report.json', {**attempt, 'status': 'passed', 'provider': 'real', 'runs': proofs})


def campaign(output: Path, cases: list[str], profiles: list[str], deadline: int) -> bool:
    output.mkdir(parents=True, mode=0o700, exist_ok=False)

    def run(job):
        case, profile = job
        name = f'{profile}-{case}'
        started = time.monotonic()
        timed_out = False
        with (output / f'{name}.log').open('w') as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child', case,
                '--profiles', profile, '--output', str(output / name)], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                process.wait(timeout=deadline)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.kill(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        record = {'case': case, 'profile': profile, 'exit_code': process.returncode, 'timed_out': timed_out,
                  'passed': process.returncode == 0 and (output / name / 'report.json').exists(),
                  'seconds': round(time.monotonic() - started, 2)}
        print(json.dumps(record), flush=True)
        return record

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, [(case, profile) for profile in profiles for case in cases]))
    dump(output / 'campaign.json', {'total': len(results), 'passed': sum(r['passed'] for r in results), 'cases': results})
    return all(r['passed'] for r in results)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--child', choices=CASES)
    parser.add_argument('--cases', nargs='+', choices=CASES, default=list(CASES))
    parser.add_argument('--profiles', nargs='+', default=['powerful', 'responses_powerful'])
    parser.add_argument('--deadline', type=int, default=240)
    args = parser.parse_args()
    if args.child:
        child(args.child, args.output.resolve(), args.profiles[0])
    else:
        sys.exit(0 if campaign(args.output.resolve(), args.cases, args.profiles, args.deadline) else 1)
