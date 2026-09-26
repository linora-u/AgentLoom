"""Public, immutable receipts for one Application execution attempt."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol

from agentloom.execution.agent_runtime import JSONValue, copy_json_value

RunEventType = Literal[
    "run.started",
    "run.completed",
    "run.failed",
    "run.interrupted",
]
RunPhase = Literal[
    "initialization",
    "execution",
    "finalization",
    "cleanup",
]


@dataclass(frozen=True, slots=True)
class GoalSnapshot(Mapping[str, object]):
    """Immutable copy of one public Goal projection."""

    _values: Mapping[str, object]

    def __init__(self, value: Mapping[str, object]) -> None:
        object.__setattr__(self, "_values", MappingProxyType(dict(value)))

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def _goal_snapshot(value: Mapping[str, object] | None) -> GoalSnapshot | None:
    return None if value is None else GoalSnapshot(value)


@dataclass(frozen=True, slots=True)
class RunInfo:
    """Canonical identity and filesystem locations for one run."""

    application_id: str
    task_id: str
    run_id: str
    run_dir: Path
    manifest_path: Path
    log_path: Path | None
    trace_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class ApplicationRunResult:
    """The Application output together with its durable run receipt."""

    output: JSONValue
    run: RunInfo
    started_at: datetime
    ended_at: datetime
    goal: GoalSnapshot | None = None
    final_answer_presented: bool = False
    answer_present: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "output",
            copy_json_value(self.output, field_name="application result output"),
        )
        object.__setattr__(self, "goal", _goal_snapshot(self.goal))
        if not isinstance(self.answer_present, bool):
            raise TypeError("answer_present must be a boolean")


@dataclass(frozen=True, slots=True)
class RunLifecycleEvent:
    """One passive lifecycle notification for an Application run."""

    event: RunEventType
    run: RunInfo
    occurred_at: datetime
    output: JSONValue = None
    answer_present: bool | None = None
    error: str | None = None
    phase: RunPhase | None = None
    goal: GoalSnapshot | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "output",
            copy_json_value(self.output, field_name="run lifecycle event output"),
        )
        object.__setattr__(self, "goal", _goal_snapshot(self.goal))
        if self.answer_present is not None and not isinstance(self.answer_present, bool):
            raise TypeError("answer_present must be a boolean when provided")


@dataclass(frozen=True, slots=True)
class RunRejection:
    """Structured reason why execution was rejected before run allocation."""

    kind: str
    message: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class RunRejectedEvent:
    """A run-less preflight rejection delivered through the event sink."""

    occurred_at: datetime
    error: RunRejection
    event: Literal["run.rejected"] = "run.rejected"
    phase: Literal["preflight"] = "preflight"
    schema_version: int = 1


RunEvent = RunLifecycleEvent | RunRejectedEvent


class RunEventSink(Protocol):
    """Consumer called synchronously for each lifecycle notification."""

    def __call__(self, event: RunEvent) -> None: ...


class ApplicationRunError(RuntimeError):
    """Failure raised after a run was allocated and can be inspected."""

    def __init__(
        self,
        message: str,
        *,
        run: RunInfo,
        phase: RunPhase,
        original_error: BaseException,
    ) -> None:
        super().__init__(message)
        self.run = run
        self.phase = phase
        self.original_error = original_error


class ApplicationRunInterrupted(KeyboardInterrupt):
    """Keyboard interruption carrying the allocated run receipt."""

    def __init__(
        self,
        message: str,
        *,
        run: RunInfo,
        phase: RunPhase,
        original_error: KeyboardInterrupt,
        resumable: bool = True,
    ) -> None:
        super().__init__(message)
        self.run = run
        self.phase = phase
        self.original_error = original_error
        self.resumable = resumable
