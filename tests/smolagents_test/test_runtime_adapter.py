from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from agentloom.adapters.smolagents.agents import ToolCallingAgentV2
from agentloom.adapters.smolagents.checkpoint_codec import (
    SmolagentsCheckpointCodec,
)
from agentloom.adapters.smolagents.model_turn_bridge import (
    MODEL_ITEMS_RAW_KEY,
    MODEL_RESPONSE_ID_RAW_KEY,
    SmolagentsModelTurnBridge,
)
from agentloom.adapters.smolagents.recoverable_errors import (
    is_recoverable_agent_error,
)
from agentloom.adapters.smolagents.runtime_adapter import (
    MODEL_ADAPTER_AUDIT_KEY,
    SmolagentsRuntimeAdapter,
)
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
from agentloom.runtime.error_recovery import RUNTIME_FEEDBACK_RAW_KEY
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
    ToolDefinition,
)
from agentloom.runtime.tool_protocol import ToolCallRecord
from smolagents.agents import (
    AgentError,
    AgentExecutionError,
    AgentGenerationError,
    AgentMaxStepsError,
    AgentParsingError,
    AgentToolCallError,
    AgentToolExecutionError,
)
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


def _binding(
    adapter: object | None = None,
    *,
    model_type: str = "test",
    model_id: str = "opaque-model",
) -> ModelTurnBinding:
    return ModelTurnBinding(
        model_type=model_type,
        model_id=model_id,
        adapter=adapter or _RecordingTurnAdapter(),  # type: ignore[arg-type]
    )


def _runtime(
    native: object,
    *,
    binding: ModelTurnBinding | None = None,
    **kwargs: object,
) -> SmolagentsRuntimeAdapter:
    return SmolagentsRuntimeAdapter(
        native,
        model_binding=binding or _binding(),
        **kwargs,
    )


def _audit(adapter_id: str = "openai_responses") -> dict[str, str]:
    return {MODEL_ADAPTER_AUDIT_KEY: adapter_id}


def test_action_step_projection_persists_runtime_error_feedback_marker() -> None:
    logger = SimpleNamespace(log_error=lambda _message: None)
    step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0),
        error=AgentParsingError(
            "malformed native tool arguments",
            logger,
        ),
    )

    first = action_step_to_protocol_messages(step)
    replayed = action_step_to_protocol_messages(step)

    assert len(first) == 1
    assert first[0].role == MessageRole.TOOL_RESPONSE
    assert first[0].raw == {RUNTIME_FEEDBACK_RAW_KEY: True}
    assert replayed[0].raw == first[0].raw


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
    runtime = _runtime(native)

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
    assert result.checkpoint.audit_metadata[MODEL_ADAPTER_AUDIT_KEY] == (
        "openai_responses"
    )
    assert result.checkpoint.payload["memory_steps"] == []
    assert result.checkpoint.payload["canonical_model_items"] == []


def test_adapter_emits_runtime_events_with_canonical_identity_and_typed_usage() -> None:
    native = _EventNativeRuntime()
    runtime = _runtime(native, binding=native.model.binding)
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


def test_adapter_event_sink_failure_is_visible_but_does_not_fail_run(
    monkeypatch,
) -> None:
    from agentloom.adapters.smolagents import runtime_adapter as adapter_module

    native = _NativeRuntime(_NativeResult(output="done"))
    runtime = _runtime(native)
    warnings: list[str] = []
    monkeypatch.setattr(
        adapter_module,
        "logger",
        SimpleNamespace(
            warning=lambda message, *args: warnings.append(message % args)
        ),
    )

    result = runtime.run(
        AgentRuntimeRequest(
            task="inspect",
            event_sink=lambda _event: (_ for _ in ()).throw(
                RuntimeError("observer failed")
            ),
        )
    )

    assert result.state == "success"
    assert [event.kind for event in result.events] == [
        "run",
        "checkpoint",
        "usage",
        "terminal",
    ]
    assert len(warnings) == len(result.events)
    assert all("observer failed" in warning for warning in warnings)


def test_adapter_classifies_provider_error_and_emits_terminal_failure() -> None:
    from litellm.exceptions import Timeout

    provider_error = Timeout(
        message="provider timeout",
        model="opaque-model",
        llm_provider="openai",
    )
    native = _NativeRuntime(_NativeResult(output=None))
    native.run = lambda **_kwargs: (_ for _ in ()).throw(provider_error)  # type: ignore[method-assign]
    runtime = _runtime(native)
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
    runtime = LimitedRuntimeAdapter(
        native,
        model_binding=_binding(),
    )
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
        runtime = _runtime(native)

        with pytest.raises(type(control_error)) as captured:
            runtime.run(AgentRuntimeRequest(task="inspect"))

        assert captured.value is control_error


def test_adapter_omits_empty_additional_args_and_resets_new_session() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    runtime = _runtime(native)

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
    runtime = _runtime(native)

    with pytest.raises(RuntimeError, match="error"):
        runtime.run(AgentRuntimeRequest(task="inspect"))


def test_adapter_preserves_max_steps_for_goal_owner_to_settle() -> None:
    native = _NativeRuntime(_NativeResult(output=None, state="max_steps_error"))
    runtime = _runtime(native)

    result = runtime.run(AgentRuntimeRequest(task="inspect"))

    assert result.state == "max_steps_error"


def test_adapter_closes_native_runtime_when_supported() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    runtime = _runtime(native)

    runtime.close()

    assert native.closed is True


def test_adapter_restores_native_memory_from_compatible_checkpoint() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    runtime = _runtime(native)
    task = TaskStep(task="prior task")
    checkpoint = RuntimeCheckpointEnvelope(
        runtime_id="smolagents",
        runtime_version=runtime.runtime_version,
        state_schema_version=runtime.state_schema_version,
        progress=1,
        audit_metadata=_audit(),
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


@pytest.mark.parametrize("record_task", [False, True])
def test_checkpoint_alone_resumes_history_through_the_real_agent_loop(record_task: bool) -> None:
    class FinalAnswerModel:
        adapter_id = "openai_chat"

        def __init__(self) -> None:
            self.requests: list[ModelTurnRequest] = []

        def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
            self.requests.append(request)
            return ModelTurnResult(
                items=(FunctionCallItem(
                    call_id=f"answer-{len(self.requests)}",
                    name="final_answer",
                    arguments_json='{"answer":"done"}',
                ),),
            )

    class FinalAnswerGateway:
        definitions = (ToolDefinition(
            name="final_answer",
            description="Finish the task.",
            parameters={
                "type": "object",
                "properties": {"answer": {"type": "string", "description": "Final answer."}},
                "required": ["answer"],
            },
        ),)

        def invoke(self, *, call_id, tool_name, arguments):
            return ToolCallRecord.completed(
                call_id=call_id, tool_name=tool_name, input=arguments, output=arguments["answer"],
            )

        def close(self) -> None:
            pass

    model = FinalAnswerModel()
    binding = _binding(model)

    def fresh_runtime() -> SmolagentsRuntimeAdapter:
        gateway = FinalAnswerGateway()
        native = ToolCallingAgentV2(
            tool_gateway=gateway,
            model=SmolagentsModelTurnBridge(binding=binding),
            max_steps=3,
            smart_summary=False,
            verbosity_level=0,
        )
        return _runtime(native, binding=binding, tool_gateway=gateway)

    original = fresh_runtime()
    checkpoint = original.run(AgentRuntimeRequest(task="Remember PRIOR_CONTEXT_SENTINEL")).checkpoint
    original.close()
    resumed = fresh_runtime()
    try:
        result = resumed.run(
            AgentRuntimeRequest(
                task="Continue with NEW_CONTEXT_SENTINEL",
                checkpoint=checkpoint,
                record_task=record_task,
            )
        )
    finally:
        resumed.close()

    messages = [item.text for item in model.requests[-1].items if isinstance(item, MessageItem)]
    assert any("PRIOR_CONTEXT_SENTINEL" in text for text in messages)
    assert any("NEW_CONTEXT_SENTINEL" in text for text in messages) is record_task
    assert any(
        isinstance(item, FunctionCallOutputItem) and item.call_id == "answer-1"
        for item in model.requests[-1].items
    )
    assert result.output == "done"


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
    runtime = _runtime(native)
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
                    audit_metadata=_audit(),
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


@pytest.mark.parametrize(
    "error_type",
    [
        AgentParsingError,
        AgentExecutionError,
        AgentToolCallError,
        AgentToolExecutionError,
    ],
)
def test_resume_replays_structured_call_error_feedback_before_next_model_turn(
    error_type: type[Exception],
) -> None:
    logger = SimpleNamespace(log_error=lambda _message: None)
    feedback_error = error_type(
        "malformed native tool arguments",
        logger,
    )
    source_native = _NativeRuntime(_NativeResult(output="done"))
    source_native.memory.steps = [
        TaskStep(task="recover the structured call"),
        ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0),
            error=feedback_error,
        ),
    ]
    source_runtime = _runtime(source_native)

    checkpoint = RuntimeCheckpointEnvelope.from_dict(
        json.loads(json.dumps(source_runtime.snapshot().to_dict()))
    )
    turn_adapter = _RecordingTurnAdapter()
    model = SmolagentsModelTurnBridge(
        binding=ModelTurnBinding(
            model_type="test",
            model_id="opaque-model",
            adapter=turn_adapter,
        )
    )
    target_native = _ReplayNativeRuntime(model)
    target_runtime = _runtime(target_native, binding=model.binding)

    target_runtime.run(
        AgentRuntimeRequest(
            task="continue",
            continue_session=True,
            checkpoint=checkpoint,
        )
    )

    feedback = (
        "Error:\n"
        "malformed native tool arguments\n"
        "Now let's retry: take care not to repeat previous errors! "
        "If you have retried several times, try a completely different approach.\n"
    )
    assert checkpoint.progress == 2
    assert turn_adapter.requests[0].items == (
        MessageItem(role="user", text="New task:\nrecover the structured call"),
        MessageItem(
            role="user",
            text=feedback,
            replay_payload={RUNTIME_FEEDBACK_RAW_KEY: True},
        ),
    )


def test_restore_rejects_non_parsing_runtime_error_metadata() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    runtime = _runtime(native)
    checkpoint = RuntimeCheckpointEnvelope(
        runtime_id="smolagents",
        runtime_version=runtime.runtime_version,
        state_schema_version=runtime.state_schema_version,
        progress=1,
        audit_metadata=_audit(),
        payload={
            "memory_steps": [
                {
                    "_step_type": "ActionStep",
                    "step_number": 1,
                    "timing": {"start_time": 0.0, "end_time": 1.0},
                    "tool_calls": [],
                    "error": {
                        "type": "ArbitraryRuntimeError",
                        "message": "must not be reconstructed",
                    },
                    "model_output_message": None,
                    "model_output": None,
                    "observations": None,
                    "action_output": None,
                    "token_usage": None,
                    "is_final_answer": False,
                }
            ],
            "canonical_model_items": [],
        },
    )

    with pytest.raises(
        ValueError,
        match="unsupported resumable smolagents error type",
    ):
        runtime.restore(checkpoint)


@pytest.mark.parametrize(
    "error_type",
    [
        AgentError,
        AgentGenerationError,
        AgentMaxStepsError,
    ],
)
def test_terminal_or_infrastructure_agent_errors_are_not_replay_feedback(
    error_type: type[Exception],
) -> None:
    logger = SimpleNamespace(log_error=lambda _message: None)

    assert is_recoverable_agent_error(error_type("stop", logger)) is False


def test_adapter_pushes_runtime_checkpoint_to_sink() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    native.memory.steps = [TaskStep(task="done")]
    runtime = _runtime(native)
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
    _runtime(native, checkpoint_sink=observed.append)

    assert len(native.step_callbacks.callbacks) == 1
    completed = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0),
        model_output_message=ChatMessage(
            role=MessageRole.ASSISTANT,
            content="done",
            raw={
                MODEL_ITEMS_RAW_KEY: (
                    MessageItem(role="assistant", text="done"),
                ),
            },
        ),
    )
    native.step_callbacks.callbacks[0](completed, agent=native)

    assert len(observed) == 1
    assert observed[0].payload["memory_steps"][0]["_step_type"] == "ActionStep"


def test_adapter_does_not_checkpoint_action_step_aborted_before_model_turn() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    native.memory.steps = [TaskStep(task="resume-safe task")]
    observed: list[RuntimeCheckpointEnvelope] = []
    _runtime(native, checkpoint_sink=observed.append)

    aborted = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0),
    )
    native.step_callbacks.callbacks[0](aborted, agent=native)

    assert observed == []


def test_adapter_snapshot_excludes_synthetic_step_without_model_turn() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    native.memory.steps = [
        TaskStep(task="resume-safe task"),
        ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0),
            action_output="synthetic max-steps answer",
        ),
    ]
    runtime = _runtime(native)

    checkpoint = runtime.snapshot()

    assert checkpoint.progress == 1
    assert [
        step["_step_type"] for step in checkpoint.payload["memory_steps"]
    ] == ["TaskStep"]
    restored = _NativeRuntime(_NativeResult(output="done"))
    _runtime(restored).restore(checkpoint)
    assert [type(step).__name__ for step in restored.memory.steps] == [
        "TaskStep"
    ]


def test_adapter_snapshot_and_restore_own_native_memory() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    native.memory.steps = [TaskStep(task="worker task")]
    runtime = _runtime(native)

    checkpoint = runtime.snapshot()
    native.memory.steps = []

    runtime.restore(checkpoint)

    assert [step.task for step in native.memory.steps] == ["worker task"]


def test_adapter_rejects_cross_protocol_checkpoint_before_memory_or_model_call() -> None:
    native = _NativeRuntime(_NativeResult(output="must not run"))
    runtime = _runtime(
        native,
        binding=_binding(
            adapter=_EventTurnAdapter(),
            model_type="responses",
            model_id="responses-deployment",
        ),
    )
    checkpoint = RuntimeCheckpointEnvelope(
        runtime_id="smolagents",
        runtime_version=runtime.runtime_version,
        state_schema_version=runtime.state_schema_version,
        progress=1,
        audit_metadata=_audit("openai_chat"),
        payload={
            # Deliberately corrupt: adapter mismatch must win before decoding.
            "memory_steps": ["not-a-memory-step"],
            "canonical_model_items": "not-a-canonical-stream",
        },
    )

    with pytest.raises(
        AgentRuntimeError,
        match="openai_chat.*openai_responses",
    ) as captured:
        runtime.run(
            AgentRuntimeRequest(
                task="continue",
                checkpoint=checkpoint,
            )
        )

    assert captured.value.category == "configuration"
    assert native.calls == []
    assert native.memory.steps == []


def test_adapter_rejects_schema2_checkpoint_without_model_adapter_identity() -> None:
    runtime = _runtime(_NativeRuntime(_NativeResult(output="must not run")))
    checkpoint = RuntimeCheckpointEnvelope(
        runtime_id="smolagents",
        runtime_version=runtime.runtime_version,
        state_schema_version=runtime.state_schema_version,
        payload={
            "memory_steps": [],
            "canonical_model_items": [],
        },
    )

    with pytest.raises(
        AgentRuntimeError,
        match="missing required model adapter identity",
    ) as captured:
        runtime.restore(checkpoint)

    assert captured.value.category == "configuration"


def test_adapter_allows_same_protocol_with_different_opaque_model_identity() -> None:
    source = _runtime(
        _NativeRuntime(_NativeResult(output="source")),
        binding=_binding(
            model_type="source-profile",
            model_id="provider/source-deployment",
        ),
    )
    checkpoint = source.snapshot()
    target_native = _NativeRuntime(_NativeResult(output="target"))
    target = _runtime(
        target_native,
        binding=_binding(
            model_type="target-profile",
            model_id="provider/replacement-deployment",
        ),
    )

    target.restore(checkpoint)

    assert target_native.memory.steps == []


def test_adapter_rejects_old_schema_and_runtime_version() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    runtime = _runtime(native)

    with pytest.raises(ValueError, match="schema 1"):
        runtime.restore(
            RuntimeCheckpointEnvelope(
                runtime_id="smolagents",
                runtime_version=runtime.runtime_version,
                state_schema_version=1,
                audit_metadata=_audit(),
                payload={"memory_steps": []},
            )
        )
    with pytest.raises(ValueError, match="runtime version"):
        runtime.restore(
            RuntimeCheckpointEnvelope(
                runtime_id="smolagents",
                runtime_version="different-version",
                state_schema_version=2,
                audit_metadata=_audit(),
                payload={
                    "memory_steps": [],
                    "canonical_model_items": [],
                },
            )
        )


def test_adapter_rejects_corrupt_call_output_linkage() -> None:
    native = _NativeRuntime(_NativeResult(output="done"))
    runtime = _runtime(native)
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
        audit_metadata=_audit(),
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
