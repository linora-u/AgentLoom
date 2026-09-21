from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
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
    effects: list[str] = []

    def record_effect(text: str) -> str:
        effects.append(text)
        return text

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
            _binding("echo", record_effect),
            _binding("denied", record_effect),
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
    from agentloom.runtimes.smolagents.tools.file_ops.read_file import read_file

    binding = bind_tool(read_file)

    properties = binding.definition.parameters["properties"]
    assert binding.definition.description.startswith(
        "Reads a file from the local filesystem."
    )
    assert "Args:" not in binding.definition.description
    assert "Returns:" not in binding.definition.description
    assert "Raises:" not in binding.definition.description
    assert "Examples:" not in binding.definition.description
    assert properties["file_path"]["type"] == "string"
    assert properties["file_path"]["description"] == (
        "Absolute path to the file to read."
    )
    assert properties["offset"]["type"] == "integer"
    assert properties["offset"]["description"] == (
        "Line number to start reading from (1-based, default 1)."
    )
    assert properties["limit"]["type"] == "integer"
    assert properties["limit"]["description"] == (
        "Maximum number of lines to read. 0 means the default (2000 lines)."
    )
    assert binding.definition.parameters["required"] == ["file_path"]
    assert binding.inputs_schema["file_path"]["required"] is True
    assert binding.inputs_schema["offset"]["required"] is False
    assert binding.inputs_schema["limit"]["required"] is False


def test_tool_like_explicit_description_and_inputs_remain_authoritative() -> None:
    class ExplicitTool:
        name = "explicit"
        description = "Explicit Tool description with Args: kept verbatim."
        inputs = {
            "value": {
                "type": "string",
                "description": "Explicit input description.",
                "nullable": False,
            }
        }

        def forward(self, value: str) -> str:
            """A callable docstring that must not replace Tool metadata.

            Args:
                value: A docstring-derived description that must not win.
            """
            return value

    binding = bind_tool(ExplicitTool())

    assert binding.definition.description == (
        "Explicit Tool description with Args: kept verbatim."
    )
    assert binding.definition.parameters["properties"]["value"][
        "description"
    ] == "Explicit input description."


def test_from_tools_clones_stateful_tool_like_objects_per_gateway() -> None:
    class StatefulTool:
        name = "stateful"
        description = "Count calls in one runtime."
        inputs = {
            "text": {
                "type": "string",
                "description": "Text",
            }
        }
        output_type = "integer"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def forward(self, text: str) -> int:
            self.calls.append(text)
            return len(self.calls)

    definition = StatefulTool()
    first = AgentLoomToolGateway.from_tools([definition])
    second = AgentLoomToolGateway.from_tools([definition])
    first_run = HookRun(
        HookPlan(),
        local_run_id="first",
        root_run_id="root",
    )
    second_run = HookRun(
        HookPlan(),
        local_run_id="second",
        root_run_id="root",
    )

    assert _invoke(first, first_run, name="stateful").output == 1
    assert _invoke(second, second_run, name="stateful").output == 1
    assert definition.calls == []


def test_from_tools_requires_uncloneable_tool_like_factory() -> None:
    class UncloneableTool:
        name = "uncloneable"
        description = "Own a process lock."
        inputs: dict[str, dict[str, object]] = {}
        output_type = "string"

        def __init__(self) -> None:
            self.lock = Lock()

        def forward(self) -> str:
            return "ok"

    with pytest.raises(
        RuntimeError,
        match=r"cannot be isolated; implement clone_for_runtime\(\)",
    ):
        AgentLoomToolGateway.from_tools([UncloneableTool()])


def test_from_tools_rejects_clone_factory_returning_same_instance() -> None:
    class InvalidCloneTool:
        name = "invalid_clone"
        description = "Return itself from clone_for_runtime."
        inputs: dict[str, dict[str, object]] = {}
        output_type = "string"

        def clone_for_runtime(self):
            return self

        def forward(self) -> str:
            return "ok"

    with pytest.raises(
        RuntimeError,
        match=r"clone_for_runtime\(\) must return a distinct",
    ):
        AgentLoomToolGateway.from_tools([InvalidCloneTool()])


def test_plain_callable_binding_keeps_callable_identity() -> None:
    def echo(text: str) -> str:
        """Echo text.

        Args:
            text: Text to echo.
        """

        return text

    first = bind_tool(echo)
    second = bind_tool(echo)

    assert first.forward is echo
    assert second.forward is echo
