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
    AgentRuntimeError,
    AgentRuntimeRequest,
    RuntimeCapabilities,
    RuntimeCheckpointEnvelope,
    RuntimeEvent,
    RuntimeRequirements,
)
from agentloom.runtime.goal import GoalCompleteError, GoalState
from agentloom.runtime.model_binding import ModelTurnBinding
from agentloom.runtime.model_protocol import (
    FunctionCallItem,
    FunctionCallOutputItem,
    MessageItem,
    ModelTurnRequest,
    ModelTurnResult,
    ModelUsage,
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

    def emit(self, step: object, *, agent: object) -> None:
        for callback in self.callbacks:
            callback(step, agent=agent)


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


class _EventTurnAdapter:
    adapter_id = "openai_responses"

    def turn(self, _request: ModelTurnRequest) -> ModelTurnResult:
        return ModelTurnResult(
            items=(MessageItem(role="assistant", text="done"),),
            response_id="response-1",
            usage=ModelUsage(
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
                cached_input_tokens=1,
            ),
        )


class _EventNativeRuntime(_NativeRuntime):
    def __init__(self) -> None:
        super().__init__(
            _NativeResult(
                output="done",
                token_usage={"input_tokens": 3, "output_tokens": 2},
            )
        )
        self.model = SmolagentsModelTurnBridge(
            binding=ModelTurnBinding(
                model_type="test",
                model_id="opaque-model",
                adapter=_EventTurnAdapter(),
            )
        )

    def run(self, **kwargs: object) -> _NativeResult:
        self.model.generate([{"role": "user", "content": "inspect"}])
        step = ActionStep(step_number=1, timing=Timing(start_time=0.0))
        step.tool_results = [
            ToolCallRecord.completed(
                call_id="call-1",
                tool_name="weather",
                input={"city": "Shanghai"},
                output={"temperature": 20},
            )
        ]
        self.memory.steps.append(step)
        self.step_callbacks.emit(step, agent=self)
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
    assert result.usage.input_tokens == 2
    assert result.checkpoint is not None
    assert result.checkpoint.runtime_id == "smolagents"
    assert result.checkpoint.state_schema_version == 2
    assert result.checkpoint.progress == 0
    assert result.checkpoint.payload["memory_steps"] == []
    assert result.checkpoint.payload["canonical_model_items"] == []


def test_adapter_emits_runtime_events_with_canonical_identity_and_typed_usage() -> None:
    native = _EventNativeRuntime()
    runtime = SmolagentsRuntimeAdapter(native)
    observed: list[RuntimeEvent] = []

    result = runtime.run(
        AgentRuntimeRequest(
            task="inspect",
            application_id="application",
            task_id="task",
            run_id="canonical-run",
            event_sink=observed.append,
        )
    )

    assert [event.kind for event in result.events] == [
        "run",
        "model",
        "usage",
        "tool",
        "checkpoint",
        "usage",
        "terminal",
    ]
    assert observed == list(result.events)
    assert {
        (event.application_id, event.task_id, event.run_id)
        for event in result.events
    } == {("application", "task", "canonical-run")}
    assert result.usage.input_tokens == 3
    assert result.usage.output_tokens == 2
    assert result.usage.total_tokens == 5
    assert result.checkpoint is not None
    assert result.checkpoint.task_id == "task"
    assert result.checkpoint.run_id == "canonical-run"


def test_adapter_classifies_provider_error_and_emits_terminal_failure() -> None:
    from litellm.exceptions import Timeout

    provider_error = Timeout(
        message="provider timeout",
        model="opaque-model",
        llm_provider="openai",
    )
    native = _NativeRuntime(_NativeResult(output=None))
    native.run = lambda **_kwargs: (_ for _ in ()).throw(provider_error)  # type: ignore[method-assign]
    runtime = SmolagentsRuntimeAdapter(native)
    observed: list[RuntimeEvent] = []

    with pytest.raises(AgentRuntimeError) as captured:
        runtime.run(
            AgentRuntimeRequest(
                task="inspect",
                event_sink=observed.append,
            )
        )

    assert captured.value.category == "provider"
    assert captured.value.cause is provider_error
    assert captured.value.retryable is True
    assert observed[-1].kind == "terminal"
    assert observed[-1].details == {
        "state": "failed",
        "category": "provider",
        "retryable": True,
    }


def test_adapter_rejects_unsupported_requirements_before_native_execution() -> None:
    class LimitedRuntimeAdapter(SmolagentsRuntimeAdapter):
        @property
        def capabilities(self) -> RuntimeCapabilities:
            return RuntimeCapabilities(
                structured_tools=True,
                parallel_tools=False,
                checkpoint_resume=True,
                subagents=True,
            )

    native = _NativeRuntime(_NativeResult(output="must not run"))
    runtime = LimitedRuntimeAdapter(native)
    observed: list[RuntimeEvent] = []

    with pytest.raises(AgentRuntimeError) as captured:
        runtime.run(
            AgentRuntimeRequest(
                task="inspect",
                requirements=RuntimeRequirements(parallel_tools=True),
                event_sink=observed.append,
            )
        )

    assert captured.value.category == "unsupported_capability"
    assert captured.value.retryable is False
    assert native.calls == []
    assert [event.kind for event in observed] == ["terminal"]
    assert observed[0].details["category"] == "unsupported_capability"


def test_adapter_preserves_goal_and_keyboard_interrupt_control_flow() -> None:
    goal_error = GoalCompleteError(
        GoalState.create(
            objective="finish",
            objective_fingerprint="test",
            token_budget=None,
        )
    )

    for control_error in (goal_error, KeyboardInterrupt()):
        native = _NativeRuntime(_NativeResult(output=None))

        def fail(
            *,
            error: BaseException = control_error,
            **_kwargs: object,
        ) -> _NativeResult:
            raise error

        native.run = fail  # type: ignore[method-assign]
        runtime = SmolagentsRuntimeAdapter(native)

        with pytest.raises(type(control_error)) as captured:
            runtime.run(AgentRuntimeRequest(task="inspect"))

        assert captured.value is control_error


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
    task = TaskStep(task="prior task")
    checkpoint = RuntimeCheckpointEnvelope(
        runtime_id="smolagents",
        runtime_version=runtime.runtime_version,
        state_schema_version=runtime.state_schema_version,
        progress=1,
        payload={
            "memory_steps": [
                task.dict() | {"_step_type": "TaskStep"}
            ],
            "canonical_model_items": (
                SmolagentsCheckpointCodec.serialize_canonical_model_items(
                    [task]
                )
            ),
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
        binding=ModelTurnBinding(
            model_type="test",
            model_id="opaque-model",
            adapter=turn_adapter,
        )
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
    canonical_items = (
        SmolagentsCheckpointCodec.serialize_canonical_model_items([step])
    )
    checkpoint = RuntimeCheckpointEnvelope.from_dict(
        json.loads(
            json.dumps(
                RuntimeCheckpointEnvelope(
                    runtime_id="smolagents",
                    runtime_version=runtime.runtime_version,
                    state_schema_version=runtime.state_schema_version,
                    progress=1,
                    payload={
                        "memory_steps": serialized_steps,
                        "canonical_model_items": canonical_items,
                    },
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


def test_adapter_rejects_old_schema_and_runtime_version() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    runtime = SmolagentsRuntimeAdapter(native)

    with pytest.raises(ValueError, match="schema 1"):
        runtime.restore(
            RuntimeCheckpointEnvelope(
                runtime_id="smolagents",
                runtime_version=runtime.runtime_version,
                state_schema_version=1,
                payload={"memory_steps": []},
            )
        )
    with pytest.raises(ValueError, match="runtime version"):
        runtime.restore(
            RuntimeCheckpointEnvelope(
                runtime_id="smolagents",
                runtime_version="different-version",
                state_schema_version=2,
                payload={
                    "memory_steps": [],
                    "canonical_model_items": [],
                },
            )
        )


def test_adapter_rejects_corrupt_call_output_linkage() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    runtime = SmolagentsRuntimeAdapter(native)
    step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0),
        observations="completed",
    )
    checkpoint = RuntimeCheckpointEnvelope(
        runtime_id="smolagents",
        runtime_version=runtime.runtime_version,
        state_schema_version=2,
        progress=1,
        payload={
            "memory_steps": (
                SmolagentsCheckpointCodec.serialize_memory_steps([step])
            ),
            "canonical_model_items": [
                {
                    "step_index": 0,
                    "item_index": 0,
                    "response_id": "response-1",
                    "item": {
                        "type": "function_call_output",
                        "call_id": "missing-call",
                        "output": "done",
                        "item_id": None,
                        "status": "completed",
                        "is_error": False,
                        "replay_payload": {},
                    },
                }
            ],
        },
    )

    with pytest.raises(ValueError, match="not correlated"):
        runtime.restore(checkpoint)
