"""Runtime-neutral contracts for executing one Agent invocation."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol

from agentloom.runtime.model_binding import ModelTurnBinding
from agentloom.runtime.tool_gateway import ToolGateway

RuntimeState = Literal[
    "success",
    "max_steps_error",
    "interrupted",
    "failed",
    "budget_limited",
]


class UnsupportedRuntimeError(ValueError):
    """A requested Agent runtime or checkpoint contract is unsupported."""


class AgentRuntimeError(RuntimeError):
    """A concrete Agent runtime failed to satisfy the execution contract."""


def require_runtime_state(
    result: AgentRuntimeResult | Any,
    *,
    allowed_states: set[str],
    error_prefix: str,
) -> None:
    state = str(getattr(result, "state", "") or "")
    if state not in allowed_states:
        raise AgentRuntimeError(f"{error_prefix}: {state or 'missing_runtime_state'}")


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
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)

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
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        try:
            normalized_metadata = json.loads(
                json.dumps(dict(self.metadata), ensure_ascii=False)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("runtime definition metadata must be JSON serializable") from exc
        object.__setattr__(self, "runtime_id", self.runtime_id.strip())
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(normalized_metadata),
        )


@dataclass(frozen=True, slots=True)
class RuntimeCheckpointEnvelope:
    """Versioned, runtime-owned state stored inside AgentLoom checkpoints."""

    runtime_id: str
    runtime_version: str
    state_schema_version: int
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.runtime_id:
            raise ValueError("runtime_id must be non-empty")
        if not self.runtime_version:
            raise ValueError("runtime_version must be non-empty")
        if isinstance(self.state_schema_version, bool) or self.state_schema_version < 1:
            raise ValueError("state_schema_version must be a positive integer")
        try:
            normalized = json.loads(json.dumps(dict(self.payload), ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("runtime checkpoint payload must be JSON serializable") from exc
        object.__setattr__(self, "payload", MappingProxyType(normalized))

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "runtime_version": self.runtime_version,
            "state_schema_version": self.state_schema_version,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeCheckpointEnvelope:
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("runtime checkpoint payload must be a mapping")
        return cls(
            runtime_id=str(value.get("runtime_id") or ""),
            runtime_version=str(value.get("runtime_version") or ""),
            state_schema_version=value.get("state_schema_version", 0),
            payload=payload,
        )

    def require_compatible(
        self,
        *,
        runtime_id: str,
        state_schema_version: int,
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


@dataclass(frozen=True, slots=True)
class AgentRuntimeRequest:
    """Input for one complete Agent runtime execution."""

    task: str
    continue_session: bool = False
    record_task: bool = True
    additional_args: Mapping[str, Any] = field(default_factory=dict)
    checkpoint: RuntimeCheckpointEnvelope | None = None
    checkpoint_sink: Callable[[RuntimeCheckpointEnvelope], None] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task, str) or not self.task:
            raise ValueError("runtime task must be a non-empty string")
        object.__setattr__(
            self,
            "additional_args",
            MappingProxyType(dict(self.additional_args)),
        )


@dataclass(frozen=True, slots=True)
class AgentRuntimeResult:
    """Runtime-neutral result from one complete Agent execution."""

    state: RuntimeState
    output: Any
    usage: Any | None = None
    checkpoint: RuntimeCheckpointEnvelope | None = None


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
        runtime_id: Any,
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
        return registration.factory(definition)


def build_builtin_runtime_registry(
    *,
    smolagents_factory: RuntimeFactory | None = None,
) -> RuntimeRegistry:
    """Build the production registry for the runtimes shipped in this stage.

    Runtime-specific construction stays in the injected factory. The
    runtime-neutral registry owns only explicit selection and never supplies a
    default or compatibility fallback.
    """

    registry = RuntimeRegistry()
    registry.register(
        "smolagents",
        capabilities=SMOLAGENTS_CAPABILITIES,
        factory=smolagents_factory,
    )
    return registry
