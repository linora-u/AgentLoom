"""Fault injection at the OS process boundary, through the real CLI/Application."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from threading import Event
import time

import pytest

from tests.pi_test.test_application import model_service, project


def start_cli(root: Path, app: Path, env=None):
    code = (
        "from pathlib import Path\nfrom agentloom.configuration.config import bind_config,load_project_config\n"
        "from agentloom.__main__ import main\n"
        "import sys\nwith bind_config(load_project_config(Path(sys.argv[1]))):\n"
        " main(['run',sys.argv[2],'--no-file-log','--output-format','jsonl'])\n"
    )
    return subprocess.Popen([sys.executable, "-c", code, str(root), str(app)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)


def node_launcher(root: Path):
    """Record the actual managed bridge PID without replacing its SDK or protocol."""
    binary = shutil.which("node")
    assert binary
    launcher = root / "bin/node"
    launcher.parent.mkdir()
    marker = root / "bridge.pid"
    launcher.write_text(f'#!/bin/sh\necho $$ > "{marker}"\nexec "{binary}" "$@"\n')
    launcher.chmod(0o755)
    return {**os.environ, "PATH": str(launcher.parent) + os.pathsep + os.environ["PATH"]}, marker


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


def test_protocol_corruption_fails_application_and_reaps_real_bridge(tmp_path):
    """The OS shim corrupts a real SDK event; it never manufactures model responses."""
    with model_service() as (url, requests):
        app = project(tmp_path, url)
        binary = shutil.which("node")
        launcher = tmp_path / "bin/node"
        launcher.parent.mkdir()
        marker = tmp_path / "bridge.pid"
        launcher.write_text(f'''#!{sys.executable}
import subprocess,sys,threading,json
from pathlib import Path
p=subprocess.Popen([{binary!r},*sys.argv[1:]],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
Path({str(marker)!r}).write_text(str(p.pid))
def forward():
 try:
  for line in sys.stdin.buffer:
   p.stdin.write(line);p.stdin.flush()
 except (BrokenPipeError,ValueError):pass
threading.Thread(target=forward,daemon=True).start()
for line in p.stdout:
 value=json.loads(line)
 if value.get('kind')=='event':
  sys.stdout.write('{{malformed frame}}\\n');sys.stdout.flush()
  p.kill();p.wait();sys.exit(2)
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
