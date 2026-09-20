"""OS faults around real Pi side effects and the durable host acknowledgement."""
import json
import os
from pathlib import Path
import signal
import sys

import pytest

from tests.pi_test.test_application import model_service, project
from tests.pi_test.test_tools_application import select
from tests.pi_test.test_process_lifecycle import sdk_node, start_cli, assert_gone


@pytest.mark.parametrize('tool', ['write', 'bash'])
@pytest.mark.parametrize('boundary', ['before_dispatch', 'before_settle', 'after_commit', 'missing_capture', 'omitted_capture', 'null_capture'])
def test_sdk_side_effect_and_journal_agree_after_fault(tmp_path, tool, boundary):
    target = tmp_path / 'effect.txt'
    binary = sdk_node()
    marker = tmp_path / 'fault.json'
    launcher = tmp_path / 'bin/node'
    launcher.parent.mkdir()
    launcher.write_text(f'''#!{sys.executable}
import subprocess,sys,threading,json,os
from pathlib import Path
if sys.argv[1:]==['--version']:os.execv({binary!r},[{binary!r},'--version'])
p=subprocess.Popen([{binary!r},*sys.argv[1:]],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
def stop(value):
 Path({str(marker)!r}).write_text(json.dumps({{'pid':p.pid,'frame':value}}));p.kill()
def forward():
 try:
  for line in sys.stdin.buffer:
   value=json.loads(line)
   if {boundary!r}=='after_commit' and value.get('kind')=='response' and (value.get('payload') or {{}}).get('method')=='tool_settle':
    stop(value);return
   p.stdin.write(line);p.stdin.flush()
 except BrokenPipeError:pass
threading.Thread(target=forward,daemon=True).start()
for line in p.stdout:
 value=json.loads(line);method=(value.get('payload') or {{}}).get('method')
 if value.get('kind')=='request':
  if ({boundary!r}=='before_dispatch' and method=='tool_dispatch') or ({boundary!r}=='before_settle' and method=='tool_settle'):
   stop(value);break
  if {boundary!r}=='missing_capture' and method=='tool_settle':
   Path(sys.argv[-1], 'capture-'+value['payload']['outcome']['authorization_id']+'.json').unlink()
   Path({str(marker)!r}).write_text(json.dumps({{'pid':p.pid,'frame':value}}))
  if {boundary!r} in ('omitted_capture','null_capture') and method=='tool_settle':
   if {boundary!r}=='omitted_capture':value['payload'].pop('capture')
   else:value['payload']['capture']=None
   line=(json.dumps(value)+'\\n').encode()
   Path({str(marker)!r}).write_text(json.dumps({{'pid':p.pid,'frame':value}}))
 sys.stdout.buffer.write(line);sys.stdout.buffer.flush()
p.wait()
os._exit(p.returncode if p.returncode >= 0 else 1)
''')
    launcher.chmod(0o755)
    args = {'path': str(target), 'content': 'one-side-effect-1031'} if tool == 'write' else {'command': "printf 'one-side-effect-1031' > effect.txt"}
    with model_service(turns=[[('side-effect', tool, args)]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': tool}], shell_settings={'allowed_commands': ['printf'], 'allowed_operators': ['*'], 'sandbox': {'enabled': False}})
        env = {**os.environ, 'PATH': str(launcher.parent) + os.pathsep + os.environ['PATH']}
        child = start_cli(tmp_path, app, env)
        try:
            stdout, stderr = child.communicate(timeout=20)
        finally:
            if child.poll() is None: child.kill(); child.wait()
        assert child.returncode != 0
        events = [json.loads(line) for line in stdout.splitlines()]
        assert events[-1]['event'] == 'run.failed'
        assert len(requests) == 1
    intercepted = json.loads(marker.read_text())
    assert_gone(intercepted['pid'])
    entries = [json.loads(path.read_text()) for path in tmp_path.rglob('native-tools/**/*.json')]
    assert len(entries) == 1
    entry = entries[0]
    if boundary == 'before_dispatch':
        assert not target.exists()
        assert entry['state'] == 'cancelled'
        assert 'record' not in entry
    else:
        assert target.read_text() == 'one-side-effect-1031'
        if boundary == 'after_commit':
            ack = intercepted['frame']['payload']
            assert entry['state'] == ack['state'] == 'committed'
            assert entry['record'] == ack['record']
            assert entry['commit_id'] == ack['commit_id']
            assert entry['record']['metadata']['native']['result_scope']['source_completeness'] == ('unknown' if tool == 'bash' else 'complete')
        else:
            assert entry['state'] == 'uncertain'
            assert 'record' not in entry
