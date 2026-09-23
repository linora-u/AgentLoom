"""External adapter fixture for ticket 05 Application acceptance, never Pi.

Only the executor/runtime boundary is supplied here. All transformations,
permissions, grants, evidence and durable results use the production host.
The live campaign supplies actual configured model bindings to this same loop.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import yaml
from agentloom.app.composition import build_builtin_runtime_registry
from agentloom.execution.agent_runtime import AgentRuntimeResult, RuntimeCapabilities
from agentloom.execution.model_protocol import FunctionCallItem, FunctionCallOutputItem, MessageItem, ToolDefinition
from agentloom.execution.native_tool_host import NativeReadToolHost
from agentloom.execution.native_tools import (
    NativeCallIdentity,
    NativeCommitAck,
    NativeExecutionOutcome,
    NativePrepareRequest,
    ToolManifestEntry,
)
from agentloom.execution.tool_protocol import ToolErrorRecord

READ = ToolManifestEntry(
    logical_name="read_file",
    visible_name="native_read",
    owner="runtime",
    provider="test-file-executor",
    capability="file.read",
    operation="read",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
    path_parameters=("path",),
)


@contextmanager
def external_read_runtime(model_factory, observations):
    class ExternalReadRuntime:
        runtime_id = "native-read-acceptance"
        capabilities = RuntimeCapabilities(True, False, False, False)

        def __init__(self, definition):
            self.definition = definition
            if definition.runtime_options.get("native_tools") != ["native_read"]:
                raise ValueError("Acceptance reader must be explicitly selected")

        def run(self, request):
            definition = self.definition
            scenario = definition.runtime_options["scenario"]
            cwd = definition.runtime_options["cwd"]
            model = model_factory(definition)
            items = (
                []
                if request.task is None
                else [MessageItem("user", request.task)]
            )
            calls, executions = [], 0
            with NativeReadToolHost(tools=(READ,), cwd=cwd) as host:
                for _ in range(4):
                    turn = model.turn(
                        items=tuple(items),
                        tools=(ToolDefinition("native_read", "Read a UTF-8 file.", dict(READ.parameters)),),
                        instructions="Use native_read exactly once for the requested path. Then return the receipt text or BLOCKED, ERROR, CANCELLED, UNCERTAIN. Do not retry refused reads.",
                    )
                    items.extend(turn.items)
                    requested = [item for item in turn.items if isinstance(item, FunctionCallItem)]
                    if not requested:
                        output = "\n".join(item.text for item in turn.items if isinstance(item, MessageItem))
                        observations[request.run_id] = {"calls": calls, "executions": executions}
                        return AgentRuntimeResult(state="success", output=output)
                    for call in requested:
                        if call.name != "native_read":
                            raise ValueError("Unexpected acceptance tool")
                        identity = NativeCallIdentity(
                            request.application_id,
                            request.task_id,
                            request.run_id,
                            definition.instance_id,
                            call.call_id,
                        )
                        prepared = host.prepare(
                            NativePrepareRequest(identity, READ, cwd, json.loads(call.arguments_json))
                        )
                        if prepared.rejection is not None:
                            content = "BLOCKED"
                        else:
                            grant = prepared.authorization
                            if scenario == "cancel_before":
                                host.cancel(identity)
                                content = "CANCELLED"
                            else:
                                delivered = host.start_execution(grant)
                                executions += 1
                                try:
                                    raw = (Path(delivered.cwd) / delivered.final_arguments["path"]).read_text()
                                    outcome = NativeExecutionOutcome(
                                        identity, delivered.authorization_id, "completed", raw
                                    )
                                except OSError as exc:
                                    outcome = NativeExecutionOutcome(
                                        identity,
                                        delivered.authorization_id,
                                        "error",
                                        error=ToolErrorRecord(
                                            type(exc).__name__, "External read failed", False, "native_executor"
                                        ),
                                    )
                                if scenario == "cancel_after":
                                    host.cancel(identity)
                                settled = host.settle(outcome)
                                content = (
                                    (settled.record.output if settled.record.status == "completed" else "ERROR")
                                    if isinstance(settled, NativeCommitAck)
                                    else "UNCERTAIN"
                                )
                        calls.append(host.receipt(identity))
                        items.append(FunctionCallOutputItem(call.call_id, str(content)))
                raise RuntimeError("Acceptance model exceeded four turns")

        def snapshot(self):
            return None

        def close(self):
            self.definition.tool_gateway.close()

    registry = build_builtin_runtime_registry()
    registry.register(
        "native-read-acceptance", capabilities=ExternalReadRuntime.capabilities, factory=ExternalReadRuntime
    )
    with (
        patch("agentloom.app.validation.build_builtin_runtime_registry", lambda: registry),
        patch("agentloom.app.agent.build_builtin_runtime_registry", lambda: registry),
    ):
        yield


SCENARIOS = (
    "allowed",
    "transformed",
    "excluded",
    "hook_blocked",
    "invalid_final",
    "missing",
    "cancel_before",
    "cancel_after",
)


def write_application(root: Path, name: str, profile: str, scenario: str, *, smol=False):
    app = root / "applications" / name
    cwd = app / "files"
    cwd.mkdir(parents=True)
    marker = "RECEIPT_" + uuid4().hex
    (cwd / "source.txt").write_text(marker)
    (cwd / "target.txt").write_text(marker + "_TRANSFORMED")
    (cwd / "denied.txt").write_text("MUST_NOT_READ")
    path = cwd / ("denied.txt" if scenario == "excluded" else "missing.txt" if scenario == "missing" else "source.txt")
    config = {
        "name": name,
        "description": "Native read governance acceptance",
        "workflow": f"Call {'read_file' if smol else 'native_read'} with {'file_path' if smol else 'path'}={str(path)!r} exactly once. Return the complete receipt text on success or the refusal/error label. Do not infer file contents.",
        "agent_runtime": "smolagents" if smol else "native-read-acceptance",
        "model_type": profile,
        "tools": [{"name": "read_file"}] if smol else [],
        "toolsets": [],
        "tool_access_control": {
            "path_validation": [{"tools": ["read_file"], "exclude_paths": [str(cwd / "denied.txt")]}]
        },
    }
    if smol:
        config["runtime_options"] = {"max_steps": 4, "smart_summary": False, "todo_mode": "off"}
    else:
        config["runtime_options"] = {"native_tools": ["native_read"], "scenario": scenario, "cwd": str(cwd)}
    if scenario in {"transformed", "hook_blocked", "invalid_final"}:
        response = (
            {"decision": "block", "reason": "BLOCKED"}
            if scenario == "hook_blocked"
            else {
                "decision": "modify",
                "modified_input": {"path": str(cwd / "target.txt") if scenario == "transformed" else 7},
            }
        )
        import shlex

        config["hooks"] = {
            "PreToolUse": [
                {
                    "id": "acceptance-transform",
                    "matcher": "native_read",
                    "command": "printf '%s' " + shlex.quote(json.dumps(response)),
                }
            ]
        }
    workflow = app / "workflows" / "root.yaml"
    workflow.parent.mkdir()
    workflow.write_text(yaml.safe_dump(config, sort_keys=False))
    return workflow, marker


def verify_native(scenario, observation, marker):
    assert len(observation["calls"]) == 1, "Expected exactly one actual model tool call"
    receipt = observation["calls"][0]
    if scenario in {"excluded", "hook_blocked", "invalid_final"}:
        assert observation["executions"] == 0
        assert receipt["rejection"]["status"] == "blocked"
    elif scenario == "cancel_before":
        assert observation["executions"] == 0 and receipt["state"] == "cancelled"
    elif scenario == "cancel_after":
        assert observation["executions"] == 1 and receipt["state"] == "uncertain"
    elif scenario == "missing":
        assert observation["executions"] == 1 and receipt["record"]["status"] == "error"
    else:
        assert observation["executions"] == 1 and receipt["state"] == "committed"
        assert receipt["raw_output"] == marker + ("_TRANSFORMED" if scenario == "transformed" else "")
        if scenario == "transformed":
            assert receipt["request"]["raw_arguments"] != receipt["final_arguments"]
