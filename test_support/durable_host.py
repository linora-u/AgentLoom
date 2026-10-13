"""Real Application/OS fault fixture; never used by product execution."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event
import time

import psutil
import pytest

from agentloom.app import Application
from agentloom.execution.durable import HostSpec
from agentloom.execution.durable.files import atomic_json, read_json
from agentloom.execution.resources import register_resource
from test_support.worker_batch import batch_project


def emit(root, event, **fields):
    fd = os.open(root / "events.jsonl", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps({"event": event, "pid": os.getpid(), "time_ns": time.monotonic_ns(), **fields}) + "\n").encode())
    finally:
        os.close(fd)


class FixtureHost:
    def __init__(self, options):
        self.root = Path(options["case"])

    def reconcile(self, task):
        receipt = self.root / f"receipt-{task.item.id}.json"
        return read_json(receipt) if receipt.exists() else None

    def revoke(self, task, invocation_id):
        emit(self.root, "revoke", item_id=task.item.id, invocation_id=invocation_id)

    def invoke(self, task, invocation_id):
        project = self.root / invocation_id
        project.mkdir()
        patch = pytest.MonkeyPatch()
        fixture = batch_project.__wrapped__(project, patch)
        root, application, _, probe, _ = next(fixture)
        value = task.item.mutable_input()
        action = value.get("action", "success")

        def behavior(payload):
            emit(self.root, "runtime_started", item_id=task.item.id, invocation_id=invocation_id)
            if action in {"failure", "crash", "held"}:
                child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"], start_new_session=False)
                atomic_json(self.root / f"native-{task.item.id}.json", {"pid": child.pid,
                    "creation": psutil.Process(child.pid).create_time(), "group": os.getpgrp()})
                emit(self.root, "native_started", item_id=task.item.id, native_pid=child.pid)
                def close():
                    emit(self.root, "cleanup_entered", item_id=task.item.id)
                    if action == "failure":
                        raise OSError("registered native close is unknown")
                    while not (self.root / f"release-{task.item.id}").exists():
                        time.sleep(0.02)
                    child.terminate()
                    child.wait(timeout=5)
                register_resource("owned-native", close)
                if action == "crash":
                    os._exit(17)
            time.sleep(0.15)
            return payload

        probe.behavior = behavior
        try:
            with Application.open(application, project_root=root, file_logging=False) as app:
                outcome = app.worker(task.worker).invoke(task.item)
            result = outcome.to_dict()
            emit(self.root, "application_closed", item_id=task.item.id, status=outcome.status)
            if action == "commit-crash":
                atomic_json(self.root / f"receipt-{task.item.id}.json", result)
                emit(self.root, "business_committed", item_id=task.item.id)
                os._exit(19)
            return result
        finally:
            try:
                next(fixture)
            except StopIteration:
                pass
            patch.undo()


def fixture_host(options):
    return FixtureHost(options)


def main():
    project, application, namespace, case = map(Path, sys.argv[1:5])
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    host = HostSpec("test_support.durable_host:fixture_host", {"case": str(case)},
                    allow_replay=sys.argv[5] == "replay", max_attempts=1)
    patch = pytest.MonkeyPatch()
    fixture = batch_project.__wrapped__(project, patch)
    next(fixture)  # Install this trusted fixture Runtime before preflight.
    try:
        with Application.open(application, project_root=project, file_logging=False) as app, app.durable(root=namespace) as execution:
            execution.executor(["workflows/worker.yaml"], host=host).run(stop_token=stop)
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass
        patch.undo()


if __name__ == "__main__":
    main()
