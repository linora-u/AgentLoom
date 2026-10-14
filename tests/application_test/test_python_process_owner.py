"""Native lifecycle through public Application calls; no foreign runtime owner."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

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


def test_default_map_reclaims_native_tree_after_top_python_sigkill(tmp_path):
    root = Path(__file__).resolve().parents[2]
    identities = []
    with (tmp_path / "host.log").open("wb") as log:
        host = subprocess.Popen([sys.executable, "-m", "test_support.python_owner_host", str(tmp_path), "kill"],
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
