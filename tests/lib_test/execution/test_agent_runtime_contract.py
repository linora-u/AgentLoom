from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

import pytest
from agentloom.application.composition import build_builtin_runtime_registry
from agentloom.execution.agent_runtime import (
    RUNTIME_EVENT_KINDS,
    AgentRuntimeError,
    AgentRuntimeRequest,
    AgentRuntimeResult,
    OutputContract,
    RuntimeArtifact,
    RuntimeCapabilities,
    RuntimeCheckpointEnvelope,
    RuntimeDefinition,
    RuntimeEvent,
    RuntimeEventSink,
    RuntimeRegistry,
    RuntimeRequirements,
    RuntimeUsage,
    UnsupportedRuntimeError,
    require_runtime_state,
)
from agentloom.execution.model_binding import ModelTurnBinding
from agentloom.execution.model_protocol import (
    MessageItem,
    ModelTurnRequest,
    ModelTurnResult,
    ToolDefinition,
)
from agentloom.execution.tool_gateway import ToolGateway
from agentloom.execution.tool_protocol import ToolCallRecord


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


def test_runtime_definition_exposes_selected_tool_manifest_and_keeps_legacy_gateway():
    from agentloom.execution.tool_gateway import AgentLoomToolGateway, bind_tool
    from agentloom.tools.loader import resolve_tool_function

    legacy = _definition("test")
    assert legacy.tool_manifest == ()
    gateway = AgentLoomToolGateway([bind_tool(resolve_tool_function("read_file"))])
    definition = replace(legacy, tool_gateway=gateway)
    (entry,) = definition.tool_manifest
    assert (entry.visible_name, entry.owner, entry.provider, entry.capability) == (
        "read_file", "runtime", "smolagents", "file.read",
    )
    assert entry.parameters == gateway.definitions[0].parameters

    class LegacySelectedGateway(_ToolGateway):
        definitions = gateway.definitions

    fallback = replace(legacy, tool_gateway=LegacySelectedGateway()).tool_manifest
    assert [(tool.visible_name, tool.owner, tool.provider) for tool in fallback] == [
        ("read_file", "external", "python"),
    ]


@pytest.mark.parametrize("mismatch", ["missing", "schema", "duplicate_definition"])
def test_runtime_definition_rejects_manifest_that_disagrees_with_selected_tools(mismatch):
    from agentloom.execution.tool_gateway import AgentLoomToolGateway, bind_tool
    from agentloom.tools.loader import resolve_tool_function

    gateway = AgentLoomToolGateway([bind_tool(resolve_tool_function("read_file"))])

    class InconsistentGateway(_ToolGateway):
        definitions = gateway.definitions * (2 if mismatch == "duplicate_definition" else 1)
        manifest = () if mismatch == "missing" else (
            replace(gateway.manifest[0], parameters={"type": "object", "properties": {}})
            if mismatch == "schema" else gateway.manifest[0],
        )

    with pytest.raises(ValueError, match="Tool manifest"):
        replace(_definition("test"), tool_gateway=InconsistentGateway())


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
        runtime_options={'max_steps': 5},
    )


def test_output_contract_validates_draft_2020_12_values_and_local_refs() -> None:
    contract = OutputContract(
        name="review_result",
        schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {
                "finding": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
            },
            "type": "array",
            "items": {"$ref": "#/$defs/finding"},
        },
    )

    assert contract.validate([{"message": "missing guard"}]) == [
        {"message": "missing guard"}
    ]
    with pytest.raises(ValueError, match="output does not satisfy"):
        contract.validate([{"message": 3}])


def test_output_contract_rejects_invalid_or_remote_schemas() -> None:
    with pytest.raises(ValueError, match="valid Draft 2020-12"):
        OutputContract(name="invalid", schema={"type": "not-a-json-type"})

    with pytest.raises(ValueError, match="remote"):
        OutputContract(
            name="remote",
            schema={"$ref": "https://schemas.example.test/result.json"},
        )


def test_runtime_definition_carries_an_immutable_output_contract() -> None:
    source_schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    contract = OutputContract(name="answer", schema=source_schema)
    definition = replace(_definition("test"), output_contract=contract)
    source_schema["properties"]["answer"]["type"] = "integer"

    assert definition.output_contract is contract
    assert definition.output_contract.schema["properties"]["answer"]["type"] == "string"


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
            goal=True,
            stop_hooks=True,
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
        capabilities=RuntimeCapabilities(True, True, True, True, goal=True, stop_hooks=True),
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
        capabilities=RuntimeCapabilities(True, True, True, True, goal=True, stop_hooks=True),
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


def test_registry_closes_runtime_whose_capabilities_drift_from_registration() -> None:
    @dataclass
    class MismatchedRuntime:
        runtime_id: str = "smolagents"
        close_calls: int = 0

        @property
        def capabilities(self) -> RuntimeCapabilities:
            return RuntimeCapabilities(True, False, True, True)

        def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResult:
            return AgentRuntimeResult(state="success", output=request.task)

        def snapshot(self) -> RuntimeCheckpointEnvelope:
            return RuntimeCheckpointEnvelope(
                runtime_id=self.runtime_id,
                runtime_version="test",
                state_schema_version=1,
                payload={},
            )

        def close(self) -> None:
            self.close_calls += 1

    runtime = MismatchedRuntime()
    registry = RuntimeRegistry()
    registry.register(
        "smolagents",
        capabilities=RuntimeCapabilities(True, True, True, True, goal=True, stop_hooks=True),
        factory=lambda _definition: runtime,
    )

    with pytest.raises(
        AgentRuntimeError,
        match="capabilities do not match",
    ) as error:
        registry.create(_definition("smolagents"))

    assert error.value.category == "internal"
    assert error.value.retryable is False
    assert runtime.close_calls == 1


def test_registry_closes_runtime_whose_identity_drifted_from_registration() -> None:
    @dataclass
    class MismatchedRuntime(_RecordingRuntime):
        close_calls: int = 0

        def close(self) -> None:
            self.close_calls += 1

    runtime = MismatchedRuntime("another-runtime", [])
    registry = RuntimeRegistry()
    registry.register(
        "smolagents",
        capabilities=RuntimeCapabilities(True, True, True, True, goal=True, stop_hooks=True),
        factory=lambda _definition: runtime,
    )

    with pytest.raises(AgentRuntimeError, match="another-runtime"):
        registry.create(_definition("smolagents"))

    assert runtime.close_calls == 1


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
            runtime_options={'max_steps': 5},
        )
    with pytest.raises(TypeError, match="ToolGateway"):
        RuntimeDefinition(
            runtime_id="test",
            name="invalid-gateway",
            description="Do not accept a tuple of arbitrary tools.",
            model=valid.model,
            tool_gateway=(),  # type: ignore[arg-type]
            runtime_options={'max_steps': 5},
        )
    with pytest.raises(TypeError):
        RuntimeDefinition(  # type: ignore[call-arg]
            runtime_id="test",
            name="legacy-tools",
            description="A tools tuple is not part of the interface.",
            model=valid.model,
            tool_gateway=valid.tool_gateway,
            tools=(),
            runtime_options={'max_steps': 5},
        )


def test_runtime_definition_freezes_json_safe_metadata() -> None:
    original = {"labels": ["proof"]}
    definition = RuntimeDefinition(
        runtime_id="test",
        name="metadata",
        description="Freeze caller-owned metadata.",
        model=_definition("test").model,
        tool_gateway=_definition("test").tool_gateway,
        runtime_options={'max_steps': 3, 'planning_interval': 2, 'smart_summary': False, 'todo_mode': "off"},
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
            runtime_options={'max_steps': 3},
            metadata={"opaque": object()},
        )


def test_runtime_definition_validates_typed_execution_context() -> None:
    valid = _definition("test")
    definition = RuntimeDefinition(
        runtime_id="test",
        name="typed-context",
        description="Expose shared runtime construction context.",
        model=valid.model,
        tool_gateway=valid.tool_gateway,
        runtime_options={'max_steps': 3, 'prompt_template_path': " prompts/custom.yaml ", 'max_consecutive_model_errors': 7},
        project_root=" /workspace ",
    )

    assert definition.runtime_options["prompt_template_path"] == " prompts/custom.yaml "
    assert definition.project_root == "/workspace"
    assert definition.runtime_options["max_consecutive_model_errors"] == 7
    with pytest.raises(ValueError, match="project_root"):
        RuntimeDefinition(
            runtime_id="test",
            name="bad-model-error-limit",
            description="Reject invalid typed execution context.",
            model=valid.model,
            tool_gateway=valid.tool_gateway,
            project_root=" ",
        )


@pytest.mark.parametrize(
    "invalid_metadata",
    [
        {1: "not-a-string-key"},
        {"number": float("nan")},
        {"number": float("inf")},
    ],
)
def test_runtime_definition_rejects_non_json_metadata(
    invalid_metadata: dict[object, object],
) -> None:
    valid = _definition("test")

    with pytest.raises(ValueError, match="JSON|string object keys|finite"):
        RuntimeDefinition(
            runtime_id="test",
            name="bad-metadata",
            description="Reject values outside the JSON contract.",
            model=valid.model,
            tool_gateway=valid.tool_gateway,
            runtime_options={'max_steps': 3},
            metadata=invalid_metadata,  # type: ignore[arg-type]
        )


def test_runtime_request_preserves_identities_requirements_and_event_sink() -> None:
    observed: list[RuntimeEvent] = []
    requirements = RuntimeRequirements(
        structured_tools=True,
        checkpoint_resume=True,
    )
    request = AgentRuntimeRequest(
        task="inspect",
        application_id=" application ",
        task_id=" task ",
        run_id=" run ",
        requirements=requirements,
        event_sink=observed.append,
    )
    event = RuntimeEvent(
        kind="run",
        application_id=request.application_id,
        task_id=request.task_id,
        run_id=request.run_id,
    )
    assert request.event_sink is not None
    request.event_sink(event)

    assert request.application_id == "application"
    assert request.task_id == "task"
    assert request.run_id == "run"
    assert request.requirements is requirements
    assert isinstance(request.event_sink, RuntimeEventSink)
    assert observed == [event]


def test_runtime_request_identity_defaults_are_temporarily_optional() -> None:
    # Production invocation must eventually populate these identities.
    request = AgentRuntimeRequest(task="inspect")

    assert request.application_id is None
    assert request.task_id is None
    assert request.run_id is None


@pytest.mark.parametrize("field_name", ["application_id", "task_id", "run_id"])
def test_runtime_request_rejects_blank_identity(field_name: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        AgentRuntimeRequest(task="inspect", **{field_name: " "})


def test_runtime_request_rejects_invalid_event_sink_and_json_arguments() -> None:
    with pytest.raises(TypeError, match="event_sink"):
        AgentRuntimeRequest(
            task="inspect",
            event_sink=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="JSON"):
        AgentRuntimeRequest(task="inspect", additional_args={"opaque": object()})


@pytest.mark.parametrize("kind", RUNTIME_EVENT_KINDS)
def test_runtime_event_vocabulary_and_identities(kind: str) -> None:
    event = RuntimeEvent(
        kind=kind,  # type: ignore[arg-type]
        timestamp=1.5,
        application_id="app",
        task_id="task",
        run_id="run",
        details={"step": 2},
    )

    assert event.kind == kind
    assert event.timestamp == 1.5
    assert event.application_id == "app"
    assert event.task_id == "task"
    assert event.run_id == "run"
    assert event.details == {"step": 2}


def test_runtime_event_requires_known_kind_timestamp_and_json_details() -> None:
    with pytest.raises(ValueError, match="event kind"):
        RuntimeEvent(kind="unknown")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timestamp"):
        RuntimeEvent(kind="run", timestamp=float("nan"))
    with pytest.raises(ValueError, match="JSON"):
        RuntimeEvent(kind="run", details={"opaque": object()})


def test_runtime_state_failure_uses_typed_runtime_error() -> None:
    result = AgentRuntimeResult(state="failed", output=None)

    with pytest.raises(AgentRuntimeError, match="runtime failed: failed"):
        require_runtime_state(
            result,
            allowed_states={"success"},
            error_prefix="runtime failed",
        )


def test_runtime_error_preserves_category_cause_and_retryability() -> None:
    cause = OSError("provider unavailable")
    error = AgentRuntimeError(
        "model turn failed",
        category="provider",
        cause=cause,
        retryable=True,
    )

    assert error.category == "provider"
    assert error.cause is cause
    assert error.__cause__ is cause
    assert error.retryable is True
    with pytest.raises(ValueError, match="category"):
        AgentRuntimeError(
            "bad category",
            category="unknown",  # type: ignore[arg-type]
        )


def test_runtime_result_normalizes_usage_and_defensively_copies_json() -> None:
    output = {"nested": [{"value": 1}]}
    usage_details = {"provider": {"cached": True}}
    usage = RuntimeUsage(
        input_tokens=2,
        output_tokens=3,
        total_tokens=5,
        details=usage_details,
    )
    artifact_metadata = {"labels": ["report"]}
    event_details = {"phase": ["start"]}
    artifact = RuntimeArtifact(
        name="report",
        kind="document",
        path="/tmp/report.md",
        metadata=artifact_metadata,
    )
    event = RuntimeEvent(kind="terminal", timestamp=1, details=event_details)
    artifacts = [artifact]
    events = [event]
    result = AgentRuntimeResult(
        state="success",
        output=output,
        usage=usage,
        artifacts=artifacts,  # type: ignore[arg-type]
        events=events,  # type: ignore[arg-type]
    )

    output["nested"][0]["value"] = 9
    usage_details["provider"]["cached"] = False
    artifact_metadata["labels"].append("mutated")
    event_details["phase"].append("mutated")
    artifacts.clear()
    events.clear()

    assert result.output == {"nested": [{"value": 1}]}
    assert result.usage.input_tokens == 2
    assert result.usage.details == {"provider": {"cached": True}}
    assert isinstance(result.artifacts, tuple)
    assert result.artifacts[0].metadata == {"labels": ["report"]}
    assert isinstance(result.events, tuple)
    assert result.events[0].details == {"phase": ["start"]}


def test_runtime_result_accepts_native_usage_shapes_at_adapter_boundary() -> None:
    result = AgentRuntimeResult(
        state="success",
        output=None,
        usage={"input": 2, "completion_tokens": 3},  # type: ignore[arg-type]
    )

    assert result.usage == RuntimeUsage(
        input_tokens=2,
        output_tokens=3,
        total_tokens=5,
    )


def test_runtime_result_rejects_invalid_state_json_and_entries() -> None:
    with pytest.raises(ValueError, match="runtime state"):
        AgentRuntimeResult(
            state="unknown",  # type: ignore[arg-type]
            output=None,
        )
    with pytest.raises(ValueError, match="JSON"):
        AgentRuntimeResult(state="success", output=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RuntimeArtifact"):
        AgentRuntimeResult(
            state="success",
            output=None,
            artifacts=(object(),),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="RuntimeEvent"):
        AgentRuntimeResult(
            state="success",
            output=None,
            events=(object(),),  # type: ignore[arg-type]
        )


def test_runtime_artifact_requires_location_and_json_metadata() -> None:
    with pytest.raises(ValueError, match="uri or path"):
        RuntimeArtifact(name="report", kind="document")
    with pytest.raises(ValueError, match="JSON"):
        RuntimeArtifact(
            name="report",
            kind="document",
            path="/tmp/report.md",
            metadata={"opaque": object()},
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
