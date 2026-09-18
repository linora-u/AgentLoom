"""Runtime-neutral contracts for executing one Agent invocation."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol, cast, runtime_checkable

from agentloom.runtime.model_binding import ModelTurnBinding
from agentloom.runtime.tool_gateway import ToolGateway

RuntimeState = Literal[
    "success",
    "max_steps_error",
    "interrupted",
    "failed",
    "budget_limited",
]
RUNTIME_STATES: tuple[RuntimeState, ...] = (
    "success",
    "max_steps_error",
    "interrupted",
    "failed",
    "budget_limited",
)

type JSONValue = (
    None
    | bool
    | int
    | float
    | str
    | list[JSONValue]
    | dict[str, JSONValue]
)
type RuntimeEventKind = Literal[
    "run",
    "model",
    "tool",
    "subagent",
    "usage",
    "checkpoint",
    "terminal",
]
RUNTIME_EVENT_KINDS: tuple[RuntimeEventKind, ...] = (
    "run",
    "model",
    "tool",
    "subagent",
    "usage",
    "checkpoint",
    "terminal",
)
type RuntimeErrorCategory = Literal[
    "configuration",
    "unsupported_capability",
    "provider",
    "tool",
    "interrupted",
    "budget_limited",
    "internal",
]
RUNTIME_ERROR_CATEGORIES: tuple[RuntimeErrorCategory, ...] = (
    "configuration",
    "unsupported_capability",
    "provider",
    "tool",
    "interrupted",
    "budget_limited",
    "internal",
)


def _copy_json_value(value: object, *, field_name: str) -> JSONValue:
    """Return a defensive JSON-native copy without coercing unsupported values."""

    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must contain finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, JSONValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"{field_name} must contain only string object keys"
                )
            normalized[key] = _copy_json_value(
                child,
                field_name=f"{field_name}.{key}",
            )
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            _copy_json_value(child, field_name=f"{field_name}[{index}]")
            for index, child in enumerate(value)
        ]
    raise ValueError(
        f"{field_name} must be JSON serializable, got {type(value).__name__}"
    )


def _frozen_json_mapping(
    value: Mapping[str, object],
    *,
    field_name: str,
) -> Mapping[str, JSONValue]:
    normalized = _copy_json_value(value, field_name=field_name)
    if not isinstance(normalized, dict):
        raise TypeError(f"{field_name} must be a mapping")
    return MappingProxyType(normalized)


def _optional_identity(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string when provided")
    return value.strip()


def _non_negative_integer(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _optional_non_empty_string(
    value: str | None,
    *,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string when provided")
    return value.strip()


class UnsupportedRuntimeError(ValueError):
    """A requested Agent runtime or checkpoint contract is unsupported."""


class AgentRuntimeError(RuntimeError):
    """A concrete Agent runtime failed to satisfy the execution contract."""

    def __init__(
        self,
        message: str,
        *,
        category: RuntimeErrorCategory = "internal",
        cause: BaseException | None = None,
        retryable: bool = False,
    ) -> None:
        if category not in RUNTIME_ERROR_CATEGORIES:
            raise ValueError(f"unsupported runtime error category: {category!r}")
        if cause is not None and not isinstance(cause, BaseException):
            raise TypeError("runtime error cause must be an exception when provided")
        if not isinstance(retryable, bool):
            raise TypeError("runtime error retryable must be a boolean")
        super().__init__(message)
        self.category = category
        self.cause = cause
        self.retryable = retryable
        if cause is not None:
            self.__cause__ = cause


def require_runtime_state(
    result: AgentRuntimeResult | object,
    *,
    allowed_states: set[str],
    error_prefix: str,
) -> None:
    state = str(getattr(result, "state", "") or "")
    if state not in allowed_states:
        category: RuntimeErrorCategory = "internal"
        if state == "interrupted":
            category = "interrupted"
        elif state == "budget_limited":
            category = "budget_limited"
        raise AgentRuntimeError(
            f"{error_prefix}: {state or 'missing_runtime_state'}",
            category=category,
        )


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    """Semantic capabilities exposed by one Agent runtime adapter."""

    structured_tools: bool
    parallel_tools: bool
    checkpoint_resume: bool
    subagents: bool


@dataclass(frozen=True, slots=True)
class RuntimeRequirements:
    """Semantic features an Application requires from its selected runtime."""

    structured_tools: bool = True
    parallel_tools: bool = False
    checkpoint_resume: bool = False
    subagents: bool = False

    def unsupported_by(
        self,
        capabilities: RuntimeCapabilities,
    ) -> tuple[str, ...]:
        return tuple(
            name
            for name in (
                "structured_tools",
                "parallel_tools",
                "checkpoint_resume",
                "subagents",
            )
            if getattr(self, name) and not getattr(capabilities, name)
        )


@dataclass(frozen=True, slots=True)
class RuntimeUsage:
    """Provider-neutral token accounting for one complete runtime execution."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    details: Mapping[str, JSONValue] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for field_name in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
        ):
            object.__setattr__(
                self,
                field_name,
                _non_negative_integer(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        object.__setattr__(
            self,
            "details",
            _frozen_json_mapping(self.details, field_name="runtime usage details"),
        )

    @classmethod
    def from_value(cls, value: object) -> RuntimeUsage:
        """Normalize legacy/native usage shapes at the runtime boundary."""

        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            raw = _frozen_json_mapping(value, field_name="runtime usage")

            def read(*names: str) -> int | None:
                for name in names:
                    if name in raw and raw[name] is not None:
                        return _non_negative_integer(
                            raw[name],
                            field_name=f"runtime usage {name}",
                        )
                return None

            input_tokens = read("input_tokens", "prompt_tokens", "input") or 0
            output_tokens = read(
                "output_tokens",
                "completion_tokens",
                "output",
            ) or 0
            total_tokens = read("total_tokens", "total")
            details_value = raw.get("details", {})
            if not isinstance(details_value, Mapping):
                raise ValueError("runtime usage details must be a mapping")
            return cls(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=(
                    input_tokens + output_tokens
                    if total_tokens is None
                    else total_tokens
                ),
                cached_input_tokens=read(
                    "cached_input_tokens",
                    "cache_read_input_tokens",
                )
                or 0,
                reasoning_tokens=read("reasoning_tokens") or 0,
                details=cast(Mapping[str, JSONValue], details_value),
            )

        input_value = getattr(value, "input_tokens", None)
        output_value = getattr(value, "output_tokens", None)
        if input_value is None and output_value is None:
            raise TypeError(
                "runtime usage must be RuntimeUsage, a mapping, or expose "
                "input_tokens/output_tokens"
            )
        input_tokens = _non_negative_integer(
            input_value or 0,
            field_name="runtime usage input_tokens",
        )
        output_tokens = _non_negative_integer(
            output_value or 0,
            field_name="runtime usage output_tokens",
        )
        raw_total = getattr(value, "total_tokens", None)
        total_tokens = (
            input_tokens + output_tokens
            if raw_total is None
            else _non_negative_integer(
                raw_total,
                field_name="runtime usage total_tokens",
            )
        )
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_input_tokens=_non_negative_integer(
                getattr(value, "cached_input_tokens", 0),
                field_name="runtime usage cached_input_tokens",
            ),
            reasoning_tokens=_non_negative_integer(
                getattr(value, "reasoning_tokens", 0),
                field_name="runtime usage reasoning_tokens",
            ),
        )


@dataclass(frozen=True, slots=True)
class RuntimeArtifact:
    """A durable artifact produced by a runtime execution."""

    name: str
    kind: str
    uri: str | None = None
    path: str | None = None
    metadata: Mapping[str, JSONValue] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("runtime artifact name must be a non-empty string")
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("runtime artifact kind must be a non-empty string")
        uri = _optional_non_empty_string(self.uri, field_name="runtime artifact uri")
        path = _optional_non_empty_string(
            self.path,
            field_name="runtime artifact path",
        )
        if uri is None and path is None:
            raise ValueError("runtime artifact must define uri or path")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "kind", self.kind.strip())
        object.__setattr__(self, "uri", uri)
        object.__setattr__(self, "path", path)
        object.__setattr__(
            self,
            "metadata",
            _frozen_json_mapping(
                self.metadata,
                field_name="runtime artifact metadata",
            ),
        )


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """A runtime-neutral event emitted during one execution."""

    kind: RuntimeEventKind
    timestamp: float = field(default_factory=time.time)
    application_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    details: Mapping[str, JSONValue] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.kind not in RUNTIME_EVENT_KINDS:
            raise ValueError(f"unsupported runtime event kind: {self.kind!r}")
        if (
            isinstance(self.timestamp, bool)
            or not isinstance(self.timestamp, (int, float))
            or not math.isfinite(self.timestamp)
            or self.timestamp < 0
        ):
            raise ValueError("runtime event timestamp must be finite and non-negative")
        object.__setattr__(self, "timestamp", float(self.timestamp))
        for field_name in ("application_id", "task_id", "run_id"):
            object.__setattr__(
                self,
                field_name,
                _optional_identity(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        object.__setattr__(
            self,
            "details",
            _frozen_json_mapping(self.details, field_name="runtime event details"),
        )


@runtime_checkable
class RuntimeEventSink(Protocol):
    """Consumer for ordered runtime-neutral execution events."""

    def __call__(self, event: RuntimeEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimeDefinition:
    """Complete, runtime-neutral definition used to construct one Agent runtime."""

    runtime_id: str
    name: str
    description: str
    model: ModelTurnBinding
    tool_gateway: ToolGateway
    max_steps: int
    instructions: str = ""
    planning_interval: int | None = None
    smart_summary: bool = True
    todo_mode: Literal["auto", "on", "off"] = "auto"
    prompt_template_path: str | None = None
    project_root: str | None = None
    max_consecutive_model_errors: int = 5
    metadata: Mapping[str, JSONValue] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for field_name in ("runtime_id", "name", "description", "instructions"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise TypeError(f"{field_name} must be a string")
        if not self.runtime_id.strip():
            raise ValueError("runtime_id must be non-empty")
        if not self.name.strip():
            raise ValueError("name must be non-empty")
        if not isinstance(self.model, ModelTurnBinding):
            raise TypeError("model must be a ModelTurnBinding")
        if not isinstance(self.tool_gateway, ToolGateway):
            raise TypeError("tool_gateway must satisfy ToolGateway")
        if (
            isinstance(self.max_steps, bool)
            or not isinstance(self.max_steps, int)
            or self.max_steps < 1
        ):
            raise ValueError("max_steps must be a positive integer")
        if self.planning_interval is not None and (
            isinstance(self.planning_interval, bool)
            or not isinstance(self.planning_interval, int)
            or self.planning_interval < 1
        ):
            raise ValueError(
                "planning_interval must be a positive integer when provided"
            )
        if not isinstance(self.smart_summary, bool):
            raise TypeError("smart_summary must be a boolean")
        if self.todo_mode not in {"auto", "on", "off"}:
            raise ValueError("todo_mode must be one of: auto, on, off")
        for field_name in ("prompt_template_path", "project_root"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(
                    f"{field_name} must be a non-empty string when provided"
                )
            if isinstance(value, str):
                object.__setattr__(self, field_name, value.strip())
        if (
            isinstance(self.max_consecutive_model_errors, bool)
            or not isinstance(self.max_consecutive_model_errors, int)
            or self.max_consecutive_model_errors < 1
        ):
            raise ValueError(
                "max_consecutive_model_errors must be a positive integer"
            )
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "runtime_id", self.runtime_id.strip())
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(
            self,
            "metadata",
            _frozen_json_mapping(
                self.metadata,
                field_name="runtime definition metadata",
            ),
        )


@dataclass(frozen=True, slots=True)
class RuntimeCheckpointEnvelope:
    """Versioned, runtime-owned state stored inside AgentLoom checkpoints."""

    runtime_id: str
    runtime_version: str
    state_schema_version: int
    payload: Mapping[str, JSONValue]
    task_id: str | None = None
    run_id: str | None = None
    progress: int = 0
    audit_metadata: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.runtime_id:
            raise ValueError("runtime_id must be non-empty")
        if not self.runtime_version:
            raise ValueError("runtime_version must be non-empty")
        if (
            isinstance(self.state_schema_version, bool)
            or not isinstance(self.state_schema_version, int)
            or self.state_schema_version < 1
        ):
            raise ValueError("state_schema_version must be a positive integer")
        for name in ("task_id", "run_id"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"{name} must be a non-empty string when provided")
        if (
            isinstance(self.progress, bool)
            or not isinstance(self.progress, int)
            or self.progress < 0
        ):
            raise ValueError("progress must be a non-negative integer")
        object.__setattr__(
            self,
            "payload",
            _frozen_json_mapping(
                self.payload,
                field_name="runtime checkpoint payload",
            ),
        )
        object.__setattr__(
            self,
            "audit_metadata",
            _frozen_json_mapping(
                self.audit_metadata,
                field_name="runtime checkpoint audit_metadata",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "runtime_id": self.runtime_id,
            "runtime_version": self.runtime_version,
            "state_schema_version": self.state_schema_version,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "progress": self.progress,
            "audit_metadata": dict(self.audit_metadata),
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RuntimeCheckpointEnvelope:
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("runtime checkpoint payload must be a mapping")
        audit_metadata = value.get("audit_metadata") or {}
        if not isinstance(audit_metadata, Mapping):
            raise ValueError("runtime checkpoint audit_metadata must be a mapping")
        return cls(
            runtime_id=str(value.get("runtime_id") or ""),
            runtime_version=str(value.get("runtime_version") or ""),
            state_schema_version=cast(
                int,
                value.get("state_schema_version", 0),
            ),
            task_id=cast(str | None, value.get("task_id")),
            run_id=cast(str | None, value.get("run_id")),
            progress=cast(int, value.get("progress", 0)),
            audit_metadata=cast(Mapping[str, JSONValue], audit_metadata),
            payload=cast(Mapping[str, JSONValue], payload),
        )

    def require_compatible(
        self,
        *,
        runtime_id: str,
        state_schema_version: int,
        runtime_version: str | None = None,
    ) -> None:
        if self.runtime_id != runtime_id:
            raise UnsupportedRuntimeError(
                f"Checkpoint runtime '{self.runtime_id}' cannot resume with runtime '{runtime_id}'"
            )
        if self.state_schema_version != state_schema_version:
            raise UnsupportedRuntimeError(
                "Checkpoint state schema "
                f"{self.state_schema_version} is incompatible with {state_schema_version}"
            )
        if (
            runtime_version is not None
            and self.runtime_version != runtime_version
        ):
            raise UnsupportedRuntimeError(
                "Checkpoint runtime version "
                f"{self.runtime_version!r} is incompatible with {runtime_version!r}"
            )


@dataclass(frozen=True, slots=True)
class AgentRuntimeRequest:
    """Input for one complete Agent runtime execution."""

    task: str
    application_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    requirements: RuntimeRequirements = field(default_factory=RuntimeRequirements)
    event_sink: RuntimeEventSink | None = None
    continue_session: bool = False
    record_task: bool = True
    additional_args: Mapping[str, JSONValue] = field(default_factory=dict)
    checkpoint: RuntimeCheckpointEnvelope | None = None
    checkpoint_sink: Callable[[RuntimeCheckpointEnvelope], None] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task, str) or not self.task:
            raise ValueError("runtime task must be a non-empty string")
        for field_name in ("application_id", "task_id", "run_id"):
            object.__setattr__(
                self,
                field_name,
                _optional_identity(
                    getattr(self, field_name),
                    field_name=field_name,
                ),
            )
        if not isinstance(self.requirements, RuntimeRequirements):
            raise TypeError("requirements must be RuntimeRequirements")
        if self.event_sink is not None and not callable(self.event_sink):
            raise TypeError("event_sink must be callable when provided")
        if not isinstance(self.continue_session, bool):
            raise TypeError("continue_session must be a boolean")
        if not isinstance(self.record_task, bool):
            raise TypeError("record_task must be a boolean")
        if self.checkpoint is not None and not isinstance(
            self.checkpoint,
            RuntimeCheckpointEnvelope,
        ):
            raise TypeError("checkpoint must be RuntimeCheckpointEnvelope")
        if self.checkpoint_sink is not None and not callable(self.checkpoint_sink):
            raise TypeError("checkpoint_sink must be callable when provided")
        object.__setattr__(
            self,
            "additional_args",
            _frozen_json_mapping(
                self.additional_args,
                field_name="runtime request additional_args",
            ),
        )


@dataclass(frozen=True, slots=True)
class AgentRuntimeResult:
    """Runtime-neutral result from one complete Agent execution."""

    state: RuntimeState
    output: JSONValue
    usage: RuntimeUsage = field(default_factory=RuntimeUsage)
    artifacts: tuple[RuntimeArtifact, ...] = ()
    events: tuple[RuntimeEvent, ...] = ()
    checkpoint: RuntimeCheckpointEnvelope | None = None

    def __post_init__(self) -> None:
        if self.state not in RUNTIME_STATES:
            raise ValueError(f"unsupported runtime state: {self.state!r}")
        object.__setattr__(
            self,
            "output",
            _copy_json_value(self.output, field_name="runtime result output"),
        )
        object.__setattr__(self, "usage", RuntimeUsage.from_value(self.usage))
        artifacts = tuple(self.artifacts)
        if not all(isinstance(artifact, RuntimeArtifact) for artifact in artifacts):
            raise TypeError("runtime result artifacts must contain RuntimeArtifact")
        events = tuple(self.events)
        if not all(isinstance(event, RuntimeEvent) for event in events):
            raise TypeError("runtime result events must contain RuntimeEvent")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "events", events)
        if self.checkpoint is not None and not isinstance(
            self.checkpoint,
            RuntimeCheckpointEnvelope,
        ):
            raise TypeError("runtime result checkpoint must be RuntimeCheckpointEnvelope")


class AgentRuntime(Protocol):
    """Deep interface implemented by concrete Agent runtime adapters."""

    runtime_id: str

    @property
    def capabilities(self) -> RuntimeCapabilities: ...

    def snapshot(self) -> RuntimeCheckpointEnvelope: ...

    def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResult: ...

    def close(self) -> None: ...


RuntimeFactory = Callable[[RuntimeDefinition], AgentRuntime]


@dataclass(frozen=True, slots=True)
class RuntimeRegistration:
    """One registered runtime's static contract and optional constructor."""

    runtime_id: str
    capabilities: RuntimeCapabilities
    factory: RuntimeFactory | None = None


SMOLAGENTS_CAPABILITIES = RuntimeCapabilities(
    structured_tools=True,
    parallel_tools=True,
    checkpoint_resume=True,
    subagents=True,
)


class RuntimeRegistry:
    """Explicit registry for complete Agent runtime adapters."""

    def __init__(self) -> None:
        self._registrations: dict[str, RuntimeRegistration] = {}

    @property
    def runtime_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._registrations))

    def register(
        self,
        runtime_id: str,
        *,
        capabilities: RuntimeCapabilities,
        factory: RuntimeFactory | None = None,
    ) -> None:
        if not runtime_id:
            raise ValueError("runtime_id must be non-empty")
        if runtime_id in self._registrations:
            raise ValueError(f"Agent runtime '{runtime_id}' is already registered")
        self._registrations[runtime_id] = RuntimeRegistration(
            runtime_id=runtime_id,
            capabilities=capabilities,
            factory=factory,
        )

    def validate(
        self,
        runtime_id: object,
        *,
        requirements: RuntimeRequirements | None = None,
    ) -> RuntimeRegistration:
        registration = (
            self._registrations.get(runtime_id)
            if isinstance(runtime_id, str)
            else None
        )
        if registration is None:
            available = ", ".join(self.runtime_ids) or "(none)"
            raise UnsupportedRuntimeError(
                f"agent_runtime {runtime_id!r} is not registered; "
                f"available runtimes: {available}"
            )
        missing = (requirements or RuntimeRequirements()).unsupported_by(
            registration.capabilities
        )
        if missing:
            raise UnsupportedRuntimeError(
                f"Agent runtime '{runtime_id}' does not support required "
                f"capabilities: {', '.join(missing)}"
            )
        return registration

    def create(self, definition: RuntimeDefinition) -> AgentRuntime:
        if not isinstance(definition, RuntimeDefinition):
            raise TypeError("runtime definition must be a RuntimeDefinition")
        registration = self.validate(definition.runtime_id)
        if registration.factory is None:
            raise UnsupportedRuntimeError(
                f"Agent runtime '{definition.runtime_id}' is registered for validation "
                "but no runtime factory was provided"
            )
        runtime = registration.factory(definition)
        contract_error: AgentRuntimeError | None = None
        try:
            actual_runtime_id = runtime.runtime_id
            actual_capabilities = runtime.capabilities
        except Exception as exc:
            contract_error = AgentRuntimeError(
                f"Agent runtime '{definition.runtime_id}' did not expose its "
                "registered contract",
                category="internal",
                cause=exc,
                retryable=False,
            )
        else:
            if actual_runtime_id != registration.runtime_id:
                contract_error = AgentRuntimeError(
                    f"Agent runtime factory registered as '{registration.runtime_id}' "
                    f"created runtime '{actual_runtime_id}'",
                    category="internal",
                    retryable=False,
                )
            elif actual_capabilities != registration.capabilities:
                contract_error = AgentRuntimeError(
                    f"Agent runtime '{registration.runtime_id}' capabilities do not "
                    "match its registration",
                    category="internal",
                    retryable=False,
                )
        if contract_error is None:
            return runtime
        try:
            runtime.close()
        except Exception as close_exc:
            contract_error.add_note(
                f"Runtime close after contract rejection also failed: {close_exc!r}"
            )
        raise contract_error


def build_builtin_runtime_registry(
    *,
    smolagents_factory: RuntimeFactory | None = None,
) -> RuntimeRegistry:
    """Build the production registry for the runtimes shipped in this stage.

    Runtime-specific construction stays in the injected factory. The
    runtime-neutral registry owns only explicit selection and never supplies a
    default or compatibility fallback.
    """

    if smolagents_factory is None:
        def smolagents_factory(
            definition: RuntimeDefinition,
        ) -> AgentRuntime:
            from agentloom.adapters.smolagents.runtime_factory import (
                SmolagentsRuntimeFactory,
            )

            return SmolagentsRuntimeFactory()(definition)

    registry = RuntimeRegistry()
    registry.register(
        "smolagents",
        capabilities=SMOLAGENTS_CAPABILITIES,
        factory=smolagents_factory,
    )
    return registry
