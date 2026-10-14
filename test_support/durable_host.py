"""Real Application/OS fault fixture; never used by product execution."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

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

    def authorize(self, task, invocation_id, assert_current):
        emit(self.root, "authorize_entered", item_id=task.item.id, invocation_id=invocation_id)
        if task.item.mutable_input().get("action") == "terminal-persistence-failure":
            import sqlite3

            from sqlalchemy import event
            from sqlalchemy.engine import Engine
            from sqlalchemy.exc import OperationalError
            def fail_terminal(connection, cursor, statement, parameters, context, executemany):
                values = parameters.values() if isinstance(parameters, dict) else parameters
                if ((self.root / "block-terminal-write").exists()
                        and statement.lstrip().upper().startswith("UPDATE ")
                        and ("SUCCESS" in values or "ERROR" in values)):
                    error = sqlite3.OperationalError("injected DBOS terminal persistence failure")
                    error.sqlite_errorcode = sqlite3.SQLITE_FULL
                    error.sqlite_errorname = "SQLITE_FULL"
                    emit(self.root, "terminal_write_failed", sqlite_errorcode=error.sqlite_errorcode)
                    raise OperationalError("UPDATE statement-secret", {"api_key": "credential-secret"}, error) from error
            event.listen(Engine, "before_cursor_execute", fail_terminal)
        if task.item.mutable_input().get("action") == "authorization-held":
            while not (self.root / "release-authorization").exists():
                time.sleep(0.01)
        assert_current()
        emit(self.root, "authorization_committed", item_id=task.item.id, invocation_id=invocation_id)

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
            if action == "captured-nested-timeout":
                from agentloom.execution.process import CapturedProcessTimeout, run_captured_process

                try:
                    run_captured_process([sys.executable, value["value"]["entry"]],
                        cwd=str(root), env=dict(os.environ), stdin=b"", timeout=8,
                        stdout_limit=1024, stderr_limit=1024, shell=False)
                except CapturedProcessTimeout:
                    emit(self.root, "captured_nested_timeout", item_id=task.item.id)
                    return payload
                raise AssertionError("Long nested Application did not reach its actual capture timeout")
            if action in {"detached-captured", "detached-mcp"}:
                from agentloom.execution.process import run_captured_process
                from agentloom.integrations.mcp.client import AgentLoomMCPClient
                from mcp import StdioServerParameters

                peer = Path(__file__).with_name("detached_resource_peer.py")
                marker = self.root / f"detached-{task.item.id}.json"
                # Deliberately minimal/forged tool-input environment. Only the
                # verified scope, rather than these supplied markers, may own
                # the actual transport and its ordinary detached descendant.
                environment = value["value"].get("env", {})
                if action == "detached-captured":
                    run_captured_process([sys.executable, str(peer), str(marker), "captured"],
                        cwd=str(root), env=environment, stdin=b"", timeout=90,
                        stdout_limit=1024, stderr_limit=1024, shell=False)
                else:
                    client = AgentLoomMCPClient(StdioServerParameters(
                        command=sys.executable, args=[str(peer), str(marker), "mcp"], env=environment,
                    ))
                    register_resource("detached-mcp", client.disconnect)
                    tool = next(tool for tool in client.get_tools() if tool.definition.name == "spawn")
                    assert tool.forward()["answer"] == 42
                    time.sleep(90)
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
    if os.environ.get("AGENTLOOM_TEST_ASYNCIO_THREADS"):
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        count = int(os.environ["AGENTLOOM_TEST_ASYNCIO_THREADS"])
        configure = asyncio.BaseEventLoop.set_default_executor
        executors = {}
        def bounded_loop(loop, executor):
            if loop not in executors:
                executors[loop] = ThreadPoolExecutor(max_workers=count)
            return configure(loop, executors[loop])
        patch.setattr(asyncio.BaseEventLoop, "set_default_executor", bounded_loop)
    fixture = batch_project.__wrapped__(project, patch)
    next(fixture)  # Install this trusted fixture Runtime before preflight.
    try:
        with Application.open(application, project_root=project, file_logging=False) as app, app.durable(root=namespace) as execution:
            jobs = None
            if os.environ.get("AGENTLOOM_TEST_PROCESS_OWNER") == "launchd":
                from agentloom.execution.durable.os_job import LaunchdJobs
                jobs = LaunchdJobs(execution.root, host)
            execution.executor(["workflows/worker.yaml"], host=host, jobs=jobs).run(stop_token=stop)
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass
        patch.undo()


if __name__ == "__main__":
    main()
