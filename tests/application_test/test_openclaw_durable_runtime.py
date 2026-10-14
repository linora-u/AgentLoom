"""Real Pi + stdio MCP through the public durable executor and SDK Adapter."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from agentloom.app import Application
from agentloom.execution.concurrency import WorkItem
from agentloom.execution.durable.os_job import process_alive

from tests.acceptance.worker_batch_support import TOKENS, model_program, port_open, prepare
from tests.application_test.mixed_runtime_support import model_service

pytestmark = pytest.mark.skipif(not os.environ.get("AGENTLOOM_TEST_OPENCLAW_RUNTIME"),
                               reason="requires the pinned OpenClaw SDK assets")


def wait_for(predicate, timeout=35):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("Real Runtime ownership evidence did not arrive; see consumer/guardian logs")
        time.sleep(0.02)


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("fault", [False, True], ids=["normal", "executor-sigkill"])
def test_real_pi_and_mcp_task_scope_exits_automatically(tmp_path, fault):
    with model_service(model_program("pi")) as (url, requests):
        prepare(tmp_path, "pi", url=url, count=1)
        application = tmp_path / "applications" / "batch_acceptance"
        namespace = tmp_path / "durable"
        with Application.open(application, project_root=tmp_path, file_logging=False) as app, app.durable(root=namespace) as execution:
            handle = execution.worker("workflows/worker.yaml").submit(WorkItem("item-0", {
                "item_id": "item-0", "fact": TOKENS[0], "file_path": str(tmp_path / "item-0.txt"),
            }))
            with (tmp_path / "consumer.log").open("w") as log:
                consumer = subprocess.Popen([sys.executable, "-m", "test_support.durable_pi_host",
                    str(tmp_path), str(namespace), "stop" if fault else "success"],
                    cwd=Path(__file__).resolve().parents[2], stdout=log, stderr=log)
                identities = []
                try:
                    wait_for(lambda: bool(rows(tmp_path / "physical-resources.jsonl")))
                    mcp = rows(tmp_path / "mcp-events.jsonl")
                    bridges = rows(tmp_path / "bridge-processes.jsonl")
                    pids = {row["pid"] for row in mcp + bridges}
                    assert bridges and any(row.get("event") == "lookup" for row in mcp)
                    for pid in pids:
                        try:
                            identities.append({"pid": pid, "creation": psutil.Process(pid).create_time()})
                        except psutil.NoSuchProcess:
                            pass  # normal completion may already have drained an observed process
                    if fault:
                        assert len(identities) == len(pids), "fault requires live genuine Pi and MCP processes"
                        resources = rows(tmp_path / "physical-resources.jsonl")
                        assert all(port_open(row["port"]) for row in resources)
                        # Record the fault before delivery. No replacement consumer,
                        # recover(), or test-side kill runs before these observations.
                        (tmp_path / "fault.json").write_text(json.dumps({
                            "target_pid": consumer.pid, "signal": "SIGKILL", "time_ns": time.monotonic_ns(),
                            "runtime_identities": identities,
                        }))
                        consumer.kill()
                        consumer.wait(timeout=5)
                    else:
                        wait_for(lambda: execution.status(handle.workflow_id).status == "SUCCESS")
                        assert handle.get_result()["status"] == "success"
                        consumer.send_signal(signal.SIGTERM)
                        consumer.wait(timeout=35)
                    wait_for(lambda: all(not process_alive(value) for value in identities))
                    guardians = list((namespace / "jobs").glob("*/*/guardian.json"))
                    assert len(guardians) == 1
                    wait_for(lambda: all(not process_alive(json.loads(path.read_text())) for path in guardians))
                    assert all(not port_open(row["port"]) for row in rows(tmp_path / "physical-resources.jsonl"))
                    assert requests and all(request["model"] == "worker" for request in requests)
                    receipts = list((namespace / "jobs").glob("*/*/exited.json"))
                    assert len(receipts) == 1
                    assert json.loads(receipts[0].read_text())["physical_exit_confirmed"] is True
                finally:
                    # Test safety cleanup is after the asserted automatic drain.
                    if consumer.poll() is None:
                        consumer.send_signal(signal.SIGTERM)
                        try:
                            consumer.wait(timeout=35)
                        except subprocess.TimeoutExpired:
                            consumer.kill()
                            consumer.wait(timeout=5)
                    for identity in identities:
                        if process_alive(identity):
                            psutil.Process(identity["pid"]).kill()
