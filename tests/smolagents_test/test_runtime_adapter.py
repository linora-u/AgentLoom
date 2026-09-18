from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from agentloom.adapters.smolagents.checkpoint_codec import (
    SmolagentsCheckpointCodec,
)
from agentloom.adapters.smolagents.model_turn_bridge import (
    MODEL_ITEMS_RAW_KEY,
    MODEL_RESPONSE_ID_RAW_KEY,
    SmolagentsModelTurnBridge,
)
from agentloom.adapters.smolagents.runtime_adapter import SmolagentsRuntimeAdapter
from agentloom.adapters.smolagents.tool_protocol import (
    action_step_to_protocol_messages,
)
from agentloom.runtime.agent_runtime import (
    AgentRuntimeRequest,
    RuntimeCheckpointEnvelope,
)
from agentloom.runtime.model_protocol import (
    FunctionCallItem,
    FunctionCallOutputItem,
    MessageItem,
    ModelTurnRequest,
    ModelTurnResult,
    ReasoningItem,
)
from agentloom.runtime.tool_protocol import ToolCallRecord
from smolagents.memory import ActionStep, TaskStep, ToolCall
from smolagents.models import ChatMessage, MessageRole
from smolagents.monitoring import Timing


@dataclass
class _NativeResult:
    output: object
    state: str = "success"
    token_usage: object | None = None


class _NativeRuntime:
    def __init__(self, result: _NativeResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []
        self.closed = False
        self.memory = SimpleNamespace(steps=[])
        self.step_callbacks = _CallbackRegistry()

    def run(self, **kwargs: object) -> _NativeResult:
        self.calls.append(kwargs)
        return self.result

    def close(self) -> None:
        self.closed = True


class _CallbackRegistry:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def register(self, _step_type: object, callback: object) -> None:
        self.callbacks.append(callback)


@dataclass
class _Tool:
    name: str = "weather"
    description: str = "Get weather."
    inputs = {
        "city": {
            "type": "string",
            "description": "City name",
        }
    }


class _RecordingTurnAdapter:
    adapter_id = "openai_responses"

    def __init__(self) -> None:
        self.requests: list[ModelTurnRequest] = []

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
        self.requests.append(request)
        return ModelTurnResult(
            items=(MessageItem(role="assistant", text="done"),),
        )


class _ReplayNativeRuntime(_NativeRuntime):
    def __init__(self, model: SmolagentsModelTurnBridge) -> None:
        super().__init__(_NativeResult(output="done"))
        self.model = model

    def run(self, **kwargs: object) -> _NativeResult:
        messages = []
        for step in self.memory.steps:
            messages.extend(action_step_to_protocol_messages(step))
        self.model.generate(messages, tools_to_call_from=[_Tool()])
        return super().run(**kwargs)


def test_adapter_translates_runtime_request_and_result() -> None:
    native = _NativeRuntime(_NativeResult(output={"ok": True}, token_usage={"input": 2}))
    runtime = SmolagentsRuntimeAdapter(native)

    result = runtime.run(
        AgentRuntimeRequest(
            task="inspect",
            continue_session=True,
            record_task=True,
            additional_args={"scope": "runtime"},
        )
    )

    assert native.calls == [
        {
            "task": "inspect",
            "return_full_result": True,
            "reset": False,
            "_skip_task_step_on_reset_false": False,
            "additional_args": {"scope": "runtime"},
        }
    ]
    assert result.state == "success"
    assert result.output == {"ok": True}
    assert result.usage == {"input": 2}
    assert result.checkpoint is not None
    assert result.checkpoint.runtime_id == "smolagents"
    assert result.checkpoint.payload == {
        "memory_steps": [],
        "step_count": 0,
    }


def test_adapter_omits_empty_additional_args_and_resets_new_session() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    runtime = SmolagentsRuntimeAdapter(native)

    runtime.run(AgentRuntimeRequest(task="inspect"))

    assert native.calls == [
        {
            "task": "inspect",
            "return_full_result": True,
            "reset": True,
        }
    ]


def test_adapter_rejects_unsuccessful_native_state() -> None:
    native = _NativeRuntime(_NativeResult(output=None, state="error"))
    runtime = SmolagentsRuntimeAdapter(native)

    with pytest.raises(RuntimeError, match="error"):
        runtime.run(AgentRuntimeRequest(task="inspect"))


def test_adapter_preserves_max_steps_for_goal_owner_to_settle() -> None:
    native = _NativeRuntime(_NativeResult(output=None, state="max_steps_error"))
    runtime = SmolagentsRuntimeAdapter(native)

    result = runtime.run(AgentRuntimeRequest(task="inspect"))

    assert result.state == "max_steps_error"


def test_adapter_closes_native_runtime_when_supported() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    runtime = SmolagentsRuntimeAdapter(native)

    runtime.close()

    assert native.closed is True


def test_adapter_restores_native_memory_from_compatible_checkpoint() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    runtime = SmolagentsRuntimeAdapter(native)
    checkpoint = RuntimeCheckpointEnvelope(
        runtime_id="smolagents",
        runtime_version=runtime.runtime_version,
        state_schema_version=runtime.state_schema_version,
        payload={
            "memory_steps": [
                TaskStep(task="prior task").dict() | {"_step_type": "TaskStep"}
            ]
        },
    )

    runtime.run(
        AgentRuntimeRequest(
            task="continue",
            continue_session=True,
            checkpoint=checkpoint,
        )
    )

    assert [step.task for step in native.memory.steps] == ["prior task"]


def test_resume_replays_canonical_items_through_the_next_model_turn() -> None:
    turn_adapter = _RecordingTurnAdapter()
    model = SmolagentsModelTurnBridge(
        adapter=turn_adapter,
        model_id="opaque-model",
    )
    native = _ReplayNativeRuntime(model)
    runtime = SmolagentsRuntimeAdapter(native)
    reasoning = ReasoningItem(
        item_id="reasoning-1",
        summary=("Inspect the repository",),
        replay_payload={
            "type": "reasoning",
            "encrypted_content": "opaque-ciphertext",
        },
    )
    function_call = FunctionCallItem(
        call_id="call-1",
        name="weather",
        arguments_json='{"city":"Shanghai"}',
        item_id="function-1",
    )
    tool_record = ToolCallRecord.completed(
        call_id="call-1",
        tool_name="weather",
        input={"city": "Shanghai"},
        output={"temperature": 20},
    )
    step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0),
        tool_calls=[
            ToolCall(
                name="weather",
                arguments={"city": "Shanghai"},
                id="call-1",
            )
        ],
        model_output_message=ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            raw={
                MODEL_ITEMS_RAW_KEY: (reasoning, function_call),
                MODEL_RESPONSE_ID_RAW_KEY: "response-1",
            },
        ),
        observations=tool_record.model_content(),
    )
    step.tool_results = [tool_record]
    serialized_steps = SmolagentsCheckpointCodec.serialize_memory_steps([step])
    checkpoint = RuntimeCheckpointEnvelope.from_dict(
        json.loads(
            json.dumps(
                RuntimeCheckpointEnvelope(
                    runtime_id="smolagents",
                    runtime_version=runtime.runtime_version,
                    state_schema_version=runtime.state_schema_version,
                    payload={"memory_steps": serialized_steps},
                ).to_dict()
            )
        )
    )

    runtime.run(
        AgentRuntimeRequest(
            task="continue",
            continue_session=True,
            checkpoint=checkpoint,
        )
    )

    assert turn_adapter.requests[0].items == (
        reasoning,
        function_call,
        FunctionCallOutputItem(
            call_id="call-1",
            output=tool_record.model_content(),
            status="completed",
            replay_payload={"record": tool_record.to_dict()},
        ),
    )


def test_adapter_pushes_runtime_checkpoint_to_sink() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    native.memory.steps = [TaskStep(task="done")]
    runtime = SmolagentsRuntimeAdapter(native)
    observed: list[RuntimeCheckpointEnvelope] = []

    result = runtime.run(
        AgentRuntimeRequest(
            task="inspect",
            checkpoint_sink=observed.append,
        )
    )

    assert observed == [result.checkpoint]
    assert result.checkpoint is not None
    assert result.checkpoint.payload["memory_steps"][0]["_step_type"] == "TaskStep"


def test_adapter_registers_one_native_step_bridge_and_pushes_completed_step() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    observed: list[RuntimeCheckpointEnvelope] = []
    SmolagentsRuntimeAdapter(native, checkpoint_sink=observed.append)

    assert len(native.step_callbacks.callbacks) == 1
    completed = ActionStep(step_number=1, timing=Timing(start_time=0.0))
    native.step_callbacks.callbacks[0](completed, agent=native)

    assert len(observed) == 1
    assert observed[0].payload["memory_steps"][0]["_step_type"] == "ActionStep"


def test_adapter_snapshot_and_restore_own_native_memory() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    native.memory.steps = [TaskStep(task="worker task")]
    runtime = SmolagentsRuntimeAdapter(native)

    checkpoint = runtime.snapshot()
    native.memory.steps = []

    runtime.restore(checkpoint)

    assert [step.task for step in native.memory.steps] == ["worker task"]
