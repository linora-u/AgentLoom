from __future__ import annotations

import time

import pytest
from agentloom.execution.agent_runtime import AgentRuntimeError
from agentloom.execution.hooks import HookEvent, HookHandler, HookPlan, HookResult, HookRun
from agentloom.execution.model_binding import ModelTurnBinding
from agentloom.execution.tool_gateway import (
    AgentLoomToolGateway,
    bind_tool,
)
from agentloom.execution.tool_protocol import ToolCallRecord, ToolErrorRecord
from agentloom.execution.trace import (
    ExplicitExecutionContext,
    bind_explicit_execution_context,
    capture_explicit_execution_context,
)
from agentloom.integrations.litellm import OpenAIChatModelTurnAdapter
from agentloom.integrations.litellm.tool_error_projection import (
    patch_litellm_tool_error_projection,
)
from agentloom.runtimes.smolagents.agents import ToolCallingAgentV2
from agentloom.runtimes.smolagents.model_turn_bridge import SmolagentsModelTurnBridge
from agentloom.runtimes.smolagents.terminal import final_answer_binding
from smolagents import Tool
from smolagents.memory import ActionStep
from smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
)
from smolagents.monitoring import Timing
from smolagents.tools import handle_agent_output_types


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


class ManagedAgentLike:
    name = "worker"
    inputs = {"request": {"type": "string", "description": "Worker request"}}

    def __call__(self, request: str) -> str:
        return f"worker:{request}"


class SetupTool(Tool):
    name = "setup_tool"
    description = "Return state initialized by setup."
    inputs = {}
    output_type = "string"

    def setup(self) -> None:
        self.ready = "ready"
        super().setup()

    def forward(self) -> str:
        return self.ready


class InvalidImageTool(Tool):
    name = "invalid_image"
    description = "Return an invalid image payload."
    inputs = {}
    output_type = "image"

    def forward(self) -> object:
        return object()


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
        self.second_request_payload = _project_chat_messages(messages)
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[_call("final-after-error", "final_answer", {"answer": "recovered"})],
        )


def _gateway(*tools: object) -> AgentLoomToolGateway:
    return AgentLoomToolGateway(
        [
            *(
                bind_tool(tool, output_normalizer=handle_agent_output_types)
                for tool in tools
            ),
            final_answer_binding(),
        ]
    )


@pytest.fixture(autouse=True)
def _bind_tool_runtime():
    current = capture_explicit_execution_context()
    run = HookRun(HookPlan(), local_run_id="tool-results", root_run_id="tool-results")
    with bind_explicit_execution_context(
        ExplicitExecutionContext(
            task_id=current.task_id,
            sub_task_id=current.sub_task_id,
            agent_id=current.agent_id,
            agent_name=current.agent_name,
            agent_config=current.agent_config,
            skill_catalog=current.skill_catalog,
            hook_run=run,
            runtime_agent_path=current.runtime_agent_path,
            root_run_id="tool-results",
            local_run_id="tool-results",
        )
    ):
        yield


def _call(call_id: str, name: str, arguments: dict) -> ChatMessageToolCall:
    return ChatMessageToolCall(
        id=call_id,
        type="function",
        function=ChatMessageToolCallFunction(name=name, arguments=arguments),
    )


def _step() -> ActionStep:
    return ActionStep(step_number=1, timing=Timing(start_time=time.time()))


def _project_chat_messages(messages) -> list[dict]:
    captured: dict = {}

    def transport(**request):
        captured.update(request)
        return {
            "id": "wire-projection",
            "choices": [{"message": {"content": "done", "tool_calls": []}}],
            "usage": {},
        }

    model = SmolagentsModelTurnBridge(
        binding=ModelTurnBinding(
            model_type="test",
            model_id="openai/test",
            adapter=OpenAIChatModelTurnAdapter(transport=transport),
        )
    )
    model.generate(messages)
    return captured["messages"]


def test_tool_runtime_returns_one_canonical_terminal_record() -> None:
    gateway = _gateway(EchoTool(), ExplodingTool())
    completed = gateway.invoke(
        call_id="runtime-ok",
        tool_name="echo",
        arguments={"text": "kept"},
    )
    failed = gateway.invoke(
        call_id="runtime-error",
        tool_name="explode",
        arguments={"label": "isolated"},
    )

    assert completed.call_id == "runtime-ok"
    assert completed.status == "completed"
    assert completed.output == "echo:kept"
    assert completed.error is None
    assert failed.call_id == "runtime-error"
    assert failed.status == "error"
    assert failed.error.kind == "execution_error"
    assert failed.error.stage == "tool_execution"
    assert failed.error.retryable is False
    assert failed.model_content() == (
        '{"ok":false,"status":"error","error":'
        '{"kind":"execution_error","message":"boom:isolated",'
        '"retryable":false,"stage":"tool_execution"}}'
    )


def test_gateway_invokes_callable_managed_agent_binding() -> None:
    gateway = _gateway(ManagedAgentLike())
    settled = gateway.invoke(
        call_id="worker-call",
        tool_name="worker",
        arguments={"request": "audit"},
    )

    assert settled.status == "completed"
    assert settled.output == "worker:audit"


def test_hooked_tool_settlement_preserves_lazy_setup_contract() -> None:
    run = HookRun(HookPlan(), local_run_id="local-setup", root_run_id="root-setup")
    execution = ExplicitExecutionContext(
        task_id="task-setup",
        sub_task_id=None,
        agent_id="agent-setup",
        agent_name="agent",
        agent_config={},
        skill_catalog=None,
        hook_run=run,
        runtime_agent_path="agent",
        root_run_id="root-setup",
        local_run_id="local-setup",
    )
    gateway = _gateway(SetupTool())

    with bind_explicit_execution_context(execution):
        settled = gateway.invoke(
            call_id="setup-call",
            tool_name="setup_tool",
            arguments={},
        )

    assert settled.status == "completed"
    assert settled.output == "ready"


def test_output_validation_failure_is_the_only_hook_terminal_record() -> None:
    run = HookRun(HookPlan(), local_run_id="local-output", root_run_id="root-output")
    execution = ExplicitExecutionContext(
        task_id="task-output",
        sub_task_id=None,
        agent_id="agent-output",
        agent_name="agent",
        agent_config={},
        skill_catalog=None,
        hook_run=run,
        runtime_agent_path="agent",
        root_run_id="root-output",
        local_run_id="local-output",
    )

    with bind_explicit_execution_context(execution):
        settled = _gateway(InvalidImageTool()).invoke(
            call_id="invalid-output-call",
            tool_name="invalid_image",
            arguments={},
        )

    assert settled.status == "error"
    assert settled.stage == "output_validation"
    traced = run.tool_outcomes_snapshot()
    assert len(traced) == 1
    assert traced[0].status == "error"
    assert traced[0].stage == "output_validation"


def test_subagent_output_validation_failure_keeps_its_tool_category() -> None:
    def invalid_subagent() -> str:
        raise AgentRuntimeError(
            "Subagent exhausted its output correction budget",
            category="output_validation",
            retryable=True,
        )

    run = HookRun(
        HookPlan(),
        local_run_id="local-subagent-output",
        root_run_id="root-subagent-output",
    )
    execution = ExplicitExecutionContext(
        task_id="task-subagent-output",
        sub_task_id=None,
        agent_id="agent-subagent-output",
        agent_name="supervisor",
        agent_config={},
        skill_catalog=None,
        hook_run=run,
        runtime_agent_path="supervisor",
        root_run_id="root-subagent-output",
        local_run_id="local-subagent-output",
    )

    with bind_explicit_execution_context(execution):
        settled = AgentLoomToolGateway.from_tools(
            [invalid_subagent]
        ).invoke(
            call_id="invalid-subagent-output",
            tool_name="invalid_subagent",
            arguments={},
        )

    assert settled.status == "error"
    assert settled.error == ToolErrorRecord(
        kind="output_validation",
        message="Subagent exhausted its output correction budget",
        retryable=True,
        stage="output_validation",
    )


def test_terminal_record_rejects_nonterminal_or_contradictory_state() -> None:
    try:
        ToolCallRecord(call_id="bad", tool_name="echo", input={}, status="pending")
    except ValueError as error:
        assert "terminal status" in str(error)
    else:
        raise AssertionError("nonterminal status was accepted")

    try:
        ToolCallRecord(
            call_id="bad-completed",
            tool_name="echo",
            input={},
            status="completed",
            error=ToolErrorRecord("execution_error", "bad", False, "tool_execution"),
        )
    except ValueError as error:
        assert "completed Tool record" in str(error)
    else:
        raise AssertionError("contradictory completed record was accepted")

    for status in ("pending", "running", "cancelled"):
        with pytest.raises(ValueError, match="Unsupported Tool terminal status"):
            ToolCallRecord.from_dict(
                {
                    "call_id": "legacy-in-flight",
                    "tool_name": "echo",
                    "input": {},
                    "status": status,
                    "output": None,
                    "error": None,
                }
            )


def test_hook_trace_consumes_base_canonical_record_without_subtype_tags() -> None:
    run = HookRun(HookPlan(), local_run_id="base-record", root_run_id="root-record")
    run.record_tool_outcome(
        ToolCallRecord.completed(
            call_id="base-call",
            tool_name="echo",
            input={"text": "one"},
            output="echo:one",
        )
    )

    traced = run.tool_outcomes_snapshot()
    assert len(traced) == 1
    assert type(traced[0]) is ToolCallRecord
    assert traced[0].status == "completed"


def test_hooked_tool_settlement_sanitizes_completed_output_without_changing_terminal_state() -> None:
    run = HookRun(HookPlan(), local_run_id="local-settle", root_run_id="root-settle")
    execution = ExplicitExecutionContext(
        task_id="task-settle",
        sub_task_id=None,
        agent_id="agent-settle",
        agent_name="agent",
        agent_config={},
        skill_catalog=None,
        hook_run=run,
        runtime_agent_path="agent",
        root_run_id="root-settle",
        local_run_id="local-settle",
    )

    with bind_explicit_execution_context(execution):
        settled = _gateway(EchoTool()).invoke(
            call_id="hooked-success",
            tool_name="echo",
            arguments={"text": "sanitized"},
        )

    assert settled.status == "completed"
    assert settled.call_id == "hooked-success"
    assert str(settled.output) == "echo:sanitized"


def test_failed_tool_keeps_provider_call_id_and_error_record() -> None:
    model = NativeBatchModel([_call("provider-failure-42", "explode", {"label": "bad"})])
    agent = ToolCallingAgentV2(
        tool_gateway=_gateway(ExplodingTool()),
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
        tool_gateway=_gateway(EchoTool(), ExplodingTool()),
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
        tool_gateway=_gateway(ExplodingTool()),
        model=model,
        max_steps=1,
        max_tokens=4096,
        verbosity_level=0,
    )
    memory_step = _step()
    list(agent._step_stream(memory_step))
    agent.memory.steps.append(memory_step)

    messages = _project_chat_messages(agent.write_memory_to_messages())

    assert messages[-2:] == [
        {
            "role": "assistant",
            "content": None,
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


def test_litellm_projects_parallel_success_results_before_message_cleaning() -> None:
    model = NativeBatchModel(
        [
            _call("wire-ok-1", "echo", {"text": "one"}),
            _call("wire-ok-2", "echo", {"text": "two"}),
        ]
    )
    agent = ToolCallingAgentV2(
        tool_gateway=_gateway(EchoTool()),
        model=model,
        max_steps=1,
        max_tokens=4096,
        verbosity_level=0,
    )
    memory_step = _step()
    list(agent._step_stream(memory_step))
    agent.memory.steps.append(memory_step)

    messages = _project_chat_messages(agent.write_memory_to_messages())

    assert messages[-3:] == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "wire-ok-1",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"text":"one"}'},
                },
                {
                    "id": "wire-ok-2",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"text":"two"}'},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "wire-ok-1",
            "content": "echo:one",
        },
        {
            "role": "tool",
            "tool_call_id": "wire-ok-2",
            "content": "echo:two",
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
        "content": "done",
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
        tool_gateway=_gateway(EchoTool()),
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
        tool_gateway=_gateway(ExplodingTool()),
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
