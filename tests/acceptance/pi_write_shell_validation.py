"""Real-model ticket 10 Applications; filesystem and journal are the outcome oracle."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.acceptance.pi_tool_validation import campaign, tool_records
from tests.acceptance.platform_tool_validation import dump

CASES = ('create', 'edit', 'overwrite', 'unread', 'excluded', 'transformed', 'stale',
         'shell_allowed', 'shell_denied', 'shell_error', 'shell_timeout', 'sandbox_required',
         'query_excluded', 'read_large', 'shell_large')


def configure(case, workspace, definition):
    target = workspace / 'target.txt'
    original = 'version = 1031\nkeep = true\n'
    tools = ['write']
    calls = [('write', {'path': str(target), 'content': 'created-live-1031'})]
    definition['shell_settings'] = {'allowed_commands': ['printf', 'cat', 'sleep', 'exit'],
                                    'allowed_operators': ['*'], 'sandbox': {'enabled': False}}
    if case in {'edit', 'overwrite', 'unread', 'excluded', 'stale'}:
        target.write_text(original)
        calls = [('write', {'path': str(target), 'content': 'replaced-live-1031'})]
        if case in {'edit', 'overwrite', 'stale'}:
            tools = ['read', 'edit' if case == 'edit' else 'write']
            calls.insert(0, ('read', {'path': str(target)}))
        if case == 'edit':
            calls[-1] = ('edit', {'path': str(target), 'edits': [{'oldText': 'version = 1031', 'newText': 'version = 2042'}]})
        if case == 'excluded':
            definition['tool_access_control'] = {'path_validation': [{'tools': ['write_file'], 'exclude_paths': [str(target)]}]}
        if case == 'stale':
            definition['hooks'] = {'PreToolUse': [{'id': 'concurrent-writer', 'matcher': 'write_file',
                'command': 'printf external-live-1031 > ' + shlex.quote(str(target))}]}
    if case == 'transformed':
        calls = [('write', {'path': str(workspace / 'unapproved.txt'), 'content': 'raw-input'})]
        effect = {'decision': 'modify', 'modified_input': {'path': str(target), 'content': 'approved-live-1031'}}
        definition['hooks'] = {'PreToolUse': [{'id': 'final-arguments', 'matcher': 'write_file',
            'command': "printf '%s' " + shlex.quote(json.dumps(effect))}]}
    if case.startswith('shell_') or case in {'sandbox_required', 'query_excluded'}:
        tools = ['bash']
        command = {
            'shell_allowed': 'printf shell-live-1031 > shell.txt',
            'shell_denied': 'touch forbidden.txt',
            'shell_error': 'printf partial-live-1031; exit 7',
            'shell_timeout': 'sleep 3',
            'sandbox_required': 'printf forbidden > forbidden.txt',
            'query_excluded': 'cat target.txt',
            'shell_large': '',
        }[case]
        calls = [('bash', {'command': command})]
        if case == 'shell_timeout': calls[0][1]['timeout'] = 0.1
        if case == 'sandbox_required': definition['shell_settings']['sandbox'] = {'enabled': True, 'mode': 'bwrap'}
        if case == 'query_excluded':
            target.write_text('DENIED-QUERY-LIVE-1031')
            definition['tool_access_control'] = {'path_validation': [{'tools': ['shell_tool'], 'exclude_paths': [str(target)]}]}
    if case == 'read_large':
        lines = [f'line-{i:04}' for i in range(5500)]
        lines[3800] = 'TARGET_RECORD: ORIGINAL-LIVE-1031'
        lines[5300] = 'OUTSIDE-QUERY-LIVE-SECRET'
        target.write_text('\n'.join(lines))
        tools = ['read', 'loom_retrieve_context']
        calls = [('read', {'path': str(target), 'offset': 1001, 'limit': 3000})]
    if case == 'shell_large':
        producer = workspace / 'producer.py'
        producer.write_text("from pathlib import Path\n"
            f"p=Path({str(workspace / 'executions.txt')!r});p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
            "print('prefix-line\\n'*500)\nprint('TARGET_RECORD: ORIGINAL-LIVE-1031')\nprint('suffix-line\\n'*5000)\n")
        definition['shell_settings']['allowed_commands'] = ['*']
        tools = ['bash', 'loom_retrieve_context']
        calls = [('bash', {'command': f'{shlex.quote(sys.executable)} {shlex.quote(str(producer))}', 'timeout': 10})]
    # Keep real-model validation inside its own disposable workspace. Relative
    # arguments also avoid mistyping a long machine-specific absolute prefix.
    rules = definition.setdefault('tool_access_control', {}).setdefault('path_validation', [])
    rules.append({'tools': ['read_file', 'write_file', 'edit_file'], 'include_paths': [str(workspace)]})
    for name, arguments in calls:
        if name in {'read', 'write', 'edit'}:
            arguments['path'] = str(Path(arguments['path']).relative_to(workspace))
    task = 'Call the following tools in order, each exactly once. Use the exact relative paths shown, relative to the current workspace. Wait for each result before the next call; do not batch dependent calls.\n'
    task += '\n'.join(f'{name}({json.dumps(arguments)})' for name, arguments in calls)
    task += '\nHooks may intentionally rewrite the destination or content. A successful tool receipt is final; do not repeat or repair that call even if the receipt differs from the requested arguments.'
    task += '\nIf blocked or errored, report that result accurately and stop; do not retry, read extra files, or use alternative commands.'
    if case in {'read_large', 'shell_large'}:
        definition['context_engine'] = {'min_chars': 1000, 'preview_max_chars': 300}
        task += '\nAfter this call, use loom_retrieve_context with the returned ContextRef, query="TARGET_RECORD", offset=0, limit=5. Report the original verification token; do not run the source tool again.'
    definition.update(tools=[{'name': name} for name in tools], description='Verify one controlled Pi tool scenario.', workflow=task)
    return target, original


def verify(case, workspace, target, original, records, entries, failed):
    blocked = {'unread', 'excluded', 'stale', 'shell_denied', 'sandbox_required', 'query_excluded'}
    if case == 'shell_timeout':
        assert failed and len(entries) == 1 and entries[0]['state'] == 'uncertain'
        assert 'record' not in entries[0]
        return
    assert not failed
    native = [r for r in records if r['tool_name'] in {'read', 'edit', 'write', 'bash'}]
    assert native, 'The real model did not invoke the requested native tool'
    for record in native:
        if record['tool_name'] in {'read', 'write', 'edit'}:
            assert (workspace / record['input']['path']).resolve() == target.resolve(), 'Model invoked the wrong file'
    if case in blocked:
        assert native[-1]['status'] == 'blocked'
        assert native[-1]['output'] is None
        if case in {'unread', 'excluded'}: assert target.read_text() == original
        if case == 'stale': assert target.read_text() == 'external-live-1031'
        assert not (workspace / 'forbidden.txt').exists()
        if case == 'query_excluded': assert 'DENIED-QUERY-LIVE-1031' not in json.dumps(records)
        return
    if case == 'shell_error':
        assert native[-1]['status'] == 'error' and native[-1]['output'] is None
        assert entries[-1]['raw_output'] == 'partial-live-1031'
        assert entries[-1]['result_scope']['source_completeness'] == 'partial'
        return
    assert all(r['status'] == 'completed' for r in native)
    for record in native:
        metadata = record['metadata']['native']
        assert metadata['provider'] == 'pi' and metadata['identity']['call_id'] == record['call_id']
        assert metadata['result_scope']['source_completeness'] == ('unknown' if record['tool_name'] == 'bash' else 'complete')
    if case == 'create': assert target.read_text() == 'created-live-1031'
    if case == 'edit': assert target.read_text() == 'version = 2042\nkeep = true\n'
    if case == 'overwrite': assert target.read_text() == 'replaced-live-1031'
    if case in {'edit', 'overwrite'}:
        assert any(p.read_bytes() == original.encode() for p in (workspace / 'runtime').rglob('native-file-history/**/*') if p.is_file())
    if case == 'transformed':
        assert target.read_text() == 'approved-live-1031'
        assert not (workspace / 'unapproved.txt').exists()
        assert native[-1]['input'] == {'path': str(target), 'content': 'approved-live-1031'}
    if case == 'shell_allowed': assert (workspace / 'shell.txt').read_text() == 'shell-live-1031'
    if case in {'read_large', 'shell_large'}:
        assert len(native) == 1
        retrieved = [r for r in records if r['tool_name'] == 'loom_retrieve_context' and r['status'] == 'completed']
        assert any('ORIGINAL-LIVE-1031' in str(r['output']) for r in retrieved)
        scope = native[0]['metadata']['native']['result_scope']
        assert scope['display_truncated'] and scope['coverage'] == ('captured_stream' if case == 'shell_large' else 'complete_query')
        if case == 'read_large':
            raw = json.loads(Path(scope['raw_artifact']['path']).read_text())['raw_output']
            assert 'OUTSIDE-QUERY-LIVE-SECRET' not in raw
            assert raw == '\n'.join(target.read_text().split('\n')[1000:4000])
        else: assert (workspace / 'executions.txt').read_text() == 'x'


def child(case, workspace, profile):
    workspace.mkdir(parents=True, mode=0o700, exist_ok=False)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip())
    dump(workspace / 'attempt.json', {'case': case, 'profile': profile, 'revision': revision, 'dirty': dirty, 'started_at': datetime.now(UTC).isoformat()})
    config = workspace / 'config'; config.mkdir(mode=0o700)
    shutil.copyfile(ROOT / 'config/llm.yaml', config / 'llm.yaml'); (config / 'llm.yaml').chmod(0o600)
    system = {'runtime': {'root_dir': str(workspace / 'runtime')}, 'checkpoint': {'enabled': False},
              'logging': {'console_enabled': False}, 'self_learning': {'enabled': False}, 'default_toolsets': []}
    definition = {'name': f'pi_{case}', 'agent_runtime': 'pi', 'model_type': profile, 'toolsets': []}
    target, original = configure(case, workspace, definition)
    (config / 'system.yaml').write_text(yaml.safe_dump(system))
    app = workspace / 'applications/validation/workflows/root.yaml'; app.parent.mkdir(parents=True)
    app.write_text(yaml.safe_dump(definition))
    from agentloom.app.runner import execute_app
    from agentloom.app.run import ApplicationRunError
    from agentloom.config.config import bind_config, load_project_config
    failed = False
    result = None
    with bind_config(load_project_config(workspace)):
        try: result = execute_app(app, file_logging=True)
        except ApplicationRunError:
            failed = True
    records = tool_records(workspace / 'runtime')
    entries = [json.loads(p.read_text()) for p in (workspace / 'runtime').rglob('native-tools/**/*.json')]
    dump(workspace / 'tool-records.json', records)
    verify(case, workspace, target, original, records, entries, failed)
    dump(workspace / 'report.json', {'case': case, 'profile': profile, 'status': 'passed', 'revision': revision,
        'dirty': dirty, 'runtime': 'pi', 'provider': 'real', 'sdk': '0.79.4', 'application_failed_as_expected': failed,
        'run_id': result.run.run_id if result else entries[0]['request']['identity']['run_id'],
        'journal_states': [e.get('state', 'rejected') for e in entries], 'native_records': len(entries)})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--child', choices=CASES)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profiles', nargs='+', default=['powerful', 'responses_powerful'])
    parser.add_argument('--cases', nargs='+', choices=CASES, default=list(CASES))
    parser.add_argument('--deadline', type=int, default=180)
    args = parser.parse_args()
    if args.child: child(args.child, args.output.resolve(), args.profiles[0])
    else: sys.exit(0 if campaign(args.output.resolve(), args.cases, args.profiles, args.deadline, script=Path(__file__)) else 1)
