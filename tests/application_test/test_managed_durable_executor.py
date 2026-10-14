"""Default durable Application behavior, including the trusted startup gate."""
import json
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
def consumer(root, application, namespace, case, *, replay=False):
    with (case / "consumer.log").open("w") as log:
        process = subprocess.Popen([sys.executable, "-m", "test_support.durable_host", str(root), str(application),
            str(namespace), str(case), "replay" if replay else "no-replay"],
            cwd=Path(__file__).resolve().parents[2], stdout=log, stderr=log)
        try:
            wait_for(lambda: (namespace / "ready.json").exists(), process)
            yield process
        finally:
            # Safety release comes after assertions about automatic behavior.
            (case / "release-authorization").touch()
            for identity in ("old", "held"):
                (case / f"release-{identity}").touch()
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
