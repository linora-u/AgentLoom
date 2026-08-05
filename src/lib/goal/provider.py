"""Run-scoped Goal state shared by a Supervisor and its Worker tree."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal, overload

from .model import GoalConfig, GoalState, validate_goal_state


class GoalBudgetLimitedError(RuntimeError):
    """Raised by the pre-request fence after a Goal exhausts its soft budget."""

    def __init__(self, state: GoalState) -> None:
        super().__init__(
            f"Goal token budget exhausted: used={state.used_tokens}, "
            f"budget={state.token_budget}"
        )
        self.state = state


class GoalCompleteError(RuntimeError):
    """Stops further model calls after the Goal completion commit point."""

    def __init__(self, state: GoalState) -> None:
        super().__init__("Goal is already complete")
        self.state = state


class GoalStateProvider:
    """Serialize Goal mutations and durably commit each state transition."""

    def __init__(self, state: GoalState) -> None:
        self._state = state
        self._lock = threading.RLock()
        # This allowance exists only in the current process. A completed Goal
        # restored after a crash must not synthesize a missing final response.
        self._completion_settlement_run_id: str | None = None
        self._completion_settlement_available = False

    @staticmethod
    def _coordinator():
        from src.lib.checkpoint.coordinator import CheckpointCoordinator

        return CheckpointCoordinator.current()

    @classmethod
    def initialize(
        cls,
        *,
        config: GoalConfig,
        objective: str,
        objective_fingerprint: str,
        resume: bool,
    ) -> GoalStateProvider:
        if not config.enabled:
            raise ValueError("Cannot initialize Goal state when Goal mode is disabled")
        coordinator = cls._coordinator()
        raw = coordinator.load_goal() if coordinator is not None else None

        if resume:
            if raw is None:
                raise ValueError("Cannot resume Goal mode: checkpoint has no Goal state")
            state = validate_goal_state(raw)
            if state.objective_fingerprint != objective_fingerprint:
                raise ValueError(
                    "Cannot resume Goal mode because the objective changed; "
                    "description, workflow, and runtime task must match"
                )
            state = state.with_resumed_budget(config.token_budget)
        else:
            if raw is not None:
                raise ValueError("Cannot start Goal mode: Goal state already exists")
            state = GoalState.create(
                objective=objective,
                objective_fingerprint=objective_fingerprint,
                token_budget=config.token_budget,
            )

        provider = cls(state)
        provider._persist_locked()
        return provider

    def _persist_locked(self) -> None:
        coordinator = self._coordinator()
        if coordinator is not None:
            coordinator.save_goal(self._state.to_dict())

    def snapshot(self) -> GoalState:
        with self._lock:
            return self._state

    def assert_request_allowed(
        self,
        *,
        local_run_id: str | None = None,
        allow_completion_settlement: bool = False,
    ) -> bool:
        """Fence new work and claim the one ephemeral final-delivery request."""

        with self._lock:
            if self._state.status == "complete":
                if (
                    self._state.token_budget is not None
                    and self._state.used_tokens >= self._state.token_budget
                ):
                    raise GoalBudgetLimitedError(self._state)
                if (
                    allow_completion_settlement
                    and self._completion_settlement_available
                    and local_run_id == self._completion_settlement_run_id
                ):
                    self._completion_settlement_available = False
                    return True
                raise GoalCompleteError(self._state)
            if self._state.status == "budget_limited":
                raise GoalBudgetLimitedError(self._state)
            return False

    def completion_settlement_pending(self, *, local_run_id: str | None) -> bool:
        """Return whether this process still owes the root its final delivery."""

        with self._lock:
            return (
                self._state.status == "complete"
                and (
                    self._state.token_budget is None
                    or self._state.used_tokens < self._state.token_budget
                )
                and self._completion_settlement_available
                and local_run_id == self._completion_settlement_run_id
            )

    def record_usage(self, *, prompt_tokens: int, completion_tokens: int) -> GoalState:
        with self._lock:
            self._state = self._state.with_usage(prompt_tokens, completion_tokens)
            self._persist_locked()
            return self._state

    def mark_started(self) -> GoalState:
        with self._lock:
            self._state = self._state.with_started()
            self._persist_locked()
            return self._state

    def complete(
        self,
        evidence: str,
        *,
        settlement_run_id: str | None = None,
    ) -> GoalState:
        with self._lock:
            previous_status = self._state.status
            self._state = self._state.with_completion(evidence)
            if previous_status == "active" and settlement_run_id is not None:
                self._completion_settlement_run_id = settlement_run_id
                self._completion_settlement_available = True
            self._persist_locked()
            return self._state


_CURRENT_GOAL_PROVIDER: ContextVar[GoalStateProvider | None] = ContextVar(
    "_CURRENT_GOAL_PROVIDER",
    default=None,
)


@overload
def get_current_goal_provider(*, required: Literal[True]) -> GoalStateProvider: ...


@overload
def get_current_goal_provider(
    *, required: Literal[False] = False
) -> GoalStateProvider | None: ...


def get_current_goal_provider(*, required: bool = False) -> GoalStateProvider | None:
    provider = _CURRENT_GOAL_PROVIDER.get()
    if provider is None and required:
        raise RuntimeError("no GoalStateProvider is bound to the current root run")
    return provider


@contextmanager
def bind_goal_state_provider(provider: GoalStateProvider) -> Iterator[GoalStateProvider]:
    token = _CURRENT_GOAL_PROVIDER.set(provider)
    try:
        yield provider
    finally:
        _CURRENT_GOAL_PROVIDER.reset(token)
