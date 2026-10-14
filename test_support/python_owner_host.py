"""Application probes using live Python factories and real controlled descendants."""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


def main():
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.process import run_captured_process, run_process_to_fds, spawn_registered_process

    from test_support.worker_batch import batch_project

    case = Path(sys.argv[1])
    mode = sys.argv[2]
    project = case / "project"
    project.mkdir()
    patch = pytest.MonkeyPatch()
    fixture = batch_project.__wrapped__(project, patch)
    root, application, _worker, probe, _write = next(fixture)
    native = """
import json,os,signal,socket,subprocess,sys,time
from pathlib import Path
import psutil
child = subprocess.Popen([sys.executable,'-c',
 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)'])
server=socket.socket();server.bind(('127.0.0.1',0));server.listen()
Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'creation':psutil.Process().create_time(),
 'child_pid':child.pid,'child_creation':psutil.Process(child.pid).create_time(),'port':server.getsockname()[1]}))
time.sleep(120)
"""

    def behavior(payload):
        command = [sys.executable, "-c", native, str(case / "native.json")]
        if mode == "captured":
            run_captured_process(shlex.join(command), cwd=str(root), env=dict(os.environ), stdin=b"", timeout=90,
                                 stdout_limit=1024, stderr_limit=1024)
            return payload
        if mode == "fds":
            with open(os.devnull, "wb") as output:
                run_process_to_fds(shlex.join(command), cwd=str(root), env=dict(os.environ),
                                   stdout_fd=output.fileno(), stderr_fd=output.fileno(), timeout=90)
            return payload
        process, _close = spawn_registered_process(
            command, env=dict(os.environ),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        process.wait(timeout=90)
        return payload

    probe.behavior = behavior
    try:
        with Application.open(application, project_root=root, file_logging=False) as app:
            receipt = app.worker("workflows/worker.yaml").map([WorkItem("native", {"value": 1})])
            (case / "receipt.json").write_text(json.dumps(receipt.to_dict()))
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass
        patch.undo()


if __name__ == "__main__":
    main()
