"""Bounded, per-call synchronous admission; Application owns Run lifetime."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import dataclass
from threading import Event
from typing import Any

from agentloom.execution.concurrency.batch_models import (
    BatchEvent,
    BatchOutcome,
    ObserverDiagnostic,
    WorkerBatchError,
    WorkerError,
    WorkerOutcome,
    WorkItem,
    WorkPlan,
)
from agentloom.execution.concurrency.policy import ResolvedWorkerExecution


class InvocationControlError(BaseException):
    """Carry actual started disposition while draining a process-control signal."""

    def __init__(self, error: BaseException, outcome: WorkerOutcome):
        self.error, self.outcome = error, outcome


@dataclass(eq=False)
class BatchControl:
    stop: Event
    reason: str = "stop requested"


def run_batch(
    *,
    plan: WorkPlan,
    batch_id: str,
    run_id: str,
    worker_identity: str,
    execution: ResolvedWorkerExecution,
    control: BatchControl,
    invoke: Callable[[WorkItem, int], WorkerOutcome],
    persist: Callable[[dict], None],
    failure_mode: str,
    stop_token: Any = None,
    event_sink: Callable[[BatchEvent], None] | None = None,
    definition_revision: str | None = None,
    config_revision: str | None = None,
) -> BatchOutcome:
    """Execute only admitted work; cleanup is part of the callback's completion."""
    outcomes: list[WorkerOutcome | None] = [None] * len(plan.items)
    events: list[BatchEvent] = []
    observers: list[ObserverDiagnostic] = []
    pending: dict[Future, int] = {}
    next_index = 0
    stopped = False
    infrastructure: WorkerBatchError | None = None
    interruption: BaseException | None = None
    completed_count = 0

    def check_stop() -> bool:
        nonlocal stopped
        if stop_token is not None and stop_token.is_set():
            control.stop.set()
        if control.stop.is_set():
            stopped = True
        return stopped

    def canceled(index: int) -> WorkerOutcome:
        return WorkerOutcome(plan.items[index].id, index, "canceled", cancellation_reason=control.reason)

    def receipt() -> BatchOutcome:
        known = tuple(outcome for outcome in outcomes if outcome is not None)
        status = (
            "failed"
            if infrastructure is not None or any(outcome.status == "failed" for outcome in known)
            else "stopped"
            if stopped
            else "completed"
        )
        return BatchOutcome(
            batch_id,
            run_id,
            worker_identity,
            status,
            execution,
            known,
            plan.input_fingerprint,
            failure_mode,
            definition_revision,
            config_revision,
            tuple(observers),
            tuple(events),
        )

    def fail(error: BaseException, phase: str) -> None:
        nonlocal infrastructure
        if infrastructure is None:
            infrastructure = (
                error
                if isinstance(error, WorkerBatchError)
                else WorkerBatchError(str(error), phase=phase, original_error=error)
            )
        control.reason = f"batch {phase} failure"
        control.stop.set()

    def persist_observed(*, initial: bool = False) -> None:
        try:
            persist({**receipt().to_dict(), **({"status": "running"} if initial else {})})
        except Exception as exc:
            fail(exc, "persistence")
        except BaseException as exc:
            nonlocal interruption
            interruption = interruption or exc
            control.reason = "batch interrupted"
            control.stop.set()

    def observe(outcome: WorkerOutcome) -> None:
        nonlocal completed_count, event_sink, interruption
        if outcomes[outcome.index] is not None:
            fail(RuntimeError("duplicate item terminal disposition"), "consistency")
            return
        outcomes[outcome.index] = outcome
        completed_count += 1
        event = BatchEvent(
            batch_id, len(events) + 1, outcome=outcome, completed_count=completed_count, total_count=len(plan.items)
        )
        events.append(event)
        if failure_mode == "fail_fast" and outcome.status == "failed":
            control.reason = "fail-fast failure"
            control.stop.set()
        if event_sink is not None:
            try:
                event_sink(event)
            except Exception as exc:
                observers.append(ObserverDiagnostic(event.sequence, type(exc).__name__, str(exc)))
                event_sink = None
            except BaseException as exc:
                interruption = interruption or exc
                control.reason = "observer interrupted"
                control.stop.set()
                event_sink = None

    def start(index: int) -> WorkerOutcome:
        # Submitted does not mean started: this final seam owns the decision.
        if control.stop.is_set() or (stop_token is not None and stop_token.is_set()):
            return canceled(index)
        outcome = invoke(plan.items[index], index)
        if not isinstance(outcome, WorkerOutcome) or outcome.index != index or outcome.item_id != plan.items[index].id:
            raise WorkerBatchError(
                "invocation callback returned inconsistent identity",
                phase="consistency",
                original_error=RuntimeError("outcome identity mismatch"),
            )
        return outcome

    persist_observed(initial=True)
    executor = None
    try:
        if plan.items and infrastructure is None and interruption is None:
            try:
                executor = ThreadPoolExecutor(max_workers=execution.concurrency, thread_name_prefix="agentloom-batch")
            except BaseException as exc:
                if isinstance(exc, Exception):
                    fail(exc, "executor")
                else:
                    interruption = exc
                    control.stop.set()
            while executor is not None and (pending or next_index < len(plan.items)):
                try:
                    check_stop()
                    # Observe every currently completed result before filling slots.
                    done = [future for future in pending if future.done()]
                    for future in done:
                        index = pending.pop(future)
                        if future.cancelled():
                            outcome = canceled(index)
                        else:
                            try:
                                outcome = future.result()
                            except InvocationControlError as exc:
                                outcome = exc.outcome
                                interruption = interruption or exc.error
                                control.reason = "invocation interrupted"
                                control.stop.set()
                            except WorkerBatchError as exc:
                                fail(exc, exc.phase)
                                outcome = next((o for o in exc.outcomes if o.index == index), None)
                                if outcome is None:
                                    outcome = WorkerOutcome(
                                        plan.items[index].id,
                                        index,
                                        "failed",
                                        error=WorkerError.from_exception(exc.original_error, category="infrastructure"),
                                    )
                            except BaseException as exc:
                                if isinstance(exc, Exception):
                                    fail(exc, "invocation_callback")
                                else:
                                    interruption = interruption or exc
                                    control.stop.set()
                                outcome = WorkerOutcome(
                                    plan.items[index].id, index, "failed", error=WorkerError.from_exception(exc)
                                )
                        observe(outcome)
                    if done:
                        persist_observed()
                    if check_stop():
                        for future in pending:
                            future.cancel()
                        # Never overwrite running Futures with cancellation.
                        while next_index < len(plan.items):
                            observe(canceled(next_index))
                            next_index += 1
                    else:
                        while next_index < len(plan.items) and len(pending) < execution.concurrency:
                            if check_stop():
                                break
                            index = next_index
                            context = copy_context()  # One independently enterable Context per submission.
                            try:
                                future = executor.submit(context.run, start, index)
                            except Exception as exc:
                                fail(exc, "executor")
                                break
                            pending[future] = index
                            next_index += 1
                    if pending:
                        wait(tuple(pending), timeout=0.05, return_when=FIRST_COMPLETED)
                except BaseException as exc:
                    if isinstance(exc, Exception):
                        fail(exc, "controller")
                    else:
                        interruption = interruption or exc
                        control.reason = "batch interrupted"
                        control.stop.set()
                    # Re-enter to drain actual starts before propagation.
    finally:
        if executor is not None:
            try:
                executor.shutdown(wait=True, cancel_futures=True)
            except BaseException as exc:
                if isinstance(exc, Exception):
                    fail(exc, "executor_cleanup")
                else:
                    interruption = interruption or exc
        if infrastructure is not None or interruption is not None:
            stopped = True
            for index, outcome in enumerate(outcomes):
                if outcome is None:
                    observe(canceled(index))
        persist_observed()
    result = receipt()
    if infrastructure is not None:
        raise WorkerBatchError(
            str(infrastructure),
            phase=infrastructure.phase,
            original_error=infrastructure.original_error,
            outcome=result,
            cleanup_errors=infrastructure.cleanup_errors,
        ) from infrastructure.original_error
    if interruption is not None:
        # Receipt is already persisted; attach it for callers inspecting interruption.
        try:
            interruption.batch_outcome = result
        except Exception:
            pass
        raise interruption
    return result
