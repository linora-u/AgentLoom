"""Native tool-call behavior through the bridge and smolagents runtime."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from agentloom.runtimes.smolagents.agents import ToolCallingAgentV2
from agentloom.runtimes.smolagents.model_turn_bridge import SmolagentsModelTurnBridge
from agentloom.runtimes.smolagents.terminal import final_answer_binding
from agentloom.execution.hooks import HookPlan, HookRun
from agentloom.execution.logging import NullLoggerBackend
from agentloom.execution.model_binding import ModelTurnBinding
from agentloom.execution.model_protocol import (
    FunctionCallItem,
    ModelProtocolError,
    ModelTurnRequest,
    ModelTurnResult,
)
from agentloom.execution.tool_gateway import (
    AgentLoomToolGateway,
    bind_tool,
)
from agentloom.execution.trace import (
    bind_explicit_execution_context,
    capture_explicit_execution_context,
)
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


class EchoTool(Tool):
    name = "echo"
    description = "Echo text."
    inputs = {"text": {"type": "string", "description": "Text to echo"}}
    output_type = "string"

    def forward(self, text: str) -> str:
        return f"echo:{text}"


class AddTool(Tool):
    name = "add"
    description = "Add two integers."
    inputs = {
        "a": {"type": "integer", "description": "First integer"},
        "b": {"type": "integer", "description": "Second integer"},
    }
    output_type = "integer"

    def forward(self, a: int, b: int) -> int:
        return a + b


class NativeToolCallModel:
    model_id = "fake-native"

    def __init__(self, tool_call: ChatMessageToolCall):
        self.tool_call = tool_call
        self.seen_tools = None

    def generate(self, _messages, stop_sequences=None, tools_to_call_from=None, **_kwargs):
        self.seen_tools = tools_to_call_from
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[self.tool_call],
        )


class RecoveringModel:
    model_id = "fake-recovering"

    def __init__(self, error: str):
        self.calls = 0
        self.error = error

    def generate(self, _messages, stop_sequences=None, tools_to_call_from=None, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            raise ModelProtocolError(self.error)
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    id="call-final",
                    type="function",
                    function=ChatMessageToolCallFunction(
                        name="final_answer",
                        arguments={"answer": "recovered"},
                    ),
                )
            ],
        )


class RecordingAdapter:
    adapter_id = "openai_chat"

    def __init__(self, result_factory):
        self.result_factory = result_factory
        self.requests: list[ModelTurnRequest] = []

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
        self.requests.append(request)
        return self.result_factory(request)


def _binding(
    adapter,
    *,
    options: dict | None = None,
) -> ModelTurnBinding:
    return ModelTurnBinding(
        model_type="test",
        model_id="opaque-model",
        adapter=adapter,
        options=options or {},
    )


def _call(call_id: str, name: str, arguments) -> ChatMessageToolCall:
    return ChatMessageToolCall(
        id=call_id,
        type="function",
        function=ChatMessageToolCallFunction(name=name, arguments=arguments),
    )


def _agent(model, tools, *, max_steps=1, logger=None):
    gateway = AgentLoomToolGateway(
        [
            *(
                bind_tool(tool, output_normalizer=handle_agent_output_types)
                for tool in tools
            ),
            final_answer_binding(),
        ]
    )
    return ToolCallingAgentV2(
        tool_gateway=gateway,
        model=model,
        logger=logger,
        max_steps=max_steps,
        max_tokens=4096,
        verbosity_level=0,
    )


@pytest.fixture(autouse=True)
def _bind_tool_runtime():
    current = capture_explicit_execution_context()
    run = HookRun(HookPlan(), local_run_id="native-tools", root_run_id="native-tools")
    with bind_explicit_execution_context(
        current.__class__(
            task_id=current.task_id,
            sub_task_id=current.sub_task_id,
            agent_id=current.agent_id,
            agent_name=current.agent_name,
            agent_config=current.agent_config,
            skill_catalog=current.skill_catalog,
            hook_run=run,
            runtime_agent_path=current.runtime_agent_path,
            root_run_id="native-tools",
            local_run_id="native-tools",
        )
    ):
        yield


def _step() -> ActionStep:
    return ActionStep(step_number=1, timing=Timing(start_time=time.time()))


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"text": "native"}, "echo:native"),
        (json.dumps({"text": "native-json"}), "echo:native-json"),
        (json.dumps(json.dumps({"text": "native-double"})), "echo:native-double"),
    ],
)
def test_tool_calling_agent_executes_native_argument_forms(arguments, expected) -> None:
    model = NativeToolCallModel(_call("call-native", "echo", arguments))
    agent = _agent(model, [EchoTool()])
    memory_step = _step()

    agent.step(memory_step)

    assert model.seen_tools[0].name == "echo"
    assert memory_step.tool_calls[0].id == "call-native"
    assert memory_step.observations.strip() == expected


@pytest.mark.parametrize(
    "error",
    [
        "Tool 'missing' not found in registered tools ['echo', 'final_answer']",
        "Malformed tool_call for 'final_answer': arguments must be a JSON object",
    ],
)
def test_tool_calling_agent_recovers_from_model_protocol_error(error: str) -> None:
    model = RecoveringModel(error)
    agent = _agent(model, [EchoTool()], max_steps=2)

    assert agent.run("Return a final answer.") == "recovered"
    assert model.calls == 2


def test_tool_calling_agent_recovers_with_logging_disabled() -> None:
    model = RecoveringModel("bad structured output")
    agent = _agent(
        model,
        [EchoTool()],
        max_steps=2,
        logger=NullLoggerBackend(),
    )

    assert agent.run("Return a final answer.") == "recovered"


def test_bridge_projects_tools_and_required_choice() -> None:
    adapter = RecordingAdapter(
        lambda _request: ModelTurnResult(
            items=(
                FunctionCallItem(
                    call_id="call-echo",
                    name="echo",
                    arguments_json='{"text":"required"}',
                ),
            )
        )
    )
    model = SmolagentsModelTurnBridge(
        binding=_binding(adapter, options={"tool_choice": "auto"}),
    )

    with model.require_tool_calls():
        message = model.generate(
            [{"role": "user", "content": "echo"}],
            tools_to_call_from=[EchoTool()],
        )

    assert adapter.requests[0].tools[0].name == "echo"
    assert adapter.requests[0].options["tool_choice"] == "required"
    assert message.tool_calls[0].function.arguments == {"text": "required"}


def test_shared_bridge_keeps_concurrent_tool_schemas_isolated() -> None:
    barrier = threading.Barrier(2)

    def result(request: ModelTurnRequest) -> ModelTurnResult:
        tool = request.tools[0]
        barrier.wait(timeout=5)
        arguments = '{"text":"x"}' if tool.name == "echo" else '{"a":1,"b":2}'
        return ModelTurnResult(
            items=(
                FunctionCallItem(
                    call_id=f"call-{tool.name}",
                    name=tool.name,
                    arguments_json=arguments,
                ),
            )
        )

    model = SmolagentsModelTurnBridge(
        binding=_binding(RecordingAdapter(result)),
    )

    def generate(tool):
        message = model.generate([], tools_to_call_from=[tool])
        return message.tool_calls[0].function.name

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            "echo": pool.submit(generate, EchoTool()),
            "add": pool.submit(generate, AddTool()),
        }
        assert {name: future.result(timeout=5) for name, future in futures.items()} == {
            "echo": "echo",
            "add": "add",
        }


def test_shared_bridge_agent_id_is_execution_local() -> None:
    model = SmolagentsModelTurnBridge(
        binding=_binding(
            RecordingAdapter(lambda _request: ModelTurnResult())
        ),
    )
    assigned = threading.Barrier(2)
    observed = threading.Barrier(2)

    def observe(label):
        model.agent_id = label
        assigned.wait(timeout=5)
        current = model.agent_id
        observed.wait(timeout=5)
        model.agent_id = None
        return current

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {label: pool.submit(observe, label) for label in ("A", "B")}
        assert {label: future.result(timeout=5) for label, future in futures.items()} == {
            "A": "A",
            "B": "B",
        }
    assert model.agent_id is None


def test_bridge_rejects_unknown_tool_name() -> None:
    model = SmolagentsModelTurnBridge(
        binding=_binding(
            RecordingAdapter(
                lambda _request: ModelTurnResult(
                    items=(
                        FunctionCallItem(
                            call_id="call-missing",
                            name="missing",
                            arguments_json="{}",
                        ),
                    ),
                )
            )
        ),
    )

    with pytest.raises(ModelProtocolError, match="not found in registered tools"):
        model.generate([], tools_to_call_from=[EchoTool()])


@pytest.mark.parametrize(
    "content",
    [
        '{"name":"echo","arguments":{"text":"x"}}',
        '<tool_call><name>echo</name></tool_call>',
        "Calling tool: echo with text=x",
    ],
)
def test_bridge_never_parses_text_tool_call_fallback(content: str) -> None:
    model = SmolagentsModelTurnBridge(
        binding=_binding(
            RecordingAdapter(lambda _request: ModelTurnResult())
        ),
    )

    with pytest.raises(ModelProtocolError, match="native structured tool_calls"):
        model.parse_tool_calls(
            ChatMessage(role=MessageRole.ASSISTANT, content=content)
        )
