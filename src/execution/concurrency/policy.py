"""Resolve one map's immutable policy from validated system configuration layers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

from agentloom.config.config_validation import WorkerExecutionSettings

WorkerExecutionSourceName = Literal[
    "framework_default", "project_system", "application_system", "application_worker", "call_override"
]


def validate_concurrency(value: object, *, source: str = "concurrency") -> int:
    """Validate without bool, text, auto, inference or an invented upper limit."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"{source} must be a strict positive integer; auto is not supported. "
            "Configure worker_execution.defaults/workers or pass map(concurrency=N)"
        )
    return value


@dataclass(frozen=True, slots=True)
class WorkerExecutionSource:
    source: WorkerExecutionSourceName
    concurrency: int

    def __post_init__(self) -> None:
        if self.source not in get_args(WorkerExecutionSourceName):
            raise ValueError(f"unknown Worker execution source: {self.source!r}")
        validate_concurrency(self.concurrency, source=self.source)

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "concurrency": self.concurrency}


@dataclass(frozen=True, slots=True)
class ResolvedWorkerExecution:
    concurrency: int
    source: WorkerExecutionSourceName
    source_chain: tuple[WorkerExecutionSource, ...]

    def __post_init__(self) -> None:
        validate_concurrency(self.concurrency)
        object.__setattr__(self, "source_chain", tuple(self.source_chain))
        if any(not isinstance(entry, WorkerExecutionSource) for entry in self.source_chain):
            raise ValueError("source_chain must contain WorkerExecutionSources")
        if not self.source_chain or (self.source, self.concurrency) != (
            self.source_chain[-1].source,
            self.source_chain[-1].concurrency,
        ):
            raise ValueError("resolved policy must match the final source-chain entry")

    def to_dict(self) -> dict[str, Any]:
        return {
            "concurrency": self.concurrency,
            "source": self.source,
            "source_chain": [entry.to_dict() for entry in self.source_chain],
        }


def validate_worker_execution(
    section: Mapping[str, Any] | WorkerExecutionSettings | None, *, scope: Literal["project", "application"]
) -> WorkerExecutionSettings:
    """Validate a complete unmerged source, including values later overridden."""
    settings = WorkerExecutionSettings.model_validate({} if section is None else section)
    if scope == "project" and "workers" in settings.model_fields_set:
        raise ValueError(
            "Project worker_execution must not contain Application-private workers; "
            "move workers to the Application config/system.yaml"
        )
    return settings


def resolve_worker_execution(
    worker_identity: str,
    *,
    project: Mapping[str, Any] | WorkerExecutionSettings | None = None,
    application: Mapping[str, Any] | WorkerExecutionSettings | None = None,
    concurrency: int | None = None,
) -> ResolvedWorkerExecution:
    """Resolve one call after Application composition canonicalizes Worker identities."""
    if not isinstance(worker_identity, str) or not worker_identity.strip():
        raise ValueError("worker_identity must be a non-empty canonical Application-relative path")
    project_policy = validate_worker_execution(project, scope="project")
    application_policy = validate_worker_execution(application, scope="application")
    chain = [WorkerExecutionSource("framework_default", 1)]
    for source, settings in (("project_system", project_policy), ("application_system", application_policy)):
        if "concurrency" in settings.defaults.model_fields_set:
            chain.append(WorkerExecutionSource(source, settings.defaults.concurrency))
    worker_policy = application_policy.workers.get(worker_identity)
    if worker_policy is not None and "concurrency" in worker_policy.model_fields_set:
        chain.append(WorkerExecutionSource("application_worker", worker_policy.concurrency))
    if concurrency is not None:
        chain.append(
            WorkerExecutionSource("call_override", validate_concurrency(concurrency, source="call concurrency"))
        )
    winner = chain[-1]
    return ResolvedWorkerExecution(winner.concurrency, winner.source, tuple(chain))
