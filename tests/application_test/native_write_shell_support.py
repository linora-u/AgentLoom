"""Ticket 06 real Application acceptance, with an independent executor fixture.

The model may be deterministic for CI or a real configured remote provider.
The fixture supplies file/subprocess execution only; policies, Hooks and
persistence are production code. Pi's own tool mapping belongs to ticket 10.
"""
from __future__ import annotations
import json
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import yaml
from agentloom.application.composition import build_builtin_runtime_registry
from agentloom.execution.agent_runtime import AgentRuntimeResult, RuntimeCapabilities
from agentloom.execution.model_protocol import FunctionCallItem, FunctionCallOutputItem, MessageItem, ToolDefinition
from agentloom.execution.native_tool_host import NativeToolHost
from agentloom.execution.native_tools import NativeCallIdentity, NativeCommitAck, NativeExecutionOutcome, NativePrepareRequest
from agentloom.execution.tool_protocol import ToolErrorRecord
from tests.application_test.native_read_support import READ
from tests.lib_test.execution.test_native_write_shell_host import write_manifest, shell_manifest

WRITE = write_manifest()
SHELL = shell_manifest()
TOOLS = (READ, WRITE, SHELL)
SCENARIOS = ("create", "overwrite", "stale", "unread", "excluded", "transformed", "command_allowed", "command_denied", "search_excluded", "sandbox_required", "executor_error", "cancel_before", "cancel_after", "large_output")


def planned_calls(cwd: Path, scenario: str, marker: str):
    if scenario in {"command_allowed", "large_output", "command_denied", "search_excluded", "sandbox_required"}:
        command = {
            "command_allowed": "printf SHELL_RECEIPT",
            "large_output": "cat large.txt",
            "command_denied": "touch forbidden.txt",
            "search_excluded": "grep -r SECRET .",
            "sandbox_required": "printf forbidden > forbidden.txt",
        }[scenario]
        return [("bash", {"command": command})]
    target = "source.txt" if scenario in {"overwrite", "stale", "unread"} else "denied.txt" if scenario == "excluded" else "new.txt"
    calls = [("native_write", {"path": str(cwd / target), "content": marker})]
    if scenario in {"overwrite", "stale"}:
        calls.insert(0, ("native_read", {"path": str(cwd / "source.txt")}))
    return calls


@contextmanager
def external_write_runtime(model_factory, observations):
    class Runtime:
        runtime_id = "native-write-acceptance"
        capabilities = RuntimeCapabilities(True, False, False, False)

        def __init__(self, definition):
            self.definition = definition

        def run(self, request):
            definition = self.definition
            scenario = definition.runtime_options["scenario"]
            cwd = definition.runtime_options["cwd"]
            model = model_factory(definition)
            items = [MessageItem("user", request.task)]
            calls, executions = [], 0
            with NativeToolHost(tools=TOOLS, cwd=cwd) as host:
                for _ in range(5):
                    turn = model.turn(items=tuple(items), tools=tuple(ToolDefinition(t.visible_name, "Execute the requested operation.", dict(t.parameters)) for t in TOOLS), instructions="Follow the exact tool sequence in the task, once each. Return DONE when finished, or BLOCKED/ERROR/CANCELLED/UNCERTAIN. Never retry a refused operation or add other tool calls.")
                    items.extend(turn.items)
                    requested = [item for item in turn.items if isinstance(item, FunctionCallItem)]
                    if not requested:
                        observations[request.run_id] = {"calls": calls, "executions": executions}
                        return AgentRuntimeResult(state="success", output="\n".join(item.text for item in turn.items if isinstance(item, MessageItem)))
                    for call in requested:
                        tool = next(t for t in TOOLS if t.visible_name == call.name)
                        identity = NativeCallIdentity(request.application_id, request.task_id, request.run_id, definition.instance_id, call.call_id)
                        if scenario == "stale" and call.name == "native_write":
                            (Path(cwd) / "source.txt").write_text("EXTERNAL_CHANGE")
                        prepared = host.prepare(NativePrepareRequest(identity, tool, cwd, json.loads(call.arguments_json)))
                        if prepared.rejection is not None:
                            content = "BLOCKED"
                        elif scenario == "cancel_before":
                            host.cancel(identity)
                            content = "CANCELLED"
                        else:
                            grant = host.start_execution(prepared.authorization)
                            executions += 1
                            try:
                                arguments = grant.final_arguments
                                if scenario == "executor_error":
                                    raise OSError("Independent executor failure before mutation")
                                if tool.operation == "read":
                                    raw = (Path(grant.cwd) / arguments["path"]).read_text()
                                elif tool.operation == "write":
                                    (Path(grant.cwd) / arguments["path"]).write_text(arguments["content"])
                                    raw = "written"
                                else:
                                    result = subprocess.run(["/bin/sh", "-c", arguments["command"]], cwd=grant.cwd, capture_output=True, text=True, timeout=10, check=True)
                                    raw = result.stdout
                                outcome = NativeExecutionOutcome(identity, grant.authorization_id, "completed", raw)
                            except (OSError, subprocess.SubprocessError) as exc:
                                outcome = NativeExecutionOutcome(identity, grant.authorization_id, "error", error=ToolErrorRecord(type(exc).__name__, "Executor failed", False, "native_executor"))
                            if scenario == "cancel_after":
                                host.cancel(identity)
                            settled = host.settle(outcome)
                            content = (settled.record.output if settled.record.status == "completed" else "ERROR") if isinstance(settled, NativeCommitAck) else "UNCERTAIN"
                        calls.append(host.receipt(identity))
                        items.append(FunctionCallOutputItem(call.call_id, str(content)))
                raise RuntimeError("Application model exceeded five turns")

        def snapshot(self):
            return None

        def close(self):
            self.definition.tool_gateway.close()

    registry = build_builtin_runtime_registry()
    registry.register("native-write-acceptance", capabilities=Runtime.capabilities, factory=Runtime)
    with patch("agentloom.application.validation.build_builtin_runtime_registry", lambda: registry), patch("agentloom.application.agent.build_builtin_runtime_registry", lambda: registry):
        yield


def write_application(root: Path, name: str, profile: str, scenario: str, *, smol=False):
    if smol:
        from tests.application_test.native_read_support import write_application as control
        return control(root, name, profile, "allowed", smol=True)
    app = root / "applications" / name
    cwd = app / "files"
    cwd.mkdir(parents=True)
    marker = "WRITE_" + uuid4().hex
    (cwd / "source.txt").write_text("ORIGINAL_" + marker)
    (cwd / "denied.txt").write_text("PRESERVE")
    (cwd / "large.txt").write_text((marker + "\n") * 4000)
    (cwd / "secrets").mkdir()
    (cwd / "secrets" / "secret.txt").write_text("SECRET_FORBIDDEN")
    plan = planned_calls(cwd, scenario, marker)
    config = {
        "name": name, "description": "Native write/Shell governance acceptance",
        "workflow": "Execute this exact sequence of tools, once each, in order: " + json.dumps(plan) + ". Then report DONE or the refusal/error label; do not retry.",
        "agent_runtime": "native-write-acceptance", "model_type": profile,
        "tools": [], "toolsets": [],
        "runtime_options": {"scenario": scenario, "cwd": str(cwd)},
        "shell_settings": {"allowed_commands": ["printf", "cat", "grep"], "allowed_operators": ["*"], "sandbox": {"enabled": scenario == "sandbox_required", "mode": "bwrap"}},
        "tool_access_control": {"path_validation": [{"tools": ["read_file", "write_file"], "exclude_paths": [str(cwd / "denied.txt")]}, {"tools": ["grep_search"], "exclude_paths": [str(cwd / "secrets")]}]},
    }
    if scenario == "transformed":
        import shlex
        response = {"decision": "modify", "modified_input": {"path": str(cwd / "transformed.txt"), "content": marker}}
        config["hooks"] = {"PreToolUse": [{"id": "write-transform", "matcher": "write_file", "command": "printf '%s' " + shlex.quote(json.dumps(response))}]}
    workflow = app / "workflows" / "root.yaml"
    workflow.parent.mkdir()
    workflow.write_text(yaml.safe_dump(config, sort_keys=False))
    return workflow, marker


def verify_native(scenario, observation, marker):
    calls = observation["calls"]
    assert len(calls) == (2 if scenario in {"overwrite", "stale"} else 1), "Unexpected model tool calls"
    receipt = calls[-1]
    cwd = Path(receipt["request"]["cwd"])
    assert (cwd / "denied.txt").read_text() == "PRESERVE"
    assert not (cwd / "forbidden.txt").exists()
    if scenario in {"stale", "unread", "excluded", "command_denied", "search_excluded", "sandbox_required"}:
        assert receipt["rejection"]["status"] == "blocked"
        assert observation["executions"] == (1 if scenario == "stale" else 0)
        assert (cwd / "source.txt").read_text() == ("EXTERNAL_CHANGE" if scenario == "stale" else "ORIGINAL_" + marker)
        assert "evidence" not in receipt
    elif scenario == "cancel_before":
        assert receipt["state"] == "cancelled" and observation["executions"] == 0
        assert not (cwd / "new.txt").exists()
    elif scenario == "cancel_after":
        assert receipt["state"] == "uncertain" and "evidence" not in receipt
        assert (cwd / "new.txt").read_text() == marker
    elif scenario == "executor_error":
        assert receipt["record"]["status"] == "error" and receipt["evidence"] == []
        assert not (cwd / "new.txt").exists()
    else:
        assert receipt["record"]["status"] == "completed"
        if scenario == "large_output":
            assert receipt["raw_output"] == (marker + "\n") * 4000
            assert receipt["result_scope"]["source_completeness"] == "unknown"
        elif scenario == "command_allowed":
            assert receipt["raw_output"] == "SHELL_RECEIPT"
        else:
            target = "source.txt" if scenario == "overwrite" else "transformed.txt" if scenario == "transformed" else "new.txt"
            assert (cwd / target).read_text() == marker
            if scenario == "overwrite":
                journal = Path(receipt["result_scope"]["raw_artifact"]["journal"])
                assert any(p.read_text() == "ORIGINAL_" + marker for p in (journal.parent / "native-file-history").rglob("*@v1"))
