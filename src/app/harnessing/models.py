"""Immutable, JSON-serializable contracts at the complete Application boundary."""
from __future__ import annotations

import hashlib
import json
import math
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol


def json_value(value: Any) -> Any:
    """Return JSON values; reject non-finite numbers and live Python handles."""
    if hasattr(value, "to_dict"):
        return json_value(value.to_dict())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    return json.loads(json.dumps(value, allow_nan=False, ensure_ascii=False))


def frozen(value: Any) -> Any:
    value = json_value(value)
    if isinstance(value, dict):
        return MappingProxyType({key: frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(frozen(item) for item in value)
    return value


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(json_value(value), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class Serializable:
    def to_dict(self) -> dict[str, Any]:
        return {item.name: json_value(getattr(self, item.name)) for item in fields(self)}


@dataclass(frozen=True)
class ApplicationRequest(Serializable):
    application_id: str
    project_root: Path
    application_root: Path
    driver: str
    task: Any = None
    objective: str = ""
    input_paths: tuple[Path, ...] = ()
    hidden_paths: tuple[Path, ...] = ()
    driver_options: Mapping[str, Any] = field(default_factory=dict)
    harness_stream: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.application_id or ":" not in self.driver:
            raise ValueError("Application request requires identity and registered importable driver")
        object.__setattr__(self, "project_root", Path(self.project_root).resolve())
        object.__setattr__(self, "application_root", Path(self.application_root).resolve())
        if not self.application_root.is_relative_to(self.project_root / "applications"):
            raise ValueError("Application must be inside project applications/")
        for name in ("input_paths", "hidden_paths"):
            object.__setattr__(self, name, tuple(Path(os.path.abspath(Path(path).expanduser())) for path in getattr(self, name)))
        object.__setattr__(self, "task", frozen(self.task))
        object.__setattr__(self, "driver_options", frozen(self.driver_options))
        object.__setattr__(self, "harness_stream", frozen(self.harness_stream))

    @classmethod
    def from_dict(cls, value):
        return cls(**dict(value))


@dataclass(frozen=True)
class PreparedInvocation(Serializable):
    request: ApplicationRequest
    objective: str
    inputs: Mapping[str, Any]
    acceptance: Mapping[str, Any]
    data: Mapping[str, Any] = field(default_factory=dict)
    task_snapshot_id: str = ""

    def __post_init__(self):
        if not isinstance(self.request, ApplicationRequest):
            object.__setattr__(self, "request", ApplicationRequest.from_dict(self.request))
        if not self.objective.strip():
            raise ValueError("Prepared Application requires its original objective")
        for name in ("inputs", "acceptance", "data"):
            object.__setattr__(self, name, frozen(getattr(self, name)))
        logical_request = self.request.to_dict()
        for key in ("project_root", "application_root", "input_paths", "hidden_paths"):
            logical_request.pop(key, None)
        identity = fingerprint({"request": logical_request, "objective": self.objective,
            "inputs": self.inputs, "acceptance": self.acceptance, "data": self.data})
        if self.task_snapshot_id and self.task_snapshot_id != identity:
            raise ValueError("Prepared task snapshot content changed")
        object.__setattr__(self, "task_snapshot_id", identity)

    @classmethod
    def from_dict(cls, value):
        return cls(**dict(value))


@dataclass(frozen=True)
class TrialContext(Serializable):
    experiment_id: str
    trial_id: str
    candidate_id: str
    task_snapshot_id: str
    root: Path
    memory_snapshot: str = ""
    library_view: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self):
        for name in ("experiment_id", "trial_id", "candidate_id", "task_snapshot_id"):
            value = getattr(self, name)
            if not value or "/" in value or "\\" in value or value in {".", ".."}:
                raise ValueError(f"Invalid {name}")
        object.__setattr__(self, "root", Path(self.root).resolve())
        object.__setattr__(self, "library_view", tuple(frozen(item) for item in self.library_view))

    @property
    def workspace_root(self): return self.root / "workspace"
    @property
    def runtime_root(self): return self.root / "runtime"
    @property
    def business_root(self): return self.root / "business"
    @property
    def output_root(self): return self.root / "output"
    @property
    def session_root(self): return self.root / "sessions"

    @classmethod
    def from_dict(cls, value): return cls(**dict(value))


@dataclass(frozen=True)
class UsageObservation(Serializable):
    invocation_id: str
    attempt_id: str = ""
    sequence: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    complete: bool = False
    duration_seconds: float | None = None

    def __post_init__(self):
        if not self.invocation_id or type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("Usage requires invocation identity and nonnegative sequence")
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Token observations must be nonnegative integers or unknown")
        if type(self.complete) is not bool:
            raise ValueError("Usage completeness must be explicit")
        if self.duration_seconds is not None and (
            not math.isfinite(self.duration_seconds) or self.duration_seconds < 0
        ):
            raise ValueError("Usage duration must be finite and nonnegative")


@dataclass(frozen=True)
class ExecutionReceipt(Serializable):
    trial_id: str
    candidate_id: str
    status: str
    output: Any = None
    accepted: bool = False
    acceptance: Mapping[str, Any] = field(default_factory=dict)
    artifacts: tuple[Mapping[str, Any], ...] = ()
    runs: tuple[Mapping[str, Any], ...] = ()
    usage: tuple[UsageObservation, ...] = ()
    cleanup_complete: bool = False
    error: str = ""

    def __post_init__(self):
        if self.status not in {"completed", "failed", "cancelled", "incomplete"}:
            raise ValueError("Invalid execution receipt status")
        if type(self.accepted) is not bool or type(self.cleanup_complete) is not bool:
            raise ValueError("Acceptance and cleanup must be explicit booleans")
        if self.accepted and self.status != "completed":
            raise ValueError("Incomplete execution cannot satisfy complete delivery")
        object.__setattr__(self, "output", frozen(self.output))
        object.__setattr__(self, "acceptance", frozen(self.acceptance))
        for name in ("artifacts", "runs"):
            object.__setattr__(self, name, tuple(frozen(item) for item in getattr(self, name)))
        object.__setattr__(self, "usage", tuple(item if isinstance(item, UsageObservation)
            else UsageObservation(**dict(item)) for item in self.usage))

    @property
    def eligible(self):
        return self.status == "completed" and self.accepted and self.cleanup_complete

    @classmethod
    def from_dict(cls, value): return cls(**dict(value))


@dataclass(frozen=True)
class ApplicationExecutionResult(Serializable):
    experiment_id: str
    task_snapshot_id: str
    status: str
    output: Any = None
    selected_trial_id: str = ""
    selected_candidate_id: str = ""
    selection_reason: str = ""
    delivery: Mapping[str, Any] = field(default_factory=dict)
    trials: tuple[ExecutionReceipt, ...] = ()
    roles: tuple[Mapping[str, Any], ...] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    library: Mapping[str, Any] = field(default_factory=dict)
    method: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "output", frozen(self.output))
        for name in ("delivery", "usage", "library", "method"):
            object.__setattr__(self, name, frozen(getattr(self, name)))
        object.__setattr__(self, "roles", tuple(frozen(item) for item in self.roles))
        object.__setattr__(self, "trials", tuple(item if isinstance(item, ExecutionReceipt)
            else ExecutionReceipt.from_dict(item) for item in self.trials))
        object.__setattr__(self, "errors", tuple(self.errors))

    @classmethod
    def from_dict(cls, value): return cls(**dict(value))


class ApplicationDriver(Protocol):
    def preflight(self, request: ApplicationRequest, policy: Any) -> None: ...
    def validate_candidate(self, invocation: PreparedInvocation, project_root: Path, application_root: Path) -> None: ...
    def prepare(self, request: ApplicationRequest, base_bundle: Any) -> PreparedInvocation: ...
    def execute_once(self, invocation: PreparedInvocation, bundle: Any, context: TrialContext) -> ExecutionReceipt: ...
    def reconcile(self, invocation: PreparedInvocation, bundle: Any, context: TrialContext) -> ExecutionReceipt | None: ...
    def publish(self, invocation: PreparedInvocation, selected: ExecutionReceipt, decision_id: str) -> Mapping[str, Any]: ...


_TRIAL_CONTEXT: ContextVar[TrialContext | None] = ContextVar("harness_trial_context", default=None)


def current_trial_context() -> TrialContext | None:
    return _TRIAL_CONTEXT.get()


@contextmanager
def bind_trial_context(context: TrialContext):
    token = _TRIAL_CONTEXT.set(context)
    try:
        yield context
    finally:
        _TRIAL_CONTEXT.reset(token)
