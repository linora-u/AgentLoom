"""Default durable Application behavior, including the trusted startup gate."""
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from agentloom.app import Application
from agentloom.execution.concurrency import WorkItem

from test_support.worker_batch import batch_project as _batch_project

batch_project = _batch_project


def wait_for(predicate, process=None, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if process is not None and process.poll() is not None:
            raise AssertionError(f"Consumer exited {process.returncode}; inspect consumer.log")
        time.sleep(0.02)
    raise TimeoutError("Public durable behavior did not arrive; inspect preserved case logs")


def events(case):
    path = case / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@contextmanager
def consumer(root, application, namespace, case, *, replay=False, waiter_threads=None):
    environment = os.environ.copy()
    if waiter_threads is not None:
        environment["AGENTLOOM_TEST_ASYNCIO_THREADS"] = str(waiter_threads)
    with (case / "consumer.log").open("w") as log:
        process = subprocess.Popen([sys.executable, "-m", "test_support.durable_host", str(root), str(application),
            str(namespace), str(case), "replay" if replay else "no-replay"],
            cwd=Path(__file__).resolve().parents[2], stdout=log, stderr=log, env=environment)
        try:
            wait_for(lambda: (namespace / "ready.json").exists()
                     and json.loads((namespace / "ready.json").read_text())["pid"] == process.pid, process)
            yield process
        finally:
            # Safety release comes after assertions about automatic behavior.
            (case / "release-authorization").touch()
            for identity in ("old", "held"):
                (case / f"release-{identity}").touch()
            for native in case.glob("native-*.json"):
                (case / f"release-{native.stem.removeprefix('native-')}").touch()
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired as error:
                    process.kill()
                    process.wait(timeout=5)
                    raise AssertionError("Default executor failed to drain accepted scopes") from error


def test_default_executor_authorizes_before_dispatch_and_confirms_resource_exit(batch_project):
    root, application, _, _, _ = batch_project
    namespace, case = root / "durable", root / "case"
    case.mkdir()
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace) as execution:
        handle = execution.worker("workflows/worker.yaml").submit(WorkItem("one", {"value": 1}))
        with consumer(root, application, namespace, case) as process:
            wait_for(lambda: execution.status(handle.workflow_id).status == "SUCCESS", process)
            assert handle.get_result()["status"] == "success"
            order = [event["event"] for event in events(case)]
            assert order.index("authorization_committed") < order.index("runtime_started")
            assert execution.resource_status(handle.workflow_id)[0]["physical_exit_confirmed"]


def test_cancel_during_authorization_prevents_dispatch_and_retains_capacity(batch_project):
    root, application, _, _, _ = batch_project
    namespace, case = root / "durable", root / "case"
    case.mkdir()
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace) as execution:
        worker = execution.worker("workflows/worker.yaml")
        old = worker.submit(WorkItem("old", {"value": 1, "action": "authorization-held"}))
        with consumer(root, application, namespace, case) as process:
            wait_for(lambda: any(event["event"] == "authorize_entered" for event in events(case)), process)
            execution.cancel(old.workflow_id)
            replacement = worker.submit(WorkItem("replacement", {"value": 2}))
            assert execution.status(old.workflow_id).status == "CANCELLED"
            assert not execution.resource_status(old.workflow_id)[0]["physical_exit_confirmed"]
            time.sleep(0.2)
            assert not any(event["event"] == "runtime_started" for event in events(case))
            (case / "release-authorization").touch()
            wait_for(lambda: execution.status(replacement.workflow_id).status == "SUCCESS", process)
            assert not any(event["event"] in {"authorization_committed", "runtime_started"}
                           and event["item_id"] == "old" for event in events(case))
            assert execution.resource_status(old.workflow_id)[0]["physical_exit_confirmed"]


def test_business_commit_survives_crashed_ack_without_another_dispatch(batch_project):
    root, application, _, _, _ = batch_project
    namespace, case = root / "durable", root / "case"
    case.mkdir()
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace) as execution:
        handle = execution.worker("workflows/worker.yaml").submit(
            WorkItem("committed", {"value": 1, "action": "commit-crash"}))
        with consumer(root, application, namespace, case, replay=True) as process:
            wait_for(lambda: execution.status(handle.workflow_id).status == "ERROR", process)
            assert execution.resource_status(handle.workflow_id)[0]["physical_exit_confirmed"]
            execution.resume(handle.workflow_id)
            wait_for(lambda: execution.status(handle.workflow_id).status == "SUCCESS", process)
            assert handle.get_result()["status"] == "success"
            assert len([event for event in events(case) if event["event"] == "runtime_started"]) == 1


def test_fast_attempt_refills_while_slow_siblings_outnumber_wait_threads(batch_project):
    root, application, _, _, write = batch_project
    namespace, case = root / "durable", root / "case"
    case.mkdir()
    write(application / "config/system.yaml", {"worker_execution": {"defaults": {"concurrency": 3}}})
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace) as execution:
        worker = execution.worker("workflows/worker.yaml")
        slow = worker.submit_many([WorkItem(f"slow-{index}", {"value": index, "action": "held"}) for index in range(2)])
        with consumer(root, application, namespace, case, waiter_threads=2) as process:
            wait_for(lambda: len([event for event in events(case) if event["event"] == "cleanup_entered"]) == 2, process)
            quick = worker.submit(WorkItem("quick", {"value": 3}))
            wait_for(lambda: execution.status(quick.workflow_id).status == "SUCCESS", process, timeout=12)
            replacement = worker.submit(WorkItem("replacement", {"value": 4}))
            wait_for(lambda: execution.status(replacement.workflow_id).status == "SUCCESS", process, timeout=12)
            assert all(execution.status(handle.workflow_id).status == "PENDING" for handle in slow)
            assert all(not execution.resource_status(handle.workflow_id)[0]["physical_exit_confirmed"] for handle in slow)


def test_executor_sigkill_cleans_scope_before_recovery_or_replacement(batch_project):
    root, application, _, _, _ = batch_project
    namespace, case = root / "durable", root / "case"
    case.mkdir()
    with Application.open(application, project_root=root, file_logging=False) as app, app.durable(root=namespace) as execution:
        worker = execution.worker("workflows/worker.yaml")
        old = worker.submit(WorkItem("old", {"value": 1, "action": "held"}))
        with consumer(root, application, namespace, case) as first:
            wait_for(lambda: any(event["event"] == "cleanup_entered" for event in events(case)), first)
            first.kill()
            first.wait(timeout=5)
            # No recover(), new executor or test protection cleanup before this
            # observed certificate: only the surviving Python guardian acts.
            wait_for(lambda: execution.resource_status(old.workflow_id)[0]["physical_exit_confirmed"])
            new = worker.submit(WorkItem("new", {"value": 2}))
            with consumer(root, application, namespace, case) as replacement:
                wait_for(lambda: execution.status(new.workflow_id).status == "SUCCESS", replacement)
                wait_for(lambda: execution.status(old.workflow_id).status == "ERROR", replacement)
                assert len([event for event in events(case) if event["event"] == "runtime_started"
                            and event["item_id"] == "old"]) == 1
