from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest
from agentloom.runtime.hooks import (
    HookEvent,
    HookHandler,
    HookPlan,
    HookResult,
    HookRun,
)
from agentloom.runtime.model_protocol import ToolDefinition
from agentloom.runtime.tool_gateway import (
    AgentLoomToolGateway,
    ToolBinding,
    bind_tool,
)
from agentloom.runtime.trace import (
    ExplicitExecutionContext,
    bind_explicit_execution_context,
)


def _binding(name: str, forward, *, schema=None) -> ToolBinding:
    properties = schema or {
        "text": {
            "type": "string",
            "description": "Text",
            "required": True,
        },
    }
    return ToolBinding(
        definition=ToolDefinition(
            name=name,
            description=f"{name} tool",
            parameters={
                "type": "object",
                "properties": properties,
                "required": [
                    key
                    for key, value in properties.items()
                    if value.get("required", True)
                ],
            },
        ),
        forward=forward,
        inputs_schema=properties,
    )


def _invoke(gateway, run, *, call_id="provider-call", name="echo", arguments=None):
    execution = ExplicitExecutionContext(
        task_id="task",
        sub_task_id=None,
        agent_id="agent",
        agent_name="agent",
        agent_config={},
        skill_catalog=None,
        hook_run=run,
        runtime_agent_path="agent",
        root_run_id=run.root_run_id,
        local_run_id=run.local_run_id,
    )
    with (
        bind_explicit_execution_context(execution),
        patch(
            "agentloom.runtime.hooks.path_validators.enforce_core_tool_guard",
            return_value=HookResult(),
        ),
    ):
        return gateway.invoke(
            call_id=call_id,
            tool_name=name,
            arguments=arguments or {"text": "value"},
        )


def test_gateway_preserves_stable_id_for_success_and_error() -> None:
    run = HookRun(HookPlan(), local_run_id="local", root_run_id="root")
    gateway = AgentLoomToolGateway(
        [
            _binding("echo", lambda text: f"echo:{text}"),
            _binding("explode", lambda text: (_ for _ in ()).throw(RuntimeError(text))),
        ]
    )

    success = _invoke(gateway, run, call_id="success-id")
    failed = _invoke(
        gateway,
        run,
        call_id="error-id",
        name="explode",
        arguments={"text": "boom"},
    )

    assert (success.call_id, success.status, success.output) == (
        "success-id",
        "completed",
        "echo:value",
    )
    assert (failed.call_id, failed.status, failed.reason) == (
        "error-id",
        "error",
        "boom",
    )


def test_gateway_hook_can_transform_or_block_before_side_effect() -> None:
    effects = []
    run = HookRun(
        HookPlan(
            (
                HookHandler(
                    HookEvent.PRE_TOOL_USE,
                    "echo",
                    lambda _context: HookResult(
                        decision="modify",
                        modified_input={"text": "changed"},
                    ),
                ),
                HookHandler(
                    HookEvent.PRE_TOOL_USE,
                    "denied",
                    lambda _context: HookResult(decision="block", reason="denied"),
                ),
            )
        ),
        local_run_id="local",
        root_run_id="root",
    )
    gateway = AgentLoomToolGateway(
        [
            _binding("echo", lambda text: effects.append(text) or text),
            _binding("denied", lambda text: effects.append(text) or text),
        ]
    )

    transformed = _invoke(gateway, run)
    blocked = _invoke(gateway, run, name="denied", call_id="blocked-id")

    assert transformed.input == {"text": "changed"}
    assert transformed.output == "changed"
    assert blocked.call_id == "blocked-id"
    assert blocked.status == "blocked"
    assert blocked.reason == "denied"
    assert effects == ["changed"]


def test_gateway_blocks_unknown_and_invalid_arguments_with_same_id() -> None:
    run = HookRun(HookPlan(), local_run_id="local", root_run_id="root")
    gateway = AgentLoomToolGateway(
        [_binding("echo", lambda text: text)]
    )

    unknown = _invoke(gateway, run, name="missing", call_id="unknown-id")
    invalid = _invoke(
        gateway,
        run,
        call_id="invalid-id",
        arguments={"unexpected": "value"},
    )

    assert (unknown.call_id, unknown.status, unknown.stage) == (
        "unknown-id",
        "blocked",
        "input_validation",
    )
    assert (invalid.call_id, invalid.status, invalid.stage) == (
        "invalid-id",
        "blocked",
        "final_decode",
    )


def test_gateway_rejects_duplicate_definitions_and_empty_call_id() -> None:
    binding = _binding("echo", lambda text: text)
    with pytest.raises(ValueError, match="Duplicate Tool definition"):
        AgentLoomToolGateway([binding, binding])

    gateway = AgentLoomToolGateway([binding])
    with pytest.raises(ValueError, match="non-empty"):
        gateway.invoke(call_id="", tool_name="echo", arguments={"text": "x"})


@dataclass
class _Closer:
    calls: int = 0

    def close(self) -> None:
        self.calls += 1


def test_gateway_close_is_idempotent() -> None:
    resource = _Closer()
    gateway = AgentLoomToolGateway(
        [],
        resource_closers=[resource.close],
    )

    gateway.close()
    gateway.close()

    assert resource.calls == 1


def test_plain_callable_binding_resolves_postponed_annotations() -> None:
    from agentloom.tools.file_ops.read_file import read_file

    binding = bind_tool(read_file)

    properties = binding.definition.parameters["properties"]
    assert properties["file_path"]["type"] == "string"
    assert properties["offset"]["type"] == "integer"
    assert properties["limit"]["type"] == "integer"
    assert binding.definition.parameters["required"] == ["file_path"]
    assert binding.inputs_schema["file_path"]["required"] is True
    assert binding.inputs_schema["offset"]["required"] is False
    assert binding.inputs_schema["limit"]["required"] is False
