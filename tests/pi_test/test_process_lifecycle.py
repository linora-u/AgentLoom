"""Fault injection at the OS process boundary, through the real CLI/Application."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from tests.pi_test.test_application import model_service, project


class CapturedCli(subprocess.Popen):
    """Capture before fault injection without waiting on an undrained pipe."""
    def __init__(self, command, root, env):
        prefix = root / f"cli-{uuid4().hex}"
        self.output_path = prefix.with_suffix(".stdout")
        self.error_path = prefix.with_suffix(".stderr")
        with self.output_path.open("w") as stdout, self.error_path.open("w") as stderr:
            super().__init__(command, stdout=stdout, stderr=stderr, text=True, env=env, start_new_session=True)

    def communicate(self, input=None, timeout=None):
        assert input is None
        super().communicate(timeout=timeout)
        return self.output_path.read_text(), self.error_path.read_text()


def start_cli(root: Path, app: Path, env=None, *, resume=None):
    code = (
        "from pathlib import Path\nfrom agentloom.config.config import bind_config,load_project_config\n"
        "from agentloom.__main__ import main\n"
        "import sys\nwith bind_config(load_project_config(Path(sys.argv[1]))):\n"
        " main(['run',sys.argv[2],'--no-file-log','--output-format','jsonl',*sys.argv[3:]])\n"
    )
    return CapturedCli([sys.executable, "-c", code, str(root), str(app),
                        *(['--resume', resume] if resume else [])], root, env)


def node_launcher(root: Path):
    """Record the actual managed bridge PID without replacing its SDK or protocol."""
    binary = sdk_node()
    launcher = root / "bin/node"
    launcher.parent.mkdir()
    marker = root / "bridge.pid"
    launcher.write_text(f'#!/bin/sh\necho $$ > "{marker}"\nexec "{binary}" "$@"\n')
    launcher.chmod(0o755)
    return {**os.environ, "PATH": str(launcher.parent) + os.pathsep + os.environ["PATH"]}, marker


def sdk_node():
    """Select an installed SDK-compatible binary for the external fault shim."""
    from agentloom.runtimes.pi.environment import build_pi_subprocess_env
    from agentloom.runtimes.pi.install import find_node

    return find_node(build_pi_subprocess_env())


def until(predicate, *, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for process boundary")
        time.sleep(0.02)


def assert_gone(pid):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_real_cli_stdout_contains_only_application_events(tmp_path):
    with model_service() as (url, _):
        app = project(tmp_path, url)
        env, marker = node_launcher(tmp_path)
        child = start_cli(tmp_path, app, env)
        stdout, stderr = child.communicate(timeout=20)
    assert child.returncode == 0, stderr
    records = [json.loads(line) for line in stdout.splitlines()]
    assert [r["event"] for r in records] == ["run.started", "run.completed"]
    assert "host:" not in stdout and "fixture-secret" not in stdout + stderr
    assert_gone(int(marker.read_text()))


@pytest.mark.parametrize("proxy_value,ca_value", [
    (None, None), ("0", ""), ("1", "system"),
])
def test_real_bridge_preserves_configured_or_default_proxy_ca(tmp_path, monkeypatch, proxy_value, ca_value):
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        if ca_value == "system":
            import certifi
            ca_value = certifi.where()
        if ca_value:
            assert Path(ca_value).is_file()
        monkeypatch.setenv("HTTPS_PROXY", os.environ.get("HTTPS_PROXY", "http://127.0.0.1:9"))
        expected = {"NODE_USE_ENV_PROXY": "1"} if proxy_value is None else {}
        for name, value in (("NODE_USE_ENV_PROXY", proxy_value), ("NODE_EXTRA_CA_CERTS", ca_value)):
            monkeypatch.delenv(name, raising=False)
            if value is not None:
                monkeypatch.setenv(name, value)
                expected[name] = value
        # Select the launcher target after applying the case's proxy policy.
        binary = sdk_node()
        launcher = tmp_path / "bin/node"
        launcher.parent.mkdir()
        marker = tmp_path / "bridge-env.json"
        launcher.write_text(f'''#!{sys.executable}
import json,os,sys
from pathlib import Path
if sys.argv[1:] != ['--version'] and sys.argv[1:3] != ['-p','process.versions.modules']:
 Path({str(marker)!r}).write_text(json.dumps({{key:value for key,value in os.environ.items() if key.startswith(('PI_','NODE_'))}}))
os.execv({binary!r},[{binary!r},*sys.argv[1:]])
''')
        launcher.chmod(0o755)
        env = {**os.environ, "PATH": str(launcher.parent) + os.pathsep + os.environ["PATH"],
               "NODE_OPTIONS": "--max-old-space-size=256", "NODE_PATH": "/ignored/node/path",
               "NODE_TLS_REJECT_UNAUTHORIZED": "0", "PI_TEST_SETTING": "must-be-removed",
               "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", "http://127.0.0.1:9"),
               "NO_PROXY": "localhost,127.0.0.1"}
        child = start_cli(tmp_path, app, env)
        stdout, stderr = child.communicate(timeout=20)
    assert child.returncode == 0, stderr
    assert requests
    assert marker.exists()
    assert json.loads(marker.read_text()) == expected
    assert "fixture-secret" not in stdout + stderr


@pytest.mark.parametrize("interrupt", ["keyboard", "child_exit"])
def test_pending_model_call_terminates_and_cleans_process(tmp_path, interrupt):
    release = Event()
    with model_service(stall=release) as (url, requests):
        app = project(tmp_path, url)
        env, marker = node_launcher(tmp_path)
        child = start_cli(tmp_path, app, env)
        try:
            until(lambda: bool(requests))
            pid = int(marker.read_text())
            os.kill(child.pid if interrupt == "keyboard" else pid, signal.SIGINT if interrupt == "keyboard" else signal.SIGKILL)
            stdout, stderr = child.communicate(timeout=10)
        finally:
            release.set()
            if child.poll() is None:
                child.kill()
                child.wait()
    records = [json.loads(line) for line in stdout.splitlines()]
    assert records[-1]["event"] == ("run.interrupted" if interrupt == "keyboard" else "run.failed")
    assert child.returncode != 0
    assert_gone(pid)
    assert "fixture-secret" not in stdout + stderr


@pytest.mark.parametrize("fault", ["malformed", "sequence", "identity", "duplicate_key", "duplicate_terminal", "duplicate_callback", "duplicate_after_close"])
def test_protocol_corruption_fails_application_and_reaps_real_bridge(tmp_path, fault):
    """The OS shim corrupts a real SDK event; it never manufactures model responses."""
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        binary = sdk_node()
        launcher = tmp_path / "bin/node"
        launcher.parent.mkdir()
        marker = tmp_path / "bridge.pid"
        launcher.write_text(f'''#!{sys.executable}
import subprocess,sys,threading,json
import os
from pathlib import Path
if sys.argv[1:]==['--version'] or sys.argv[1:3]==['-p','process.versions.modules']:os.execv({binary!r},[{binary!r},*sys.argv[1:]])
p=subprocess.Popen([{binary!r},*sys.argv[1:]],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
Path({str(marker)!r}).write_text(str(p.pid))
ack=threading.Event()
def forward():
 try:
  for line in sys.stdin.buffer:
   value=json.loads(line)
   if value.get('kind')=='response' and value.get('request_id')=='pi:repeated':
    ack.set();continue
   p.stdin.write(line);p.stdin.flush()
 except (BrokenPipeError,ValueError):pass
threading.Thread(target=forward,daemon=True).start()
saved=b''
for line in p.stdout:
 value=json.loads(line)
 fault={fault!r}
 trigger=(value.get('kind')=='response' and value.get('payload',{{}}).get('method')=='run') if fault=='duplicate_terminal' else value.get('kind')=='event'
 if fault=='duplicate_after_close':
  if value.get('payload',{{}}).get('method')=='run':saved=line
  trigger=value.get('kind')=='response' and value.get('payload',{{}}).get('method')=='close'
 if trigger:
  if fault=='malformed': damaged='{{malformed frame}}\\n'
  elif fault=='duplicate_terminal': damaged=line.decode()*2
  elif fault=='duplicate_after_close': damaged=(line+saved).decode()
  elif fault=='duplicate_callback':
   value={{'version':2,'kind':'request','instance_id':value['instance_id'],'run_id':value['run_id'],'request_id':'pi:repeated',
     'payload':{{'method':'platform_invoke','identity':{{'application_id':'pi','task_id':'task','run_id':value['run_id'],'instance_id':value['instance_id'],'call_id':'call'}},'tool_name':'unavailable','arguments':{{}}}}}}
   damaged=json.dumps(value)+'\\n'
   sys.stdout.write(damaged);sys.stdout.flush()
   if not ack.wait(timeout=3):sys.exit(3)
  elif fault=='duplicate_key': damaged=line.decode().replace('"version":2','"version":2,"version":2')
  else:
   if fault=='sequence':value['sequence']+=1
   if fault=='identity':value['run_id']='wrong-run'
   damaged=json.dumps(value)+'\\n'
  sys.stdout.write(damaged);sys.stdout.flush()
  if p.poll() is None:p.kill()
  p.wait();sys.exit(2)
 sys.stdout.buffer.write(line);sys.stdout.buffer.flush()
p.wait()
''')
        launcher.chmod(0o755)
        env = {**os.environ, "PATH": str(launcher.parent) + os.pathsep + os.environ["PATH"]}
        child = start_cli(tmp_path, app, env)
        stdout, stderr = child.communicate(timeout=20)
    records = [json.loads(line) for line in stdout.splitlines()]
    assert records[-1]["event"] == "run.failed"
    assert "protocol failure" in stdout + stderr
    assert_gone(int(marker.read_text()))


def test_missing_node_is_a_clear_configuration_failure(tmp_path):
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        child = start_cli(tmp_path, app, {**os.environ, "PATH": ""})
        stdout, stderr = child.communicate(timeout=20)
    assert child.returncode != 0
    assert "Pi requires Node" in stdout + stderr
    assert not requests


def test_older_bundled_node_does_not_hide_compatible_node(tmp_path):
    with model_service() as (url, _):
        app = project(tmp_path, url)
        old = tmp_path / "old-bin/node"
        old.parent.mkdir()
        old.write_text('#!/bin/sh\necho v18.4.0\n')
        old.chmod(0o755)
        child = start_cli(tmp_path, app, {**os.environ, "PATH": str(old.parent) + os.pathsep + os.environ["PATH"]})
        stdout, stderr = child.communicate(timeout=20)
    assert child.returncode == 0, stderr
    assert json.loads(stdout.splitlines()[-1])["event"] == "run.completed"


def test_application_finalization_retries_failed_pi_directory_cleanup(tmp_path, monkeypatch):
    from tempfile import TemporaryDirectory

    from agentloom.app.run import ApplicationRunError
    from agentloom.app.runner import execute_app
    from agentloom.config.config import bind_config, load_project_config

    original_cleanup = TemporaryDirectory.cleanup
    directories = []
    attempts = {}

    def flaky_cleanup(directory):
        path = Path(directory.name)
        if path.name.startswith("agentloom-pi-"):
            attempts[path] = attempts.get(path, 0) + 1
            if path not in directories:
                directories.append(path)
                raise OSError("temporary Pi directory cleanup failure")
        return original_cleanup(directory)

    monkeypatch.setattr(TemporaryDirectory, "cleanup", flaky_cleanup)
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        with bind_config(load_project_config(tmp_path)):
            with pytest.raises(ApplicationRunError, match="temporary Pi directory cleanup failure"):
                execute_app(app, file_logging=False)
    assert len(requests) == 1
    assert directories and all(attempts[path] >= 2 and not path.exists() for path in directories)
