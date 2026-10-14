"""Actual Application, Pi SDK bridge, MCP peer and tool processes; no paid model."""
import json
import os
import shlex
import sys
import time
from contextlib import nullcontext
from contextvars import copy_context
from pathlib import Path
from threading import Thread
from uuid import uuid4

import psutil
import pytest


def main():
    from agentloom.app import Application
    from agentloom.execution.concurrency import WorkItem
    from agentloom.execution.process import CapturedProcessTimeout, run_captured_process
    from agentloom.execution.process_scope import bind_managed_process_scope
    from agentloom.execution.resources import register_resource
    from agentloom.execution.subprocess_env import build_subprocess_env
    from agentloom.execution.trace import get_current_agent_id
    from agentloom.integrations.mcp.client import AgentLoomMCPClient
    from agentloom.runtimes.pi.protocol import Handshake
    from agentloom.runtimes.pi.transport import PiTransport
    from mcp import StdioServerParameters

    from test_support.worker_batch import batch_project

    case, mode = Path(sys.argv[1]), sys.argv[2]
    default_owner = mode.startswith("default-")
    if default_owner:
        mode = mode.removeprefix("default-")
    project = case / "project"
    project.mkdir()
    patch = pytest.MonkeyPatch()
    fixture = batch_project.__wrapped__(project, patch)
    root, application, _worker, probe, _write = next(fixture)
    evidence = {"root_pid": os.getpid(), "root_creation": psutil.Process().create_time(),
                "root_group": os.getpgrp(), "models_requested": 0}

    def save_evidence():
        pending = case / "resources.pending"
        pending.write_text(json.dumps(evidence))
        pending.replace(case / "resources.json")

    def behavior(payload):
        bridge = PiTransport(get_current_agent_id() or uuid4().hex)
        reply = bridge.request(Handshake(method="handshake", protocol_version=2, bridge_version=1,
                                         native_tool_contract=1), timeout=15)
        evidence.update(pi_pid=bridge.process.pid, pi_group=os.getpgid(bridge.process.pid),
                        pi_creation=psutil.Process(bridge.process.pid).create_time(), pi_handshake_method=reply.payload.method)
        peer = Path(__file__).resolve().parents[1] / "tests/mcp_test/fixtures/stdio_server.py"
        peer_evidence = case / "mcp-events.jsonl"
        client = AgentLoomMCPClient(
            StdioServerParameters(command=sys.executable, args=[str(peer), str(peer_evidence)]),
            adapter_kwargs={"client_session_timeout_seconds": 2.0} if mode == "mcp-timeout" else None,
        )
        register_resource("actual-scoped-mcp", client.disconnect)
        tool = next(item for item in client.get_tools() if item.definition.name == "lookup")
        result = tool.forward(query="managed peer")
        evidence.update(mcp_pid=result["pid"], mcp_group=os.getpgid(result["pid"]),
                        mcp_creation=psutil.Process(result["pid"]).create_time(), mcp_answer=result["answer"])
        if mode in {"mcp-timeout", "mcp-cancel"}:
            slow = next(item for item in client.get_tools() if item.definition.name == "slow_lookup")
            errors = []

            def call_slow():
                try:
                    slow.forward(query="cancel this request")
                except BaseException as error:
                    errors.append(type(error).__name__)

            if mode == "mcp-timeout":
                call_slow()
                assert errors, "configured MCP timeout was not observed"
            else:
                call_context = copy_context()
                caller = Thread(target=call_context.run, args=(call_slow,))
                caller.start()
                deadline = time.monotonic() + 5
                while '"slow_started"' not in peer_evidence.read_text():
                    if time.monotonic() > deadline:
                        raise TimeoutError("MCP request start watchdog")
                    time.sleep(0.02)
                client.disconnect()
                caller.join(timeout=5)
                assert not caller.is_alive(), "MCP synchronous caller survived cancellation"
                assert errors, "MCP cancellation did not reach its caller"
            evidence["mcp_call_errors"] = errors
        if mode == "mcp-start-failure":
            try:
                AgentLoomMCPClient(StdioServerParameters(command=str(case / "missing-mcp-command")))
            except Exception as error:
                evidence["mcp_start_error"] = type(error).__name__
            else:
                raise AssertionError("missing MCP executable was accepted")
        result = run_captured_process(
            shlex.join([sys.executable, "-c", "import os; print(os.getpgrp())"]),
            cwd=str(root), env=build_subprocess_env(), stdin=b"", timeout=5,
            stdout_limit=1024, stderr_limit=1024,
        )
        evidence["tool_group"] = int(result.stdout.strip())
        if mode == "timeout":
            marker = case / "timeout-child.json"
            code = "import os,json,time; from pathlib import Path; Path(" + repr(str(marker)) + ").write_text(json.dumps({'pid':os.getpid(),'group':os.getpgrp()})); time.sleep(60)"
            try:
                run_captured_process(shlex.join([sys.executable, "-c", code]), cwd=str(root),
                                     env=build_subprocess_env(), stdin=b"", timeout=0.2,
                                     stdout_limit=1024, stderr_limit=1024)
            except CapturedProcessTimeout:
                evidence["tool_timeout_detected"] = True
            evidence["timeout_child"] = json.loads(marker.read_text())
        save_evidence()
        if mode == "hang":
            time.sleep(120)
        if mode == "crash":
            os._exit(17)
        return payload

    probe.behavior = behavior
    try:
        with (nullcontext() if default_owner else bind_managed_process_scope()), Application.open(
            application, project_root=root, file_logging=False,
        ) as app:
            result = app.worker("workflows/worker.yaml").invoke(WorkItem("managed", {"value": 1}))
            evidence["outcome_status"] = result.status
            evidence["outcome"] = result.to_dict()
            evidence["application_still_alive_after_tool"] = True
            save_evidence()
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass
        patch.undo()


if __name__ == "__main__":
    main()
