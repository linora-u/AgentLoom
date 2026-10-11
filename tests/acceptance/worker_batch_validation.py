"""Opt-in isolated CLI campaign for the finite Worker map, with real Providers."""
from __future__ import annotations

import argparse
import importlib.metadata
import io
import json
import os
import platform
import signal
import subprocess
import sys
import time
from contextlib import nullcontext, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def versions():
    import agentloom
    from agentloom.runtimes.pi.metadata import SDK_VERSION
    return {'python': sys.version, 'platform': platform.platform(), 'source': agentloom.__file__,
        'dependencies': {name: importlib.metadata.version(name)
            for name in ('smolagents', 'litellm', 'httpx', 'openai', 'mcp', 'pytest')},
        'node': subprocess.check_output(['node', '--version'], text=True).strip(),
        'node_abi': subprocess.check_output(['node', '-p', 'process.versions.modules'], text=True).strip(),
        'pi_sdk': SDK_VERSION}


def pi_secrets():
    path = Path(os.environ.get('PI_CODING_AGENT_DIR') or Path.home() / '.pi/agent') / 'auth.json'
    if not path.is_file():
        return []
    values = []
    def walk(value):
        if isinstance(value, dict):
            for key, child_value in value.items():
                if key.lower() in {'access', 'refresh', 'api_key', 'token'} and isinstance(child_value, str):
                    values.append(child_value)
                else:
                    walk(child_value)
        elif isinstance(value, list):
            for child_value in value:
                walk(child_value)
    walk(json.loads(path.read_text()))
    return values


def live_settings(profile, model_source):
    from agentloom.config.config import build_effective_agent_config_snapshot, load_project_config
    snapshot = build_effective_agent_config_snapshot(
        {'_yaml_file_path': str(model_source / 'applications/news_agent/workflows/initial.yaml')},
        base_config=load_project_config(model_source))
    settings = snapshot.llm.for_type(profile)
    values = {**settings.model_dump(exclude={'extra_completion_params', 'input_token_limit'}),
        **settings.extra_completion_params, 'timeout': 45, 'num_retries': 0, 'max_output_tokens': 2048}
    return values, [settings.api_key] if settings.api_key else []


def child(root: Path, runtime: str, profile: str, *, url=None, route=False, items=2, concurrency=2, model_source=ROOT):
    from agentloom.__main__ import main
    from agentloom.app.run import RunInfo
    from agentloom.config.config import bind_config, load_project_config
    from agentloom.execution import RuntimeHome

    from tests.acceptance.worker_batch_support import (
        FixtureState,
        capture_bridges,
        configure,
        prepare,
        scan_secrets,
        verify_run,
    )
    if root.exists():
        raise FileExistsError(f'preserve earlier attempt: {root}')
    root.mkdir(parents=True, mode=0o700)
    settings, secrets = (None, ['fixture-key']) if url else live_settings(profile, model_source)
    if not url and runtime == 'pi':
        secrets.extend(pi_secrets())
    workflow = prepare(root, runtime, url=url, profile=profile, model_settings=settings, count=items)
    (root / 'config').chmod(0o700)
    state = FixtureState(root, count=items, capacities=(concurrency,))
    if route:
        from agentloom_ark_route import bind_ark_interface_route
        routing = bind_ark_interface_route()
    else:
        routing = nullcontext(None)
    command = ['run', str(workflow), '--no-file-log', '--output-format', 'jsonl']
    attempt = {'runtime': runtime, 'profile': profile, 'provider': 'fixture' if url else 'real',
        'entry': 'agentloom.__main__.main', 'cli_arguments': command,
        'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()),
        'started_at': datetime.now(UTC).isoformat(), 'versions': versions()}
    (root / 'attempt.json').write_text(json.dumps(attempt, indent=2))
    output = io.StringIO()
    with routing as route_evidence, capture_bridges(root), configure(state), bind_config(load_project_config(root)):
        with redirect_stdout(output):
            main(command, standalone_mode=False)
    stdout = output.getvalue()
    (root / 'cli.stdout').write_text(stdout)
    events = [json.loads(line) for line in stdout.splitlines()]
    assert [e['event'] for e in events] == ['run.started', 'run.completed']
    terminal = events[-1]
    run = terminal['run']
    context = RuntimeHome(root / 'runtime').context(application_id=run['application_id'],
        task_id=run['task_id'], run_id=run['run_id'])
    info = RunInfo(application_id=run['application_id'], task_id=run['task_id'], run_id=run['run_id'],
        run_dir=Path(run['run_dir']), manifest_path=Path(run['manifest_path']),
        log_path=Path(run['log_path']) if run['log_path'] else None, trace_dir=context.trace_dir)
    proof = verify_run(SimpleNamespace(run=info, output=terminal['output']), state)
    answer = json.loads(terminal['output'])
    assert answer['batch_ids'] == [b['batch_id'] for b in proof['batches']]
    assert answer['run_id'] == run['run_id']
    assert answer['reduction'] == proof['reduction']
    if not url and runtime == 'pi':
        secrets.extend(pi_secrets())
    secret_proof = scan_secrets(root, secrets)
    report = {**attempt, 'status': 'passed', 'route': route_evidence,
        'proof': proof, 'secret_scan': secret_proof}
    (root / 'report.json').write_text(json.dumps(report, indent=2))
    print(stdout, end='')


def campaign(args):
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    reports = []
    for runtime in args.runtimes:
        root = args.output / runtime
        command = [sys.executable, str(Path(__file__).resolve()), '--child', '--runtimes', runtime,
            '--profile', args.profile, '--items', str(args.items), '--concurrency', str(args.concurrency),
            '--output', str(root)]
        command.extend(['--model-source', str(args.model_source)])
        if args.ark_interface_route:
            command.append('--ark-interface-route')
        started = time.monotonic()
        with (args.output / f'{runtime}.stdout').open('w') as stdout, (args.output / f'{runtime}.stderr').open('w') as stderr:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr, cwd=ROOT, start_new_session=True)
            timed_out = False
            try:
                process.wait(timeout=args.deadline)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.kill(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        passed = process.returncode == 0 and (root / 'report.json').is_file()
        record = {'runtime': runtime, 'command': command, 'exit_code': process.returncode,
            'timed_out': timed_out, 'passed': passed, 'elapsed_seconds': time.monotonic() - started}
        if passed:
            import psutil
            proof = json.loads((root / 'report.json').read_text())['proof']['resource_exit']
            assert not any(psutil.pid_exists(pid) for pid in proof['pids']), 'child left owned process alive'
        reports.append(record)
    (args.output / 'campaign.json').write_text(json.dumps(reports, indent=2))
    from tests.acceptance.worker_batch_support import scan_secrets
    _settings, secrets = live_settings(args.profile, args.model_source.resolve())
    if 'pi' in args.runtimes:
        secrets.extend(pi_secrets())
    scan = scan_secrets(args.output, secrets)
    (args.output / 'secret-scan.json').write_text(json.dumps(scan, indent=2))
    print(json.dumps(reports, indent=2))
    return all(record['passed'] for record in reports)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--runtimes', nargs='+', choices=('pi', 'smolagents'), required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--items', type=int, choices=(2, 4), default=2)
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--deadline', type=int, default=180)
    parser.add_argument('--ark-interface-route', action='store_true')
    parser.add_argument('--fixture-url')
    parser.add_argument('--model-source', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.child:
        child(args.output, args.runtimes[0], args.profile, url=args.fixture_url,
            route=args.ark_interface_route, items=args.items, concurrency=args.concurrency,
            model_source=args.model_source.resolve())
    else:
        if args.fixture_url:
            parser.error('--fixture-url is for isolated --child tests')
        raise SystemExit(0 if campaign(args) else 1)


if __name__ == '__main__':
    main()
