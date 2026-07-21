import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from smolagents import LiteLLMModel, Tool
from smolagents.memory import ActionStep
from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction, MessageRole
from smolagents.monitoring import Timing

from src.lib.logging import NullLoggerBackend
from src.lib.smolagents.agent.base_agent import ToolCallingAgentV2
from src.lib.smolagents.models.litellm_model import LiteLLMModelV2
from src.lib.smolagents.models.tool_call_parser import ToolCallParseError


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


class TextFallbackModel:
    tool_name_key = "name"
    tool_arguments_key = "arguments"
    model_id = "fake-text"

    def __init__(self, content: str):
        self.content = content
        self._last_tools_to_call_from = []
        self.seen_tools = None

    def generate(self, _messages, stop_sequences=None, tools_to_call_from=None, **_kwargs):
        self.seen_tools = tools_to_call_from
        self._last_tools_to_call_from = list(tools_to_call_from or [])
        return ChatMessage(role=MessageRole.ASSISTANT, content=self.content)

    def parse_tool_calls(self, message):
        return LiteLLMModelV2.parse_tool_calls(self, message)


class NativeToolCallModel:
    def __init__(self, tool_call: ChatMessageToolCall):
        self.tool_call = tool_call
        self.seen_tools = None

    def generate(self, _messages, stop_sequences=None, tools_to_call_from=None, **_kwargs):
        self.seen_tools = tools_to_call_from
        return ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[self.tool_call])


class UnknownToolThenFinalModel:
    model_id = "fake-unknown-then-final"

    def __init__(self):
        self.calls = 0

    def generate(self, _messages, stop_sequences=None, tools_to_call_from=None, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            raise ToolCallParseError(
                "Tool 'tool_name' not found in registered tools "
                "['echo', 'final_answer']"
            )
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    id="call-final-after-unknown",
                    type="function",
                    function=ChatMessageToolCallFunction(
                        name="final_answer",
                        arguments={"answer": "recovered"},
                    ),
                )
            ],
        )


class MalformedArgumentsThenFinalModel:
    model_id = "fake-malformed-then-final"

    def __init__(self):
        self.calls = 0

    def generate(self, _messages, stop_sequences=None, tools_to_call_from=None, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            message = ChatMessage(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[
                    ChatMessageToolCall(
                        id="call-malformed",
                        type="function",
                        function=ChatMessageToolCallFunction(
                            name="final_answer",
                            arguments=(
                                '{"answer":{"summary":"returned '
                                '{"enabled": true}"}}'
                            ),
                        ),
                    )
                ],
            )
            LiteLLMModelV2._normalize_and_validate_tool_calls(
                message,
                list(tools_to_call_from or []),
            )
            return message
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    id="call-final-after-malformed",
                    type="function",
                    function=ChatMessageToolCallFunction(
                        name="final_answer",
                        arguments={"answer": {"summary": 'returned {"enabled": true}'}},
                    ),
                )
            ],
        )


def _make_agent(model, tools):
    return ToolCallingAgentV2(
        tools=tools,
        model=model,
        max_steps=1,
        max_tokens=4096,
        verbosity_level=0,
    )


def _action_step() -> ActionStep:
    return ActionStep(step_number=1, timing=Timing(start_time=time.time()))


def test_tool_calling_agent_executes_text_fallback_call():
    model = TextFallbackModel('{"name": "echo", "arguments": {"text": "hello"}}')
    agent = _make_agent(model, [EchoTool()])

    memory_step = _action_step()
    result = agent.step(memory_step)

    assert result.output is None
    assert model.seen_tools[0].name == "echo"
    assert memory_step.observations.strip() == "echo:hello"


def test_tool_calling_agent_executes_native_tool_call():
    tool_call = ChatMessageToolCall(
        id="call_native",
        type="function",
        function=ChatMessageToolCallFunction(name="echo", arguments={"text": "native"}),
    )
    model = NativeToolCallModel(tool_call)
    agent = _make_agent(model, [EchoTool()])

    memory_step = _action_step()
    agent.step(memory_step)

    assert model.seen_tools[0].name == "echo"
    assert memory_step.tool_calls[0].id == "call_native"
    assert memory_step.observations.strip() == "echo:native"


def test_tool_calling_agent_recovers_when_model_emits_an_unknown_tool() -> None:
    model = UnknownToolThenFinalModel()
    agent = ToolCallingAgentV2(
        tools=[EchoTool()],
        model=model,
        max_steps=2,
        max_tokens=4096,
        verbosity_level=0,
    )

    assert agent.run("Return a final answer.") == "recovered"
    assert model.calls == 2


def test_tool_calling_agent_rejects_malformed_native_arguments_and_recovers() -> None:
    model = MalformedArgumentsThenFinalModel()
    agent = ToolCallingAgentV2(
        tools=[],
        model=model,
        max_steps=2,
        max_tokens=4096,
        verbosity_level=0,
    )

    assert agent.run("Return a structured final answer.") == {
        "summary": 'returned {"enabled": true}'
    }
    assert model.calls == 2


def test_tool_calling_agent_runs_with_logging_explicitly_disabled() -> None:
    model = UnknownToolThenFinalModel()
    agent = ToolCallingAgentV2(
        tools=[EchoTool()],
        model=model,
        logger=NullLoggerBackend(),
        max_steps=2,
        max_tokens=4096,
        verbosity_level=0,
    )

    assert agent.run("Return a final answer.") == "recovered"


def test_tool_calling_agent_executes_native_tool_call_with_json_string_arguments():
    tool_call = ChatMessageToolCall(
        id="call_native_json",
        type="function",
        function=ChatMessageToolCallFunction(name="echo", arguments=json.dumps({"text": "native-json"})),
    )
    model = NativeToolCallModel(tool_call)
    agent = _make_agent(model, [EchoTool()])

    memory_step = _action_step()
    agent.step(memory_step)

    assert memory_step.tool_calls[0].id == "call_native_json"
    assert memory_step.observations.strip() == "echo:native-json"


def test_tool_calling_agent_executes_native_tool_call_with_double_encoded_arguments():
    tool_call = ChatMessageToolCall(
        id="call_native_double",
        type="function",
        function=ChatMessageToolCallFunction(
            name="echo",
            arguments=json.dumps(json.dumps({"text": "native-double"})),
        ),
    )
    model = NativeToolCallModel(tool_call)
    agent = _make_agent(model, [EchoTool()])

    memory_step = _action_step()
    agent.step(memory_step)

    assert memory_step.tool_calls[0].id == "call_native_double"
    assert memory_step.observations.strip() == "echo:native-double"


def test_text_fallback_applies_schema_bound_coercion_at_execution():
    model = TextFallbackModel('{"name": "add", "arguments": {"a": "2", "b": "3"}}')
    agent = _make_agent(model, [AddTool()])

    memory_step = _action_step()
    agent.step(memory_step)

    assert memory_step.observations.strip() == "5"


def test_litellm_model_keeps_tools_schema_and_tool_choice():
    model = LiteLLMModelV2(model_id="test/model")
    completion_kwargs = model._prepare_completion_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools_to_call_from=[EchoTool()],
        tool_choice="auto",
    )

    assert completion_kwargs["tools"][0]["function"]["name"] == "echo"
    assert completion_kwargs["tool_choice"] == "auto"


def test_tool_calling_agent_requires_a_tool_call_even_when_model_default_is_auto(
    monkeypatch,
):
    model = LiteLLMModelV2(model_id="test/model", tool_choice="auto")
    observed_choices: list[str] = []

    def generate(_messages, stop_sequences=None, tools_to_call_from=None, **_kwargs):
        completion_kwargs = model._prepare_completion_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            stop_sequences=stop_sequences,
            tools_to_call_from=tools_to_call_from,
        )
        observed_choices.append(completion_kwargs["tool_choice"])
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    id="call-required",
                    type="function",
                    function=ChatMessageToolCallFunction(
                        name="echo",
                        arguments={"text": "required"},
                    ),
                )
            ],
        )

    monkeypatch.setattr(model, "generate", generate)
    agent = _make_agent(model, [EchoTool()])

    memory_step = _action_step()
    agent.step(memory_step)

    assert observed_choices == ["required"]
    assert memory_step.observations.strip() == "echo:required"


def test_shared_litellm_model_keeps_concurrent_tool_schemas_isolated(monkeypatch):
    model = LiteLLMModelV2(model_id="test/model")
    generate_barrier = threading.Barrier(2)

    def _fake_parent_generate(
        _self,
        _messages,
        stop_sequences=None,
        response_format=None,
        tools_to_call_from=None,
        **_kwargs,
    ):
        tool_name = tools_to_call_from[0].name
        generate_barrier.wait(timeout=5)
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    id=f"call-{tool_name}",
                    type="function",
                    function=ChatMessageToolCallFunction(
                        name=tool_name,
                        arguments={"text": "x"} if tool_name == "echo" else {"a": 1, "b": 2},
                    ),
                )
            ],
        )

    monkeypatch.setattr(LiteLLMModel, "generate", _fake_parent_generate)

    def _generate_and_parse(tool):
        message = model.generate([], tools_to_call_from=[tool])
        return model.parse_tool_calls(message).tool_calls[0].function.name

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            "echo": pool.submit(_generate_and_parse, EchoTool()),
            "add": pool.submit(_generate_and_parse, AddTool()),
        }
        assert {name: future.result(timeout=5) for name, future in futures.items()} == {
            "echo": "echo",
            "add": "add",
        }


def test_shared_litellm_model_agent_id_is_execution_local():
    model = LiteLLMModelV2(model_id="test/model")
    assigned = threading.Barrier(2)
    observed = threading.Barrier(2)

    def _observe(label):
        model.agent_id = label
        assigned.wait(timeout=5)
        current = model.agent_id
        observed.wait(timeout=5)
        model.agent_id = None
        return current

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {label: pool.submit(_observe, label) for label in ("A", "B")}
        assert {label: future.result(timeout=5) for label, future in futures.items()} == {
            "A": "A",
            "B": "B",
        }
    assert model.agent_id is None


def test_generate_without_tool_calls_does_not_create_native_detection_state():
    model = LiteLLMModelV2(model_id="test/model")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(role=MessageRole.ASSISTANT, content='{"name":"echo","arguments":{"text":"x"}}', tool_calls=None)
            )
        ],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    model.retryer = lambda _func, **_kwargs: response
    model.client = SimpleNamespace(completion=lambda **_kwargs: response)

    message = model.generate([{"role": "user", "content": "hi"}], tools_to_call_from=[EchoTool()])

    assert message.tool_calls is None
    assert not hasattr(model, "_native_tool_calls_detected")
    assert not hasattr(model, "should_use_native_tool_calls")


def test_generate_with_native_tool_calls_does_not_create_native_detection_state():
    model = LiteLLMModelV2(model_id="test/model")
    tool_call = ChatMessageToolCall(
        id="call_auto",
        type="function",
        function=ChatMessageToolCallFunction(name="echo", arguments={"text": "x"}),
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(role=MessageRole.ASSISTANT, content="", tool_calls=[tool_call]))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    model.retryer = lambda _func, **_kwargs: response
    model.client = SimpleNamespace(completion=lambda **_kwargs: response)

    message = model.generate([{"role": "user", "content": "hi"}], tools_to_call_from=[EchoTool()])

    assert message.tool_calls == [tool_call]
    assert not hasattr(model, "_native_tool_calls_detected")
    assert not hasattr(model, "should_use_native_tool_calls")


def test_litellm_generate_normalizes_native_tool_call_arguments():
    model = LiteLLMModelV2(model_id="test/model")
    tool_call = ChatMessageToolCall(
        id="call_auto_json",
        type="function",
        function=ChatMessageToolCallFunction(
            name="echo",
            arguments=json.dumps(json.dumps({"text": "x"})),
        ),
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(role=MessageRole.ASSISTANT, content="", tool_calls=[tool_call]))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    model.retryer = lambda _func, **_kwargs: response
    model.client = SimpleNamespace(completion=lambda **_kwargs: response)

    message = model.generate([{"role": "user", "content": "hi"}], tools_to_call_from=[EchoTool()])

    assert message.tool_calls[0].function.arguments == {"text": "x"}


def test_litellm_generate_rejects_unknown_native_tool():
    model = LiteLLMModelV2(model_id="test/model")
    tool_call = ChatMessageToolCall(
        id="call_unknown",
        type="function",
        function=ChatMessageToolCallFunction(name="missing", arguments=json.dumps({"text": "x"})),
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(role=MessageRole.ASSISTANT, content="", tool_calls=[tool_call]))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    model.retryer = lambda _func, **_kwargs: response
    model.client = SimpleNamespace(completion=lambda **_kwargs: response)

    with pytest.raises(ToolCallParseError) as exc_info:
        model.generate([{"role": "user", "content": "hi"}], tools_to_call_from=[EchoTool()])

    assert "Tool 'missing' not found" in str(exc_info.value)


def test_text_fallback_rejects_unknown_tool_before_execution():
    model = TextFallbackModel('{"name": "missing", "arguments": {"text": "x"}}')
    agent = _make_agent(model, [EchoTool()])

    with pytest.raises(Exception) as exc_info:
        agent.step(_action_step())

    assert "Tool 'missing' not found" in str(exc_info.value)
