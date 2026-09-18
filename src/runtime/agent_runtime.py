"""Runtime-neutral contracts for executing one Agent invocation."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol

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


RuntimeFactory = Callable[[], AgentRuntime]


class RuntimeRegistry:
    """Explicit registry for complete Agent runtime adapters."""

    def __init__(self) -> None:
        self._factories: dict[str, RuntimeFactory] = {}

    def register(self, runtime_id: str, factory: RuntimeFactory) -> None:
        if not runtime_id:
            raise ValueError("runtime_id must be non-empty")
        if runtime_id in self._factories:
            raise ValueError(f"Agent runtime '{runtime_id}' is already registered")
        self._factories[runtime_id] = factory

    def create(self, runtime_id: str) -> AgentRuntime:
        factory = self._factories.get(runtime_id)
        if factory is None:
            available = ", ".join(sorted(self._factories)) or "(none)"
            raise UnsupportedRuntimeError(
                f"Agent runtime '{runtime_id}' is not registered; available runtimes: {available}"
            )
        return factory()


def build_builtin_runtime_registry(
    *,
    smolagents_factory: RuntimeFactory,
) -> RuntimeRegistry:
    """Build the production registry for the runtimes shipped in this stage.

    Runtime-specific construction stays in the injected factory. The
    runtime-neutral registry owns only explicit selection and never supplies a
    default or compatibility fallback.
    """

    registry = RuntimeRegistry()
    registry.register("smolagents", smolagents_factory)
    return registry
