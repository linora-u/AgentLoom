"""Public, immutable receipts for one Application execution attempt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

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
class RunInfo:
    """Canonical identity and filesystem locations for one run."""

    application_id: str
    task_id: str
    run_id: str
    run_dir: Path
    manifest_path: Path
    log_path: Path | None


@dataclass(frozen=True, slots=True)
class ApplicationRunResult:
    """The Application output together with its durable run receipt."""

    output: str
    run: RunInfo
    started_at: datetime
    ended_at: datetime


@dataclass(frozen=True, slots=True)
class RunLifecycleEvent:
    """One passive lifecycle notification for an Application run."""

    event: RunEventType
    run: RunInfo
    occurred_at: datetime
    output: str | None = None
    error: str | None = None
    phase: RunPhase | None = None
    schema_version: int = 1


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
