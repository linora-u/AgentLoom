from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from agentloom.runtime.agent_runtime import (
    AgentRuntimeError,
    AgentRuntimeRequest,
    AgentRuntimeResult,
    RuntimeCapabilities,
    RuntimeCheckpointEnvelope,
    RuntimeDefinition,
    RuntimeRegistry,
    RuntimeRequirements,
    UnsupportedRuntimeError,
    build_builtin_runtime_registry,
    require_runtime_state,
)
from agentloom.runtime.model_binding import ModelTurnBinding
from agentloom.runtime.model_protocol import (
    MessageItem,
    ModelTurnRequest,
    ModelTurnResult,
    ToolDefinition,
)
from agentloom.runtime.tool_gateway import ToolGateway
from agentloom.runtime.tool_protocol import ToolCallRecord


class _ModelAdapter:
    adapter_id = "openai_chat"

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
        return ModelTurnResult(
            items=(MessageItem(role="assistant", text=request.model),)
        )


class _ToolGateway:
    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return ()

    def invoke(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> ToolCallRecord:
        return ToolCallRecord.completed(
            call_id=call_id,
            tool_name=tool_name,
            input=dict(arguments),
            output="done",
        )

    def close(self) -> None:
        return None


def _definition(runtime_id: str) -> RuntimeDefinition:
    gateway = _ToolGateway()
    assert isinstance(gateway, ToolGateway)
    return RuntimeDefinition(
        runtime_id=runtime_id,
        name="runtime-contract",
        description="Exercise one registered runtime.",
        model=ModelTurnBinding(
            model_type="test",
            model_id="opaque-model",
            adapter=_ModelAdapter(),
        ),
        tool_gateway=gateway,
        max_steps=5,
    )


@dataclass
class _RecordingRuntime:
    runtime_id: str
    requests: list[AgentRuntimeRequest]

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            structured_tools=True,
            parallel_tools=True,
            checkpoint_resume=True,
            subagents=True,
        )

    def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResult:
        self.requests.append(request)
        return AgentRuntimeResult(
            state="success",
            output="done",
            checkpoint=RuntimeCheckpointEnvelope(
                runtime_id=self.runtime_id,
                runtime_version="test",
                state_schema_version=1,
                payload={"turn": 1},
            ),
        )

    def close(self) -> None:
        return None


def test_registry_resolves_a_complete_runtime_without_exposing_native_types() -> None:
    requests: list[AgentRuntimeRequest] = []
    definitions: list[RuntimeDefinition] = []
    registry = RuntimeRegistry()

    def factory(definition: RuntimeDefinition) -> _RecordingRuntime:
        definitions.append(definition)
        return _RecordingRuntime("smolagents", requests)

    registry.register(
        "smolagents",
        capabilities=RuntimeCapabilities(True, True, True, True),
        factory=factory,
    )
    definition = _definition("smolagents")

    runtime = registry.create(definition)
    result = runtime.run(
        AgentRuntimeRequest(
            task="inspect the project",
            continue_session=False,
            additional_args={"scope": "runtime"},
        )
    )

    assert result.state == "success"
    assert result.output == "done"
    assert result.checkpoint is not None
    assert result.checkpoint.payload == {"turn": 1}
    assert requests[0].task == "inspect the project"
    assert requests[0].additional_args == {"scope": "runtime"}
    assert definitions == [definition]
    assert definitions[0] is definition


def test_registry_rejects_unknown_runtime_without_fallback() -> None:
    registry = RuntimeRegistry()
    registry.register(
        "smolagents",
        capabilities=RuntimeCapabilities(True, True, True, True),
        factory=lambda _definition: _RecordingRuntime("smolagents", []),
    )

    with pytest.raises(UnsupportedRuntimeError, match="langgraph"):
        registry.create(_definition("langgraph"))


def test_builtin_registry_registers_only_explicit_smolagents_factory() -> None:
    requests: list[AgentRuntimeRequest] = []
    created: list[str] = []

    def build_smolagents(
        _definition: RuntimeDefinition,
    ) -> _RecordingRuntime:
        created.append("smolagents")
        return _RecordingRuntime("smolagents", requests)

    registry = build_builtin_runtime_registry(
        smolagents_factory=build_smolagents,
    )

    runtime = registry.create(_definition("smolagents"))

    assert runtime.runtime_id == "smolagents"
    assert created == ["smolagents"]
    with pytest.raises(UnsupportedRuntimeError, match="langgraph"):
        registry.create(_definition("langgraph"))
    assert created == ["smolagents"]


def test_registry_validates_capabilities_without_constructing_runtime() -> None:
    registry = RuntimeRegistry()
    registry.register(
        "minimal",
        capabilities=RuntimeCapabilities(
            structured_tools=True,
            parallel_tools=False,
            checkpoint_resume=False,
            subagents=False,
        ),
    )

    registration = registry.validate(
        "minimal",
        requirements=RuntimeRequirements(structured_tools=True),
    )

    assert registry.runtime_ids == ("minimal",)
    assert registration.runtime_id == "minimal"
    with pytest.raises(
        UnsupportedRuntimeError,
        match="checkpoint_resume, subagents",
    ):
        registry.validate(
            "minimal",
            requirements=RuntimeRequirements(
                structured_tools=True,
                checkpoint_resume=True,
                subagents=True,
            ),
        )
    with pytest.raises(UnsupportedRuntimeError, match="no runtime factory"):
        registry.create(_definition("minimal"))


def test_runtime_definition_requires_neutral_model_and_tool_gateway() -> None:
    valid = _definition("test")

    assert isinstance(valid.model, ModelTurnBinding)
    assert isinstance(valid.tool_gateway, ToolGateway)
    with pytest.raises(TypeError, match="ModelTurnBinding"):
        RuntimeDefinition(
            runtime_id="test",
            name="invalid-model",
            description="Do not accept an arbitrary model.",
            model=object(),  # type: ignore[arg-type]
            tool_gateway=valid.tool_gateway,
            max_steps=5,
        )
    with pytest.raises(TypeError, match="ToolGateway"):
        RuntimeDefinition(
            runtime_id="test",
            name="invalid-gateway",
            description="Do not accept a tuple of arbitrary tools.",
            model=valid.model,
            tool_gateway=(),  # type: ignore[arg-type]
            max_steps=5,
        )
    with pytest.raises(TypeError):
        RuntimeDefinition(  # type: ignore[call-arg]
            runtime_id="test",
            name="legacy-tools",
            description="A tools tuple is not part of the interface.",
            model=valid.model,
            tool_gateway=valid.tool_gateway,
            tools=(),
            max_steps=5,
        )


def test_runtime_definition_freezes_json_safe_metadata() -> None:
    original = {"labels": ["proof"]}
    definition = RuntimeDefinition(
        runtime_id="test",
        name="metadata",
        description="Freeze caller-owned metadata.",
        model=_definition("test").model,
        tool_gateway=_definition("test").tool_gateway,
        max_steps=3,
        planning_interval=2,
        smart_summary=False,
        todo_mode="off",
        metadata=original,
    )
    original["labels"].append("mutated")

    assert definition.metadata == {"labels": ["proof"]}
    with pytest.raises(ValueError, match="JSON serializable"):
        RuntimeDefinition(
            runtime_id="test",
            name="bad-metadata",
            description="Reject opaque metadata.",
            model=definition.model,
            tool_gateway=definition.tool_gateway,
            max_steps=3,
            metadata={"opaque": object()},
        )


def test_runtime_state_failure_uses_typed_runtime_error() -> None:
    result = AgentRuntimeResult(state="failed", output=None)

    with pytest.raises(AgentRuntimeError, match="runtime failed: failed"):
        require_runtime_state(
            result,
            allowed_states={"success"},
            error_prefix="runtime failed",
        )


def test_checkpoint_envelope_rejects_cross_runtime_resume() -> None:
    checkpoint = RuntimeCheckpointEnvelope(
        runtime_id="smolagents",
        runtime_version="1.26.0",
        state_schema_version=1,
        payload={"steps": []},
    )

    checkpoint.require_compatible(
        runtime_id="smolagents",
        state_schema_version=1,
    )
    with pytest.raises(UnsupportedRuntimeError, match="langgraph"):
        checkpoint.require_compatible(
            runtime_id="langgraph",
            state_schema_version=1,
        )


def test_checkpoint_envelope_requires_json_safe_payload() -> None:
    with pytest.raises(ValueError, match="JSON"):
        RuntimeCheckpointEnvelope(
            runtime_id="smolagents",
            runtime_version="1.26.0",
            state_schema_version=1,
            payload={"object": object()},
        )


def test_checkpoint_envelope_round_trips_through_json_shape() -> None:
    checkpoint = RuntimeCheckpointEnvelope(
        runtime_id="smolagents",
        runtime_version="1.26.0",
        state_schema_version=1,
        payload={"steps": [{"number": 1}]},
    )

    restored = RuntimeCheckpointEnvelope.from_dict(checkpoint.to_dict())

    assert restored == checkpoint
