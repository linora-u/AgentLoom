from __future__ import annotations

import ast
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from agentloom.runtimes.smolagents.agents import ToolCallingAgentV2
from agentloom.runtimes.smolagents.tool_proxy import (
    SmolagentsToolGatewayProxy,
    build_smolagents_tool_proxies,
)
from agentloom.runtime.model_protocol import ToolDefinition
from agentloom.runtime.tool_gateway import final_answer_binding
from agentloom.runtime.tool_protocol import ToolCallRecord
from smolagents.memory import ActionStep
from smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
)
from smolagents.monitoring import Timing

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _GatewayCall:
    call_id: str
    tool_name: str
    arguments: dict


class _RecordingGateway:
    def __init__(self, definitions, outputs=None):
        self._definitions = tuple(definitions)
        self.outputs = dict(outputs or {})
        self.calls: list[_GatewayCall] = []
        self._lock = threading.Lock()

    @property
    def definitions(self):
        return self._definitions

    def invoke(self, *, call_id, tool_name, arguments):
        with self._lock:
            self.calls.append(
                _GatewayCall(call_id, tool_name, dict(arguments))
            )
        output = self.outputs.get(tool_name, arguments)
        return ToolCallRecord.completed(
            call_id=call_id,
            tool_name=tool_name,
            input=arguments,
            output=output,
        )

    def close(self):
        raise AssertionError("Tool proxies must not close their Gateway")


def _definition(name: str, *, strict: bool | None = None) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name} description",
        parameters={
            "type": "object",
            "properties": {
                "payload": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "integer"},
                                },
                                "required": ["id"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["items"],
                    "additionalProperties": False,
                }
            },
            "required": ["payload"],
            "additionalProperties": False,
        },
        strict=strict,
    )


def _final_definition() -> ToolDefinition:
    return final_answer_binding().definition


def _call(call_id: str, name: str, arguments: dict):
    return ChatMessageToolCall(
        id=call_id,
        type="function",
        function=ChatMessageToolCallFunction(
            name=name,
            arguments=arguments,
        ),
    )


class _BatchModel:
    model_id = "gateway-test"

    def __init__(self, calls):
        self.calls = calls

    def generate(self, *_args, **_kwargs):
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=self.calls,
        )


def _step() -> ActionStep:
    return ActionStep(step_number=1, timing=Timing(start_time=time.time()))


def test_proxy_carries_the_exact_canonical_nested_and_strict_definition() -> None:
    definition = _definition("nested", strict=True)
    gateway = _RecordingGateway([definition, _final_definition()])

    proxy = build_smolagents_tool_proxies(gateway)[0]

    assert isinstance(proxy, SmolagentsToolGatewayProxy)
    assert proxy._agentloom_tool_definition is definition
    assert proxy.inputs["payload"]["properties"] == (
        definition.parameters["properties"]["payload"]["properties"]
    )
    assert proxy._agentloom_tool_definition.strict is True


def test_proxy_direct_call_uses_compatibility_id_without_owning_gateway() -> None:
    gateway = _RecordingGateway(
        [_definition("nested"), _final_definition()],
        outputs={"nested": "done"},
    )
    proxy = build_smolagents_tool_proxies(gateway)[0]

    assert proxy(payload={"items": [{"id": 1}]}) == "done"
    assert gateway.calls[0].tool_name == "nested"
    assert gateway.calls[0].call_id


def test_agent_routes_provider_id_and_final_answer_through_gateway() -> None:
    gateway = _RecordingGateway(
        [_definition("nested"), _final_definition()],
        outputs={"final_answer": "finished"},
    )
    agent = ToolCallingAgentV2(
        tool_gateway=gateway,
        model=_BatchModel(
            [_call("provider-final", "final_answer", {"answer": "finished"})]
        ),
        max_steps=1,
        max_tokens=4096,
        verbosity_level=0,
    )
    step = _step()

    outputs = list(agent._step_stream(step))

    assert gateway.calls == [
        _GatewayCall("provider-final", "final_answer", {"answer": "finished"})
    ]
    assert step.tool_results[0].call_id == "provider-final"
    assert outputs[-1].is_final_answer is True
    assert outputs[-1].output == "finished"


def test_final_answer_uses_provider_supported_string_schema() -> None:
    definition = _final_definition()

    assert definition.parameters["properties"]["answer"]["type"] == "string"
    assert definition.parameters["required"] == ["answer"]


def test_agent_resolves_native_state_references_before_gateway() -> None:
    gateway = _RecordingGateway(
        [_definition("nested"), _final_definition()]
    )
    agent = ToolCallingAgentV2(
        tool_gateway=gateway,
        model=_BatchModel([]),
        max_steps=1,
        max_tokens=4096,
        verbosity_level=0,
    )
    agent.state["saved_payload"] = {"items": [{"id": 7}]}

    record = agent.execute_tool_call_record(
        "nested",
        {"payload": "saved_payload"},
        call_id="provider-state",
    )

    assert record.call_id == "provider-state"
    assert gateway.calls == [
        _GatewayCall(
            "provider-state",
            "nested",
            {"payload": {"items": [{"id": 7}]}},
        )
    ]


def test_agent_routes_unknown_tool_to_gateway_with_provider_id() -> None:
    gateway = _RecordingGateway(
        [_definition("nested"), _final_definition()]
    )
    agent = ToolCallingAgentV2(
        tool_gateway=gateway,
        model=_BatchModel([]),
        max_steps=1,
        max_tokens=4096,
        verbosity_level=0,
    )

    record = agent.execute_tool_call_record(
        "missing",
        {},
        call_id="provider-unknown",
    )

    assert record.call_id == "provider-unknown"
    assert gateway.calls == [
        _GatewayCall("provider-unknown", "missing", {})
    ]


def test_parallel_agent_calls_preserve_each_provider_id() -> None:
    gateway = _RecordingGateway(
        [_definition("nested"), _final_definition()]
    )
    calls = [
        _call("provider-a", "nested", {"payload": {"items": [{"id": 1}]}}),
        _call("provider-b", "nested", {"payload": {"items": [{"id": 2}]}}),
    ]
    agent = ToolCallingAgentV2(
        tool_gateway=gateway,
        model=_BatchModel(calls),
        max_steps=1,
        max_tokens=4096,
        verbosity_level=0,
    )
    step = _step()

    list(agent._step_stream(step))

    assert {call.call_id for call in gateway.calls} == {
        "provider-a",
        "provider-b",
    }
    assert {record.call_id for record in step.tool_results} == {
        "provider-a",
        "provider-b",
    }


def test_agent_rejects_missing_provider_call_id() -> None:
    gateway = _RecordingGateway(
        [_definition("nested"), _final_definition()]
    )
    agent = ToolCallingAgentV2(
        tool_gateway=gateway,
        model=_BatchModel([]),
        max_steps=1,
        max_tokens=4096,
        verbosity_level=0,
    )

    with pytest.raises(ValueError, match="non-empty"):
        agent.execute_tool_call_record("nested", {}, call_id="")


def test_proxy_builder_requires_explicit_final_answer() -> None:
    gateway = _RecordingGateway([_definition("nested")])

    with pytest.raises(ValueError, match="explicitly provide"):
        build_smolagents_tool_proxies(gateway)


def test_agents_source_can_execute_tools_only_through_gateway() -> None:
    source = (
        ROOT / "src/runtimes/smolagents/agents.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_names = {
        "settle_tool_call",
        "inject_hooks",
        "clone_tool_for_runtime",
        "_execute_tool_pipeline",
    }

    assert forbidden_names.isdisjoint(
        {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
        }
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"forward", "__call__"}
        for node in ast.walk(tree)
    )
    assert ".tool_gateway.invoke(" in source


def test_removed_tool_shim_cannot_restore_a_second_execution_path() -> None:
    shim = ROOT / "src/runtimes/smolagents/tool_shim.py"
    protocol = (
        ROOT / "src/runtimes/smolagents/tool_protocol.py"
    ).read_text(encoding="utf-8")

    assert not shim.exists()
    assert "def settle_tool_call" not in protocol
    assert "TOOL_SETTLER_ATTR" not in protocol
