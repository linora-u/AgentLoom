"""Public DBOS executor with real Application/processes on the current OS."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from agentloom.app import Application
from agentloom.execution.concurrency import WorkItem
from agentloom.execution.durable import HostSpec
from agentloom.execution.durable.os_job import LaunchdJobs, process_alive
from agentloom.execution.durable.os_job import ResourceOwnershipError
from test_support.worker_batch import batch_project as _batch_project

batch_project = _batch_project
PROCESS_OWNER = os.environ.get("AGENTLOOM_TEST_PROCESS_OWNER", "openclaw")
pytestmark = pytest.mark.skipif(sys.platform != "darwin" and PROCESS_OWNER == "launchd",
                               reason="launchd product adapter requires macOS")


def wait_for(predicate, process=None, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if process is not None and process.poll() is not None:
            raise AssertionError(f"Consumer exited {process.returncode}; see consumer.log")
        time.sleep(0.03)
    raise TimeoutError("Durable executor public behavior did not arrive; see preserved case trace")


def events(case):
    path = case / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@contextmanager
def consumer(root, application, namespace, case, *, replay=False):
    repository = Path(__file__).resolve().parents[2]
    with (case / f"consumer-{time.time_ns()}.log").open("w") as log:
        process = subprocess.Popen([sys.executable, "-m", "test_support.durable_host", str(root), str(application),
            str(namespace), str(case), "replay" if replay else "no-replay"], cwd=repository, stdout=log, stderr=log)
        try:
            wait_for(lambda: (namespace / "ready.json").exists() and json.loads((namespace / "ready.json").read_text())["pid"] == process.pid, process)
            yield process
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=35)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    raise AssertionError("Consumer failed to drain on stop")
            # Independent safety cleanup of exact OS resources, preserving logs.
            spec = HostSpec("test_support.durable_host:fixture_host", {"case": str(case)}, allow_replay=replay)
            if PROCESS_OWNER == "openclaw":
                from agentloom.execution.durable.openclaw_job import OpenClawJobs
                runtime = os.environ.get("AGENTLOOM_TEST_OPENCLAW_RUNTIME")
                jobs = OpenClawJobs(namespace, spec,
                    runtime_root=Path(runtime) if runtime else None,
                    node=os.environ.get("AGENTLOOM_TEST_OPENCLAW_NODE"))
            else:
                jobs = LaunchdJobs(namespace, spec)
            jobs.recover()


def test_public_executor_completes_real_application_without_model(batch_project):
    root, application, _, _, write = batch_project
    namespace, case = root / "durable", root / "case"
    case.mkdir()
    write(application / "config/system.yaml", {"worker_execution": {"workers": {"workflows/worker.yaml": {"concurrency": 2}}}})
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace) as execution:
        worker = execution.worker("workflows/worker.yaml")
        handles = worker.submit_many([WorkItem(str(i), {"value": i}) for i in range(4)])
        with consumer(root, application, namespace, case) as process:
            wait_for(lambda: all(execution.status(handle.workflow_id).status in {"SUCCESS", "ERROR"} for handle in handles), process)
            results = [handle.get_result() for handle in handles]
            assert all(result["status"] == "success" for result in results)
            assert len([e for e in events(case) if e["event"] == "runtime_started"]) == 4
        assert process.returncode == 0
        for path in (namespace / "jobs").glob("*/*/identity.json"):
            assert not process_alive(json.loads(path.read_text()))


@pytest.mark.parametrize("action", ["failure", "crash", "held"])
def test_capacity_replacement_waits_for_old_native_exit(batch_project, action):
    root, application, _, _, _ = batch_project
    namespace, case = root / "durable", root / "case"
    case.mkdir()
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace) as execution:
        worker = execution.worker("workflows/worker.yaml")
        old = worker.submit(WorkItem("old", {"value": 1, "action": action}))
        with consumer(root, application, namespace, case) as process:
            wait_for(lambda: (case / "native-old.json").exists(), process)
            native = json.loads((case / "native-old.json").read_text())
            new = worker.submit(WorkItem("new", {"value": 2}))
            if action == "held":
                wait_for(lambda: any(e["event"] == "cleanup_entered" for e in events(case)), process)
                execution.cancel(old.workflow_id)
            wait_for(lambda: any(e["event"] == "runtime_started" and e["item_id"] == "new" for e in events(case)), process)
            assert not process_alive(native), "replacement overlapped old native process"
            wait_for(lambda: execution.status(new.workflow_id).status == "SUCCESS", process)
            assert new.get_result()["status"] == "success"
            assert execution.status(old.workflow_id).status == ("CANCELLED" if action == "held" else "ERROR")
        assert process.returncode == 0


def test_business_receipt_reconciles_after_root_crash_without_repeat_model(batch_project):
    root, application, _, _, _ = batch_project
    namespace, case = root / "durable", root / "case"
    case.mkdir()
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace) as execution:
        handle = execution.worker("workflows/worker.yaml").submit(WorkItem("committed", {"value": 1, "action": "commit-crash"}))
        with consumer(root, application, namespace, case) as process:
            wait_for(lambda: execution.status(handle.workflow_id).status == "ERROR", process)
            execution.resume(handle.workflow_id)
            wait_for(lambda: execution.status(handle.workflow_id).status == "SUCCESS", process)
            assert handle.get_result()["status"] == "success"
            assert len([e for e in events(case) if e["event"] == "runtime_started"]) == 1


def test_executor_sigkill_reaps_old_scope_before_upstream_recovery(batch_project):
    root, application, _, _, _ = batch_project
    namespace, case = root / "durable", root / "case"
    case.mkdir()
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace) as execution:
        worker = execution.worker("workflows/worker.yaml")
        old = worker.submit(WorkItem("old", {"value": 1, "action": "held"}))
        # Keep the first context open: its independent cleanup must not run
        # before the second process performs real startup recovery.
        with consumer(root, application, namespace, case) as first:
            wait_for(lambda: any(e["event"] == "cleanup_entered" for e in events(case)), first)
            native = json.loads((case / "native-old.json").read_text())
            first.kill()
            first.wait(timeout=5)
            if PROCESS_OWNER == "openclaw":
                # No new executor or test-side recovery may do this cleanup.
                wait_for(lambda: not process_alive(native))
                wait_for(lambda: all(not process_alive(json.loads(path.read_text()))
                                    for path in (namespace / "jobs").glob("*/*/guardian.json")))
            else:
                assert process_alive(native), "fault did not leave an old OS-owned invocation"
            new = worker.submit(WorkItem("new", {"value": 2}))
            with consumer(root, application, namespace, case) as replacement:
                wait_for(lambda: execution.status(new.workflow_id).status == "SUCCESS", replacement)
                assert not process_alive(native)
                wait_for(lambda: execution.status(old.workflow_id).status == "ERROR", replacement)
                assert len([e for e in events(case) if e["event"] == "runtime_started" and e["item_id"] == "old"]) == 1


def test_resume_rejects_live_scope_even_after_upstream_cancel_status(batch_project):
    root, application, _, _, _ = batch_project
    namespace, case = root / "durable", root / "case"
    case.mkdir()
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace) as execution:
        old = execution.worker("workflows/worker.yaml").submit(WorkItem("old", {"value": 1, "action": "held"}))
        with consumer(root, application, namespace, case) as process:
            wait_for(lambda: any(e["event"] == "cleanup_entered" for e in events(case)), process)
            execution.cancel(old.workflow_id)
            assert execution.status(old.workflow_id).status == "CANCELLED"
            with pytest.raises(ResourceOwnershipError, match="drain"):
                execution.resume(old.workflow_id)
