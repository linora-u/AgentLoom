"""Real smol Application with a local model binding, in an isolated scope."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="managed process groups require POSIX")


def test_managed_smol_background_joins_scope_and_is_reaped(tmp_path):
    root = Path(__file__).resolve().parents[2]
    code = '''
import json,os,sys
from pathlib import Path
import pytest
from agentloom.execution.process_scope import bind_managed_process_scope
from tests.application_test import test_smol_compatibility_application as acceptance
case = Path(sys.argv[1])
original = acceptance._CompatibilityProvider.turn
evidence = {"root_pid":os.getpid(),"root_group":os.getpgrp(),"models_requested":0}
def turn(provider,request):
    result = original(provider,request)
    if provider.background_tasks:
        task = provider.background_tasks[0]
        evidence.update(background_pid=task.pid,background_group=os.getpgid(task.pid))
        (case/"scope.json").write_text(json.dumps(evidence))
    return result
patch=pytest.MonkeyPatch()
patch.setattr(acceptance._CompatibilityProvider,"turn",turn)
try:
    with bind_managed_process_scope():
        acceptance.test_smol_runtime_options_execute_tools_todo_and_goal_through_application(case,patch,False,True)
finally:
    patch.undo()
'''
    child = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=root,
                           capture_output=True, text=True, timeout=30, start_new_session=True)
    (tmp_path / "stdout.log").write_text(child.stdout)
    (tmp_path / "stderr.log").write_text(child.stderr)
    assert child.returncode == 0, child.stdout + child.stderr
    evidence = json.loads((tmp_path / "scope.json").read_text())
    assert evidence["root_group"] == evidence["root_pid"]
    assert evidence["background_group"] == evidence["root_group"]


@pytest.mark.parametrize("mode", ["foreground", "background", "timeout", "promotion", "cleanup-failure"])
def test_formal_smol_cli_preserves_shell_modes_and_reports_cleanup_failure(tmp_path, mode):
    root = Path(__file__).resolve().parents[2]
    child = subprocess.run([sys.executable, "-m", "test_support.managed_smol_cli", str(tmp_path), mode],
                           cwd=root, capture_output=True, text=True, timeout=30, start_new_session=True)
    (tmp_path / "stdout.log").write_text(child.stdout)
    (tmp_path / "stderr.log").write_text(child.stderr)
    evidence = json.loads((tmp_path / "evidence.json").read_text())
    assert "native" in evidence, child.stdout + child.stderr
    assert evidence["native"]["group"] == evidence["root_group"] == evidence["root_pid"]
    assert evidence["native_alive_after_cli"] is False
    events = [json.loads(line) for line in child.stdout.splitlines()]
    if mode == "cleanup-failure":
        assert child.returncode != 0
        assert events[-1]["event"] == "run.failed"
        assert "injected-background-close-failure" in " ".join(evidence["runner_error_causes"])
    else:
        assert child.returncode == 0, child.stdout + child.stderr
        assert events[-1]["event"] == "run.completed"
        assert "smol-scope-complete" in child.stdout
    if mode in {"background", "promotion", "cleanup-failure"}:
        assert len(evidence["background_tasks"]) == 1
        assert evidence["native_alive_before_final"] is True
    else:
        assert evidence["background_tasks"] == []
        assert evidence["native_alive_before_final"] is False


def test_watchdog_failure_reaches_original_resource_owner_after_successful_retry(tmp_path):
    root = Path(__file__).resolve().parents[2]
    code = '''
import os,subprocess,sys,tempfile,threading
from pathlib import Path
from agentloom.execution import RuntimeHome,bind_run_context
from agentloom.execution.resources import bind_resource_scope,close_run_resources
from agentloom.execution.process_scope import bind_managed_process_scope
from agentloom.execution import process as processes
from agentloom.runtimes.smolagents.tools.shell.tree_kill import SizeWatchdog
context = RuntimeHome(Path(sys.argv[1])/"runtime").context(application_id="watchdog",task_id="task",run_id="run")
original = processes.terminate_process_tree
failure = OSError("original-watchdog-cleanup-error")
notified = threading.Event()
calls = 0
def terminate(*args):
    global calls
    calls += 1
    if calls == 1:
        raise failure
    return original(*args)
processes.terminate_process_tree = terminate
def thread_error(args):
    assert args.exc_value is failure
    notified.set()
threading.excepthook = thread_error
def contains_original(error):
    return error is failure or any(contains_original(child) for child in getattr(error,"exceptions",()))
with bind_managed_process_scope(),bind_run_context(context),bind_resource_scope(context.runtime_key),tempfile.TemporaryFile() as output:
    output.write(b"over-limit")
    output.flush()
    proc,close = processes.spawn_registered_process([sys.executable,"-c","import time;time.sleep(60)"],env=dict(os.environ),
        stdin=subprocess.DEVNULL,stdout=output.fileno(),stderr=output.fileno())
    watchdog = SizeWatchdog(proc.pid,"ignored",max_bytes=1,output_fd=output.fileno(),close_process=close)
    try:
        watchdog.start()
        assert notified.wait(5),"watchdog failed to exercise the real closer"
        assert proc.poll() is None
        try:
            close_run_resources()
        except BaseException as error:
            assert contains_original(error),"original cleanup failure was replaced"
        else:
            raise AssertionError("owner silently accepted the successful physical retry")
        assert proc.poll() is not None
        close_run_resources()
        assert calls == 2
    finally:
        watchdog.stop()
        close()
'''
    child = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=root,
                           capture_output=True, text=True, timeout=20, start_new_session=True)
    (tmp_path / "stdout.log").write_text(child.stdout)
    (tmp_path / "stderr.log").write_text(child.stderr)
    assert child.returncode == 0, child.stdout + child.stderr
