"""Trusted host configuration, never selected by durable task input."""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .authorization import assert_current_attempt as assert_current_attempt
from .client import TaskDescription, canonical_json


def exception_outcomes(error: BaseException):
    """Retain invocation receipts through Application finalization errors.

    Cleanup can raise a new ExceptionGroup with the invocation error as its
    context. Traverse the original exception graph, preserving receipts rather
    than manufacturing a new one from the final top-level message.
    """
    from agentloom.execution.concurrency import WorkerOutcome
    pending, seen, receipts, known = [error], set(), [], set()
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        candidates = [getattr(current, "worker_outcome", None), *getattr(current, "outcomes", ())]
        for candidate in candidates:
            if isinstance(candidate, WorkerOutcome) and id(candidate) not in known:
                known.add(id(candidate))
                receipts.append(candidate)
        related = [current.__cause__, current.__context__, getattr(current, "original_error", None)]
        if isinstance(current, BaseExceptionGroup):
            related.extend(current.exceptions)
        pending.extend(cause for cause in related if isinstance(cause, BaseException))
    return tuple(receipts)


class InvocationHost(Protocol):
    def reconcile(self, task: TaskDescription) -> dict | None: ...
    def invoke(self, task: TaskDescription, invocation_id: str) -> dict: ...
    def revoke(self, task: TaskDescription, invocation_id: str) -> None: ...


@dataclass(frozen=True)
class HostSpec:
    """An importable factory and JSON options installed by the CLI owner.

    Options should contain paths and host settings, not credentials. The child
    reloads private configuration from the project. Live handles and closures
    cannot cross this interface.
    """
    factory: str
    options: dict
    allow_replay: bool = False
    max_attempts: int = 1

    def __post_init__(self):
        module, separator, name = self.factory.partition(":")
        if not separator or not module or not name.isidentifier():
            raise ValueError("Host factory must be an importable module:function")
        if type(self.allow_replay) is not bool or type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("Invalid host retry policy")
        if self.max_attempts > 1 and not self.allow_replay:
            raise ValueError("Retries require explicit host replay authorization")
        canonical_json(self.options)

    def to_dict(self):
        return {"factory": self.factory, "options": self.options,
                "allow_replay": self.allow_replay, "max_attempts": self.max_attempts}

    def build(self) -> InvocationHost:
        module, name = self.factory.split(":")
        return getattr(importlib.import_module(module), name)(self.options)


class ApplicationHost:
    def __init__(self, options: dict[str, Any]):
        self.project_root = Path(options["project_root"]).resolve(strict=True)
        self.application = Path(options["application"]).resolve(strict=True)

    def reconcile(self, task):
        return None

    def revoke(self, task, invocation_id):
        pass

    def invoke(self, task, invocation_id):
        from agentloom.app import Application

        with Application.open(self.application, project_root=self.project_root) as app:
            result = app.worker(task.worker).invoke(task.item)
        # Result becomes visible only after the whole Application closes.
        return result.to_dict()


def application_host(options):
    return ApplicationHost(options)
