"""Crash the real SDK on either side of the native durable acknowledgement."""
import json
import os
from pathlib import Path
import sys

import pytest

from tests.pi_test.test_application import model_service, project
from tests.pi_test.test_tools_application import select
from tests.pi_test.test_process_lifecycle import sdk_node, start_cli, assert_gone


@pytest.mark.parametrize('boundary', ['before_settle', 'after_commit'])
def test_native_sdk_death_preserves_exact_durable_state(tmp_path, boundary):
    (tmp_path / 'note.txt').write_text('Durable native evidence 6729')
    binary = sdk_node()
    marker = tmp_path / 'native-boundary.json'
    launcher = tmp_path / 'bin/node'
    launcher.parent.mkdir()
    launcher.write_text(f'''#!{sys.executable}
import subprocess,sys,threading,json,os
from pathlib import Path
if sys.argv[1:]==['--version']:os.execv({binary!r},[{binary!r},'--version'])
p=subprocess.Popen([{binary!r},*sys.argv[1:]],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
def terminate(value):
 Path({str(marker)!r}).write_text(json.dumps({{'pid':p.pid,'frame':value}}))
 p.kill()
def forward():
 try:
  for line in sys.stdin.buffer:
   value=json.loads(line)
   if {boundary!r}=='after_commit' and value.get('kind')=='response' and (value.get('payload') or {{}}).get('method')=='tool_settle':
    terminate(value);return
   p.stdin.write(line);p.stdin.flush()
 except BrokenPipeError:pass
threading.Thread(target=forward,daemon=True).start()
for line in p.stdout:
 value=json.loads(line)
 if {boundary!r}=='before_settle' and value.get('kind')=='request' and value['payload']['method']=='tool_settle':
  terminate(value);break
 sys.stdout.buffer.write(line);sys.stdout.buffer.flush()
p.wait()
''')
    launcher.chmod(0o755)
    with model_service(turns=[[('durable-read', 'read', {'path': 'note.txt'})]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{'name': 'read'}])
        env = {**os.environ, 'PATH': str(launcher.parent) + os.pathsep + os.environ['PATH']}
        child = start_cli(tmp_path, app, env)
        try:
            stdout, stderr = child.communicate(timeout=12)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
    assert child.returncode != 0
    events = [json.loads(line) for line in stdout.splitlines()]
    assert events[-1]['event'] == 'run.failed'
    assert len(requests) == 1
    intercepted = json.loads(marker.read_text())
    assert_gone(intercepted['pid'])
    entries = [json.loads(path.read_text()) for path in tmp_path.rglob('native-tools/**/*.json')]
    assert len(entries) == 1
    if boundary == 'before_settle':
        assert entries[0]['state'] == 'uncertain'
        assert 'record' not in entries[0]
    else:
        ack = intercepted['frame']['payload']
        assert ack['state'] == entries[0]['state'] == 'committed'
        assert ack['commit_id'] == entries[0]['commit_id']
        assert ack['record'] == entries[0]['record']
        assert 'Durable native evidence 6729' in json.dumps(entries[0]['record']['output'])
