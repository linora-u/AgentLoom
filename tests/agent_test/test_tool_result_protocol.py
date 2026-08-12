from __future__ import annotations

import time

from smolagents import Tool
from smolagents.memory import ActionStep
from smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
)
from smolagents.monitoring import Timing

from src.lib.smolagents.agent.base_agent import ToolCallingAgentV2
from src.lib.smolagents.hooks import HookEvent, HookHandler, HookPlan, HookResult, HookRun
from src.lib.smolagents.hooks.tool_shim import inject_hooks
from src.lib.smolagents.models.litellm_model import LiteLLMModelV2
from src.lib.smolagents.models.provider_tool_errors import patch_litellm_tool_error_projection
from src.trace import ExplicitExecutionContext, bind_explicit_execution_context


class ExplodingTool(Tool):
    name = "explode"
    description = "Always fail with the supplied label."
    inputs = {"label": {"type": "string", "description": "Failure label"}}
    output_type = "string"

    def forward(self, label: str) -> str:
        raise RuntimeError(f"boom:{label}")


class EchoTool(Tool):
    name = "echo"
    description = "Echo the supplied text."
    inputs = {"text": {"type": "string", "description": "Text"}}
    output_type = "string"

    def forward(self, text: str) -> str:
        return f"echo:{text}"


class NativeBatchModel:
    model_id = "fake-native-batch"

    def __init__(self, calls: list[ChatMessageToolCall]) -> None:
        self.calls = calls

    def generate(self, _messages, stop_sequences=None, tools_to_call_from=None, **_kwargs):
        return ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=self.calls)


class FailureThenFinalModel:
    model_id = "fake-failure-then-final"

    def __init__(self) -> None:
        self.calls = 0
        self.second_request_payload: list[dict] | None = None

    def generate(self, messages, stop_sequences=None, tools_to_call_from=None, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return ChatMessage(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[_call("recover-call-11", "explode", {"label": "recoverable"})],
            )
        self.second_request_payload = LiteLLMModelV2(model_id="openai/test")._prepare_completion_kwargs(
            messages=messages,
            tools_to_call_from=tools_to_call_from,
        )["messages"]
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[_call("final-after-error", "final_answer", {"answer": "recovered"})],
        )


def _call(call_id: str, name: str, arguments: dict) -> ChatMessageToolCall:
    return ChatMessageToolCall(
        id=call_id,
        type="function",
        function=ChatMessageToolCallFunction(name=name, arguments=arguments),
    )


def _step() -> ActionStep:
    return ActionStep(step_number=1, timing=Timing(start_time=time.time()))


def test_failed_tool_keeps_provider_call_id_and_error_record() -> None:
    model = NativeBatchModel([_call("provider-failure-42", "explode", {"label": "bad"})])
    agent = ToolCallingAgentV2(
        tools=[ExplodingTool()],
        model=model,
        max_steps=1,
        max_tokens=4096,
        verbosity_level=0,
    )
    memory_step = _step()

    list(agent._step_stream(memory_step))

    assert memory_step.tool_calls[0].id == "provider-failure-42"
    assert len(memory_step.tool_results) == 1
    result = memory_step.tool_results[0]
    assert result.call_id == "provider-failure-42"
    assert result.status == "error"
    assert result.error.kind == "execution_error"
    assert result.error.retryable is False
    assert "boom:bad" in result.error.message


def test_parallel_calls_settle_success_and_failure_independently() -> None:
    model = NativeBatchModel(
        [
            _call("call-ok", "echo", {"text": "kept"}),
            _call("call-error", "explode", {"label": "isolated"}),
        ]
    )
    agent = ToolCallingAgentV2(
        tools=[EchoTool(), ExplodingTool()],
        model=model,
        max_steps=1,
        max_tokens=4096,
        verbosity_level=0,
    )
    memory_step = _step()

    list(agent._step_stream(memory_step))

    by_id = {result.call_id: result for result in memory_step.tool_results}
    assert by_id["call-ok"].status == "completed"
    assert by_id["call-ok"].output == "echo:kept"
    assert by_id["call-error"].status == "error"
    assert "boom:isolated" in by_id["call-error"].error.message


def test_litellm_payload_projects_native_tool_calls_and_error_results() -> None:
    model = NativeBatchModel([_call("wire-error-7", "explode", {"label": "wire"})])
    agent = ToolCallingAgentV2(
        tools=[ExplodingTool()],
        model=model,
        max_steps=1,
        max_tokens=4096,
        verbosity_level=0,
    )
    memory_step = _step()
    list(agent._step_stream(memory_step))
    agent.memory.steps.append(memory_step)

    completion = LiteLLMModelV2(model_id="openai/test")._prepare_completion_kwargs(
        messages=agent.write_memory_to_messages(),
    )

    assert completion["messages"][-2:] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "wire-error-7",
                    "type": "function",
                    "function": {"name": "explode", "arguments": '{"label":"wire"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "wire-error-7",
            "content": (
                '{"ok":false,"status":"error","error":'
                '{"kind":"execution_error","message":"boom:wire",'
                '"retryable":false,"stage":"tool_execution"}}'
            ),
        },
    ]


def test_litellm_projects_tool_errors_to_anthropic_and_bedrock_native_flags() -> None:
    from litellm.litellm_core_utils.prompt_templates import factory

    patch_litellm_tool_error_projection()
    tool_message = {
        "role": "tool",
        "tool_call_id": "wire-error-7",
        "content": (
            '{"ok":false,"status":"error","error":'
            '{"kind":"execution_error","message":"boom",'
            '"retryable":false,"stage":"tool_execution"}}'
        ),
    }

    anthropic = factory.convert_to_anthropic_tool_result(tool_message)
    bedrock = factory._convert_to_bedrock_tool_call_result(tool_message)

    assert anthropic["tool_use_id"] == "wire-error-7"
    assert anthropic["is_error"] is True
    assert bedrock["toolResult"]["toolUseId"] == "wire-error-7"
    assert bedrock["toolResult"]["status"] == "error"

    success_message = {
        "role": "tool",
        "tool_call_id": "wire-success-8",
        "content": '{"ok":true,"status":"completed","output":"done"}',
    }
    assert "is_error" not in factory.convert_to_anthropic_tool_result(success_message)
    assert "status" not in factory._convert_to_bedrock_tool_call_result(success_message)["toolResult"]


def test_policy_block_is_a_non_retryable_tool_result_with_same_call_id() -> None:
    observed = []
    run = HookRun(
        HookPlan(
            (
                HookHandler(
                    HookEvent.PRE_TOOL_USE,
                    "echo",
                    lambda context: observed.append(context)
                    or HookResult(decision="block", reason="workspace policy denied"),
                ),
            )
        ),
        local_run_id="local-block",
        root_run_id="root-block",
        project_root="/tmp",
    )
    execution = ExplicitExecutionContext(
        task_id="task-block",
        sub_task_id=None,
        agent_id="agent-block",
        agent_name="agent",
        agent_config={},
        skill_catalog=None,
        hook_run=run,
        runtime_agent_path="agent",
        root_run_id="root-block",
        local_run_id="local-block",
    )
    model = NativeBatchModel([_call("provider-block-8", "echo", {"text": "not-run"})])
    agent = ToolCallingAgentV2(
        tools=[inject_hooks(EchoTool())],
        model=model,
        max_steps=1,
        max_tokens=4096,
        verbosity_level=0,
    )
    memory_step = _step()

    with bind_explicit_execution_context(execution):
        list(agent._step_stream(memory_step))

    result = memory_step.tool_results[0]
    assert result.call_id == "provider-block-8"
    assert result.status == "blocked"
    assert result.error.kind == "policy_blocked"
    assert result.error.retryable is False
    assert observed[0].tool_call_id == "provider-block-8"


def test_next_model_request_receives_native_error_and_can_recover() -> None:
    model = FailureThenFinalModel()
    agent = ToolCallingAgentV2(
        tools=[ExplodingTool()],
        model=model,
        max_steps=2,
        max_tokens=4096,
        verbosity_level=0,
    )

    assert agent.run("Recover from the Tool failure.") == "recovered"

    assert model.second_request_payload is not None
    call_message, result_message = model.second_request_payload[-2:]
    assert call_message["tool_calls"][0]["id"] == "recover-call-11"
    assert result_message["role"] == "tool"
    assert result_message["tool_call_id"] == "recover-call-11"
    assert '"status":"error"' in result_message["content"]
    assert "boom:recoverable" in result_message["content"]
