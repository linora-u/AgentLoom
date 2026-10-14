"""Native lifecycle through public Application calls; no foreign runtime owner."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextvars import copy_context
from pathlib import Path
from threading import Event, Thread

import psutil
import pytest

from test_support.worker_batch import batch_project as _batch_project

batch_project = _batch_project

pytestmark = pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="managed POSIX execution")


def _alive(identity):
    try:
        process = psutil.Process(identity["pid"])
        return process.create_time() == identity["creation"] and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _marker(path, host, identities):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            for p in psutil.Process(host.pid).children(recursive=True):
                identity = {"pid": p.pid, "creation": p.create_time()}
                if identity not in identities:
                    identities.append(identity)
        except psutil.Error:
            pass
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                pass
        if host.poll() is not None:
            raise AssertionError(f"Application probe exited before readiness: {host.returncode}")
        time.sleep(.02)
    raise AssertionError("Native readiness watchdog")


def _safety(host, identities):
    # This runs only after product observations/assertions; never acceptance evidence.
    if host.poll() is None:
        host.kill()
        host.wait(timeout=5)
    for identity in reversed(identities):
        if _alive(identity):
            try:
                psutil.Process(identity["pid"]).kill()
            except psutil.Error:
                pass


@pytest.mark.parametrize("backend", ["registered", "captured", "fds"])
def test_default_map_reclaims_native_tree_after_top_python_sigkill(tmp_path, backend):
    root = Path(__file__).resolve().parents[2]
    identities = []
    with (tmp_path / "host.log").open("wb") as log:
        host = subprocess.Popen([sys.executable, "-m", "test_support.python_owner_host", str(tmp_path), backend],
                                cwd=root, stdout=log, stderr=log)
        try:
            marker = _marker(tmp_path / "native.json", host, identities)
            for identity in ({"pid": marker["pid"], "creation": marker["creation"]},
                             {"pid": marker["child_pid"], "creation": marker["child_creation"]}):
                if identity not in identities:
                    identities.append(identity)
            descriptions = list(tmp_path.rglob("native_resources/*/description.json"))
            assert len(descriptions) == 1, "Actual guardian identity is required before fault injection"
            anchor = json.loads(descriptions[0].read_text())["anchor"]
            identity = {"pid": anchor["pid"], "creation": anchor["creation"]}
            if identity not in identities:
                identities.append(identity)
            assert _alive({"pid": marker["child_pid"], "creation": marker["child_creation"]})
            os.kill(host.pid, signal.SIGKILL)
            host.wait(timeout=5)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and any(_alive(p) for p in identities):
                time.sleep(.02)
            survivors = [p for p in identities if _alive(p)]
            (tmp_path / "automatic-exit-observation.json").write_text(json.dumps({"survivors": survivors}))
            assert survivors == [], "Managed native descendants survived top-level Python SIGKILL"
            with socket.socket() as connection:
                assert connection.connect_ex(("127.0.0.1", marker["port"])) != 0
        finally:
            _safety(host, identities)


def test_native_start_failure_preserves_os_cause_without_unconfirmed_cleanup(batch_project, monkeypatch):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.process import spawn_registered_process

    root, application, _worker, probe, _write = batch_project
    native_popen = subprocess.Popen

    def unavailable_executable(argv, *args, **options):
        if len(argv) > 1 and str(argv[1]).endswith("_process_guardian.py"):
            options["executable"] = str(root / "missing-python-executable")
        return native_popen(argv, *args, **options)

    monkeypatch.setattr(subprocess, "Popen", unavailable_executable)

    def behavior(_payload):
        spawn_registered_process([sys.executable, "-c", "print(1)"], env=dict(os.environ))

    probe.behavior = behavior
    with Application.open(application, project_root=root, file_logging=False) as app:
        receipt = app.worker("workflows/worker.yaml").map([WorkItem("native", {"value": 1})])
    outcome = receipt.outcomes[0]
    assert outcome.status == "failed" and outcome.error.error_type == "FileNotFoundError"
    assert outcome.cleanup_errors == (), "No process existed; cleanup uncertainty was fabricated"


@pytest.mark.parametrize("text", [False, True])
def test_default_map_preserves_native_stdio_root_result_and_root_first_cleanup(batch_project, text):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.process import spawn_registered_process

    root, application, _worker, probe, _write = batch_project
    payload = "中文 stdin\n" * 20000
    identities = []
    command = """
import json,os,signal,subprocess,sys
import psutil
child = subprocess.Popen([sys.executable,'-c',
 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)'])
print(json.dumps({'pid':os.getpid(),'child_pid':child.pid,
 'child_creation':psutil.Process(child.pid).create_time()}), flush=True)
data=sys.stdin.buffer.read()
sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
sys.stderr.buffer.write(b'actual native stderr');sys.stderr.buffer.flush()
sys.exit(7)
"""

    def behavior(_payload):
        process, _close = spawn_registered_process(
            [sys.executable, "-c", command], env=dict(os.environ), text=text,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        output, error = process.communicate(payload if text else payload.encode(), timeout=10)
        if not text:
            output, error = output.decode(), error.decode()
        marker, data = output.split("\n", 1)
        marker = json.loads(marker)
        identities.append({"pid": marker["child_pid"], "creation": marker["child_creation"]})
        assert process.pid == marker["pid"], "The public handle must identify the native root"
        assert process.returncode == 7, "The native exit code must not be replaced by the guardian's code"
        assert data == payload and error == "actual native stderr"
        return {"code": process.returncode, "size": len(data)}

    probe.behavior = behavior
    with Application.open(application, project_root=root, file_logging=False) as app:
        receipt = app.worker("workflows/worker.yaml").map([WorkItem("stdio", {"value": 1})])
    assert receipt.outcomes[0].status == "success", receipt.to_dict()
    assert identities and not any(_alive(identity) for identity in identities)


def test_cancel_during_real_bootstrap_construction_never_dispatches_native_command(batch_project, monkeypatch):
    from agentloom.app import Application
    from agentloom.execution import managed_process
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.resources import close_run_resources

    root, application, _worker, probe, _write = batch_project
    marker = root / "native-must-not-start"
    startup, release = Event(), Event()
    handles, bootstraps, failures = [], [], []
    original_popen, original_admit = subprocess.Popen, managed_process.NativeProcessScope.admit

    def admit(scope, process):
        original_admit(scope, process)
        handles.append(process)

    def popen(argv, *args, **options):
        process = original_popen(argv, *args, **options)
        if len(argv) > 1 and str(argv[1]).endswith("_process_guardian.py"):
            bootstraps.append({"pid": process.pid, "creation": psutil.Process(process.pid).create_time()})
            startup.set()
            assert release.wait(timeout=10)
        return process

    monkeypatch.setattr(managed_process.NativeProcessScope, "admit", admit)
    monkeypatch.setattr(subprocess, "Popen", popen)

    def behavior(_payload):
        context = copy_context()

        def cancel():
            assert startup.wait(timeout=10)
            try:
                context.run(close_run_resources)
            except BaseException as error:
                failures.append(error)

        def allow_bootstrap():
            assert startup.wait(timeout=10)
            deadline = time.monotonic() + 5
            # The private flag is only a fault-injection barrier. Acceptance
            # observes the public outcome and whether the command ever ran.
            while not handles[0]._stopped and time.monotonic() < deadline:
                time.sleep(.005)
            release.set()

        closer, releaser = Thread(target=cancel), Thread(target=allow_bootstrap)
        closer.start()
        releaser.start()
        try:
            managed_process.launch_owned_process(
                [sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).touch()"],
                env=dict(os.environ), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        finally:
            release.set()
            closer.join(timeout=10)
            releaser.join(timeout=10)
            assert not closer.is_alive() and not releaser.is_alive()

    probe.behavior = behavior
    with Application.open(application, project_root=root, file_logging=False) as app:
        receipt = app.worker("workflows/worker.yaml").map([WorkItem("startup", {"value": 1})])
    assert receipt.outcomes[0].status == "failed", receipt.to_dict()
    assert receipt.outcomes[0].error.error_type == "InterruptedError", receipt.to_dict()
    assert not marker.exists() and not any(_alive(identity) for identity in bootstraps)
    assert not failures, failures


@pytest.mark.parametrize("tool", ["grep", "glob", "snapshot"])
def test_application_search_and_profile_helpers_join_default_native_owner(batch_project, tool):
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.runtimes.smolagents.tools.search.glob_tool.glob_tool import glob_search
    from agentloom.runtimes.smolagents.tools.search.grep_tool.grep_tool import grep_search
    from agentloom.runtimes.smolagents.tools.shell.shell_snapshot import create_snapshot, remove_snapshot

    root, application, _worker, probe, _write = batch_project
    (root / "target.txt").write_text("native-search-target\n")

    def behavior(_payload):
        if tool == "grep":
            assert "native-search-target" in grep_search("native-search-target", path=str(root))
        elif tool == "glob":
            assert "target.txt" in glob_search("*.txt", path=str(root))
        else:
            snapshot = create_snapshot("/bin/bash", env={"PATH": os.environ["PATH"], "HOME": str(root)})
            try:
                assert snapshot is not None
            finally:
                remove_snapshot(snapshot)
        return {"tool": tool}

    probe.behavior = behavior
    with Application.open(application, project_root=root, file_logging=False) as app:
        receipt = app.worker("workflows/worker.yaml").map([WorkItem("helper", {"value": 1})])
    assert receipt.outcomes[0].status == "success", receipt.to_dict()
    descriptions = list(root.rglob("native_resources/*/description.json"))
    assert descriptions, "Task helper bypassed the common native resource admission"
    for description in descriptions:
        assert json.loads(description.with_name("exited.json").read_text())["physical_exit_confirmed"]
