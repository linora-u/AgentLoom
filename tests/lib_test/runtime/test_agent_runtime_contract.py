from __future__ import annotations

from dataclasses import dataclass

import pytest
from agentloom.runtime.agent_runtime import (
    AgentRuntimeRequest,
    AgentRuntimeResult,
    RuntimeCapabilities,
    RuntimeCheckpointEnvelope,
    RuntimeRegistry,
    UnsupportedRuntimeError,
    build_builtin_runtime_registry,
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
    registry = RuntimeRegistry()
    registry.register("smolagents", lambda: _RecordingRuntime("smolagents", requests))

    runtime = registry.create("smolagents")
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


def test_registry_rejects_unknown_runtime_without_fallback() -> None:
    registry = RuntimeRegistry()
    registry.register("smolagents", lambda: _RecordingRuntime("smolagents", []))

    with pytest.raises(UnsupportedRuntimeError, match="langgraph"):
        registry.create("langgraph")


def test_builtin_registry_registers_only_explicit_smolagents_factory() -> None:
    requests: list[AgentRuntimeRequest] = []
    created: list[str] = []

    def build_smolagents() -> _RecordingRuntime:
        created.append("smolagents")
        return _RecordingRuntime("smolagents", requests)

    registry = build_builtin_runtime_registry(
        smolagents_factory=build_smolagents,
    )

    runtime = registry.create("smolagents")

    assert runtime.runtime_id == "smolagents"
    assert created == ["smolagents"]
    with pytest.raises(UnsupportedRuntimeError, match="langgraph"):
        registry.create("langgraph")
    assert created == ["smolagents"]


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
