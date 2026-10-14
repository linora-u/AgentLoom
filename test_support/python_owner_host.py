"""Application probes using live Python factories and real controlled descendants."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def main():
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.process import spawn_registered_process

    from test_support.worker_batch import batch_project

    case = Path(sys.argv[1])
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
        process, _close = spawn_registered_process(
            [sys.executable, "-c", native, str(case / "native.json")], env=dict(os.environ),
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
