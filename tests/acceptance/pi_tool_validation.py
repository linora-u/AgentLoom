"""Opt-in real-provider Pi Application campaign; private profiles stay outside reports."""
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
from tests.acceptance.platform_tool_validation import configure_case, verify_case, dump

CASES = ('native_read', 'mcp', 'mcp_nested', 'mcp_error', 'goal', 'skill', 'skill_proposal',
         'memory', 'context', 'worker', 'parallel_workers', 'goal_worker')


def tool_records(run_dir: Path):
    records = {}
    for path in run_dir.rglob('runtime_events.jsonl'):
        for line in path.read_text().splitlines():
            event = json.loads(line)
            if event['kind'] == 'tool':
                detail = event['details']
                record = detail['record']
                records[(detail['instance_id'], record['call_id'])] = record
    return list(records.values())


def child(case: str, workspace: Path, profile: str):
    workspace.mkdir(parents=True, mode=0o700, exist_ok=False)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip())
    dump(workspace / 'attempt.json', {'case': case, 'profile': profile, 'revision': revision, 'dirty': dirty,
                                     'started_at': datetime.now(UTC).isoformat()})
    config = workspace / 'config'
    config.mkdir(mode=0o700)
    shutil.copyfile(ROOT / 'config/llm.yaml', config / 'llm.yaml')
    (config / 'llm.yaml').chmod(0o600)
    system = {'runtime': {'root_dir': str(workspace / 'runtime')}, 'checkpoint': {'enabled': False},
              'logging': {'console_enabled': False}, 'self_learning': {'enabled': False},
              'default_toolsets': []}
    workflow = workspace / 'applications' / case / 'workflows/root.yaml'
    workflow.parent.mkdir(parents=True)
    definition = {'name': f'validate_{case}', 'agent_runtime': 'pi', 'model_type': profile,
                  'toolsets': [], 'tools': [], 'concurrency': 'auto'}
    mcp = config / 'mcp.json'
    mcp.write_text(json.dumps({'mcpServers': {'facts': {'command': sys.executable,
        'args': [str(ROOT / 'tests/mcp_test/fixtures/stdio_server.py'), str(workspace / 'mcp-events.jsonl')]}}}))
    definition['mcp_servers'] = str(mcp)
    if case == 'native_read':
        (workspace / 'native-note.txt').write_text('Native verification token: PI-NATIVE-6941\n')
        definition.pop('mcp_servers')
        definition.update(tools=[{'name': 'read'}], description='Verify the native note.',
                          workflow='Use read to open native-note.txt and report its exact verification token.')
        expected = {'read'}
    else:
        expected = configure_case(case, workspace, workflow, system, definition)
        definition['workflow'] = definition['workflow'].replace('final_answer', 'your final response')
        for worker in workflow.parent.glob('worker_agents/*.yaml'):
            worker_config = yaml.safe_load(worker.read_text())
            worker_config.update(agent_runtime='pi', model_type=profile)
            worker_config.pop('runtime_options', None)
            worker_config['workflow'] = worker_config['workflow'].replace('final_answer', 'your final response')
            worker.write_text(yaml.safe_dump(worker_config))
    (config / 'system.yaml').write_text(yaml.safe_dump(system))
    workflow.write_text(yaml.safe_dump(definition))
    from agentloom.app.runner import execute_app
    from agentloom.config.config import bind_config, load_project_config
    with bind_config(load_project_config(workspace)):
        result = execute_app(workflow, file_logging=True)
    records = tool_records(result.run.run_dir)
    dump(workspace / 'tool-records.json', records)
    completed = {record['tool_name'] for record in records if record['status'] == 'completed'}
    assert expected <= completed, f'Missing successful tool calls: {sorted(expected - completed)}'
    proof = verify_case(case, workspace, records, result) if case != 'native_read' else {}
    if case == 'native_read':
        assert 'PI-NATIVE-6941' in str(result.output)
        assert any('PI-NATIVE-6941' in str(record['output']) for record in records)
        proof['native_journal'] = [str(path) for path in (result.run.run_dir / 'native-tools').rglob('*.json')]
        assert proof['native_journal']
    dump(workspace / 'report.json', {'case': case, 'profile': profile, 'status': 'passed',
        'revision': revision, 'dirty': dirty, 'runtime': 'pi', 'provider': 'real', 'sdk': '0.79.4',
        'output': result.output, 'run_id': result.run.run_id, 'run_dir': str(result.run.run_dir),
        'manifest_path': str(result.run.manifest_path), 'completed_tools': sorted(completed), **proof})


def campaign(destination: Path, cases: list[str], profiles: list[str], deadline: int, *, script: Path = Path(__file__)):
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)

    def run(job):
        profile, case = job
        name = f'{profile}-{case}'
        workspace = destination / name
        started = time.monotonic()
        timed_out = False
        with (destination / f'{name}.log').open('w') as log:
            process = subprocess.Popen([sys.executable, str(script.resolve()), '--child', case,
                '--output', str(workspace), '--profiles', profile], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
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
                  'passed': process.returncode == 0 and (workspace / 'report.json').exists(),
                  'seconds': round(time.monotonic() - started, 2), 'workspace': str(workspace)}
        print(json.dumps(record), flush=True)
        return record

    with ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(run, [(profile, case) for profile in profiles for case in cases]))
    dump(destination / 'campaign.json', {'cases': records, 'passed': sum(item['passed'] for item in records), 'total': len(records)})
    return all(item['passed'] for item in records)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--child', choices=CASES)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profiles', nargs='+', default=['powerful', 'responses_powerful'])
    parser.add_argument('--cases', nargs='+', choices=CASES, default=list(CASES))
    parser.add_argument('--deadline', type=int, default=180)
    args = parser.parse_args()
    if args.child:
        child(args.child, args.output.resolve(), args.profiles[0])
    else:
        sys.exit(0 if campaign(args.output.resolve(), args.cases, args.profiles, args.deadline) else 1)
