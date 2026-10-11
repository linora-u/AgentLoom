"""Immutable public work values and receipts for finite Worker map execution."""

from __future__ import annotations

import hashlib
import json
import math
import traceback as traceback_module
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from agentloom.execution.concurrency.policy import ResolvedWorkerExecution

WorkerStatus = Literal["success", "failed", "canceled"]
BatchStatus = Literal["completed", "failed", "stopped"]
FailureMode = Literal["collect", "fail_fast"]
type JSONInput = None | bool | int | float | str | Sequence[JSONInput] | Mapping[str, JSONInput]


def _freeze_json(value: object, *, field_name: str, ancestors: frozenset[int] = frozenset()) -> Any:
    """Copy and freeze JSON, rejecting nonfinite numbers, bad keys and cycles."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError(f"{field_name} must contain finite JSON numbers")
    if isinstance(value, (Mapping, list, tuple)):
        if id(value) in ancestors:
            raise ValueError(f"{field_name} must not contain a JSON reference cycle")
        ancestors = ancestors | {id(value)}
        if isinstance(value, Mapping):
            if any(not isinstance(key, str) for key in value):
                raise ValueError(f"{field_name} must contain only string JSON object keys")
            return MappingProxyType(
                {
                    key: _freeze_json(child, field_name=f"{field_name}.{key}", ancestors=ancestors)
                    for key, child in value.items()
                }
            )
        return tuple(
            _freeze_json(child, field_name=f"{field_name}[{index}]", ancestors=ancestors)
            for index, child in enumerate(value)
        )
    raise ValueError(f"{field_name} must be JSON serializable, got {type(value).__name__}")


def _mutable_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _mutable_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(child) for child in value]
    return value


def _identity(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class WorkItem:
    """Stable business identity and a recursively immutable JSON input snapshot."""

    id: str
    input: JSONInput

    def __post_init__(self) -> None:
        _identity(self.id, field_name="WorkItem.id")
        object.__setattr__(self, "input", _freeze_json(self.input, field_name="WorkItem.input"))

    def mutable_input(self) -> JSONInput:
        """Create an owned, mutable JSON copy for a single invocation."""
        return _mutable_json(self.input)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "input": self.mutable_input()}


@dataclass(frozen=True, slots=True)
class WorkPlan:
    """Finite business plan; planning and reduction remain Application code."""

    items: Sequence[WorkItem]
    input_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.items, Sequence) or isinstance(self.items, (str, bytes, bytearray)):
            raise ValueError("WorkPlan.items must be a finite Sequence of WorkItems; generators are unsupported")
        items = tuple(self.items)
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, WorkItem):
                raise ValueError("WorkPlan.items must contain only WorkItems")
            if item.id in seen:
                raise ValueError(f"duplicate WorkItem id in batch: {item.id!r}")
            seen.add(item.id)
        object.__setattr__(self, "items", items)
        payload = json.dumps(
            [item.to_dict() for item in items],
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        object.__setattr__(self, "input_fingerprint", hashlib.sha256(payload).hexdigest())

    def to_dict(self) -> dict[str, Any]:
        return {"items": [item.to_dict() for item in self.items], "input_fingerprint": self.input_fingerprint}


@dataclass(frozen=True, slots=True)
class WorkerError:
    """Serializable cause; retryable metadata never authorizes Worker replay."""

    error_type: str
    message: str
    category: str = "invocation"
    traceback: str | None = None
    retryable: bool | None = None

    def __post_init__(self) -> None:
        _identity(self.error_type, field_name="WorkerError.error_type")
        _identity(self.category, field_name="WorkerError.category")
        if not isinstance(self.message, str) or (self.traceback is not None and not isinstance(self.traceback, str)):
            raise ValueError("WorkerError message/traceback must be strings")
        if self.retryable is not None and not isinstance(self.retryable, bool):
            raise ValueError("WorkerError.retryable must be a boolean when supplied")

    @classmethod
    def from_exception(
        cls, error: BaseException, *, category: str | None = None, traceback: str | None = None
    ) -> WorkerError:
        native_category = getattr(error, "category", None)
        resolved_category = category or (
            native_category if isinstance(native_category, str) and native_category.strip() else "invocation"
        )
        native_retryable = getattr(error, "retryable", None)
        return cls(
            type(error).__name__,
            str(error),
            resolved_category,
            traceback if traceback is not None else "".join(traceback_module.format_exception(error)),
            native_retryable if isinstance(native_retryable, bool) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "category": self.category,
            "traceback": self.traceback,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class CleanupDiagnostic:
    phase: str
    error_type: str
    message: str

    def __post_init__(self) -> None:
        _identity(self.phase, field_name="CleanupDiagnostic.phase")
        _identity(self.error_type, field_name="CleanupDiagnostic.error_type")
        if not isinstance(self.message, str):
            raise ValueError("CleanupDiagnostic.message must be a string")

    def to_dict(self) -> dict[str, Any]:
        return {"phase": self.phase, "error_type": self.error_type, "message": self.message}


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    """Actual terminal disposition of one known input, including cleanup evidence."""

    item_id: str
    index: int
    status: WorkerStatus
    invocation_id: str | None = None
    output: JSONInput = None
    error: WorkerError | None = None
    usage: Mapping[str, JSONInput] = field(default_factory=dict)
    cleanup_errors: tuple[CleanupDiagnostic, ...] = ()
    cancellation_reason: str | None = None
    local_run_id: str | None = None
    root_run_id: str | None = None
    task_id: str | None = None
    duration_seconds: float = 0.0

    def __post_init__(self) -> None:
        _identity(self.item_id, field_name="WorkerOutcome.item_id")
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise ValueError("WorkerOutcome.index must be a non-negative integer")
        if self.status not in ("success", "failed", "canceled"):
            raise ValueError("WorkerOutcome.status must be success, failed or canceled")
        if self.status == "canceled" and self.invocation_id is not None:
            raise ValueError("unstarted canceled work cannot have an invocation identity")
        if self.status == "canceled" and not self.cancellation_reason:
            raise ValueError("unstarted canceled work must have a cancellation reason")
        if self.error is not None and not isinstance(self.error, WorkerError):
            raise ValueError("WorkerOutcome.error must be a WorkerError")
        if any(not isinstance(entry, CleanupDiagnostic) for entry in self.cleanup_errors):
            raise ValueError("WorkerOutcome.cleanup_errors must contain CleanupDiagnostics")
        for name in ("invocation_id", "local_run_id", "root_run_id", "task_id"):
            if getattr(self, name) is not None:
                _identity(getattr(self, name), field_name=f"WorkerOutcome.{name}")
        if self.cancellation_reason is not None and not isinstance(self.cancellation_reason, str):
            raise ValueError("WorkerOutcome.cancellation_reason must be a string")
        if not isinstance(self.usage, Mapping):
            raise ValueError("WorkerOutcome.usage must be a JSON mapping")
        if (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or (not math.isfinite(self.duration_seconds) or self.duration_seconds < 0)
        ):
            raise ValueError("WorkerOutcome.duration_seconds must be a finite non-negative number")
        object.__setattr__(self, "output", _freeze_json(self.output, field_name="WorkerOutcome.output"))
        object.__setattr__(self, "usage", _freeze_json(self.usage, field_name="WorkerOutcome.usage"))
        object.__setattr__(self, "cleanup_errors", tuple(self.cleanup_errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "index": self.index,
            "status": self.status,
            "invocation_id": self.invocation_id,
            "output": _mutable_json(self.output),
            "error": None if self.error is None else self.error.to_dict(),
            "usage": _mutable_json(self.usage),
            "cleanup_errors": [entry.to_dict() for entry in self.cleanup_errors],
            "cancellation_reason": self.cancellation_reason,
            "local_run_id": self.local_run_id,
            "root_run_id": self.root_run_id,
            "task_id": self.task_id,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True, slots=True)
class BatchEvent:
    """Serialized controller observation, ordered independently of input indexes."""

    batch_id: str
    sequence: int
    event: str = "item.completed"
    outcome: WorkerOutcome | None = None
    completed_count: int | None = None
    total_count: int | None = None

    def __post_init__(self) -> None:
        _identity(self.batch_id, field_name="BatchEvent.batch_id")
        _identity(self.event, field_name="BatchEvent.event")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("BatchEvent.sequence must be a non-negative integer")
        if self.outcome is not None and not isinstance(self.outcome, WorkerOutcome):
            raise ValueError("BatchEvent.outcome must be a WorkerOutcome")
        for name in ("completed_count", "total_count"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"BatchEvent.{name} must be a non-negative integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "sequence": self.sequence,
            "event": self.event,
            "outcome": None if self.outcome is None else self.outcome.to_dict(),
            "completed_count": self.completed_count,
            "total_count": self.total_count,
        }


@dataclass(frozen=True, slots=True)
class ObserverDiagnostic:
    sequence: int
    error_type: str
    message: str

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("ObserverDiagnostic.sequence must be a non-negative integer")
        _identity(self.error_type, field_name="ObserverDiagnostic.error_type")
        if not isinstance(self.message, str):
            raise ValueError("ObserverDiagnostic.message must be a string")

    def to_dict(self) -> dict[str, Any]:
        return {"sequence": self.sequence, "error_type": self.error_type, "message": self.message}


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """Ordered finite-map receipt and the pinned policy that governed this call."""

    batch_id: str
    run_id: str
    worker_identity: str
    status: BatchStatus
    execution: ResolvedWorkerExecution
    outcomes: tuple[WorkerOutcome, ...] = ()
    input_fingerprint: str = ""
    failure_mode: FailureMode = "collect"
    definition_revision: str | None = None
    config_revision: str | None = None
    observer_errors: tuple[ObserverDiagnostic, ...] = ()
    events: tuple[BatchEvent, ...] = ()

    def __post_init__(self) -> None:
        for name in ("batch_id", "run_id", "worker_identity"):
            _identity(getattr(self, name), field_name=f"BatchOutcome.{name}")
        if self.status not in ("completed", "failed", "stopped"):
            raise ValueError("BatchOutcome.status must be completed, failed or stopped")
        if self.failure_mode not in ("collect", "fail_fast"):
            raise ValueError("failure_mode must be collect or fail_fast")
        if not isinstance(self.execution, ResolvedWorkerExecution):
            raise ValueError("BatchOutcome.execution must be a ResolvedWorkerExecution")
        if any(not isinstance(entry, WorkerOutcome) for entry in self.outcomes):
            raise ValueError("BatchOutcome.outcomes must contain WorkerOutcomes")
        if any(not isinstance(entry, ObserverDiagnostic) for entry in self.observer_errors):
            raise ValueError("BatchOutcome.observer_errors must contain ObserverDiagnostics")
        if any(not isinstance(entry, BatchEvent) for entry in self.events):
            raise ValueError("BatchOutcome.events must contain BatchEvents")
        for name in ("input_fingerprint", "definition_revision", "config_revision"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"BatchOutcome.{name} must be a string")
        object.__setattr__(self, "outcomes", tuple(self.outcomes))
        object.__setattr__(self, "observer_errors", tuple(self.observer_errors))
        object.__setattr__(self, "events", tuple(self.events))

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "run_id": self.run_id,
            "worker_identity": self.worker_identity,
            "status": self.status,
            "execution": self.execution.to_dict(),
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "input_fingerprint": self.input_fingerprint,
            "failure_mode": self.failure_mode,
            "definition_revision": self.definition_revision,
            "config_revision": self.config_revision,
            "observer_errors": [entry.to_dict() for entry in self.observer_errors],
            "events": [event.to_dict() for event in self.events],
        }


class WorkerBatchError(RuntimeError):
    """Infrastructure/cleanup failure retaining the original cause and partial receipt."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        original_error: BaseException,
        outcome: BatchOutcome | None = None,
        outcomes: Sequence[WorkerOutcome] = (),
        cleanup_errors: Sequence[CleanupDiagnostic] = (),
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.original_error = original_error
        self.outcome = outcome
        self.outcomes = tuple(outcomes) if outcome is None else outcome.outcomes
        self.cleanup_errors = tuple(cleanup_errors)
