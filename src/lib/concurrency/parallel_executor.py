"""
Parallel executor for batch agent-tool invocations.

Design inspirations:
- AgentLoom test_parallel_workers.py: ThreadPoolExecutor + Agent-as-Tool pattern
- AgentLoom task_context.py: sub_task_context() for per-thread log isolation
"""

from __future__ import annotations

import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from typing import Any, Callable, List, Optional

from src.lib.concurrency.models import TaskResult


class ParallelAgentExecutor:
    """
    Thread-pool executor with back-pressure, circuit-breaker, and log isolation.

    Args:
        max_workers: Maximum parallel threads. Defaults to ``min(rpm, 10)``
                     where rpm comes from the model-type's rate limiter.
                     Actual request pacing is handled by the rate limiter
                     (``interval = 60/RPM``), so we only need to cap thread count.
        model_type: Used to look up RPM for auto-calculating max_workers.
        max_pending: Back-pressure threshold — when this many futures are
                     in-flight, block until at least one completes before
        circuit_breaker_threshold: After this many *consecutive* failures the
                                   remaining tasks are marked "skipped"
    """

    def __init__(
        self,
        max_workers: Optional[int] = None,
        model_type: str = "powerful",
        max_pending: int = 20,
        circuit_breaker_threshold: int = 5,
    ):
        self._model_type = model_type
        self._max_pending = max_pending
        self._cb_threshold = circuit_breaker_threshold

        if max_workers is not None:
            self._max_workers = max_workers
        else:
            self._max_workers = self._auto_max_workers(model_type)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def execute_batch(
        self,
        tasks: List[dict],
        agent_tool: Callable,
        on_progress: Optional[Callable[[int, int, TaskResult], None]] = None,
    ) -> List[TaskResult]:
        """
        Execute *tasks* in parallel, each by calling ``agent_tool(**task)``.

        Returns one ``TaskResult`` per input task (order may differ from input).

        Features:
        - **Back-pressure**: at most *max_pending* futures in flight.
        - **Circuit-breaker**: after *circuit_breaker_threshold* consecutive
          failures the remaining tasks are returned as ``status="skipped"``.
        - **Error isolation**: one task's exception does not affect others.
        - **Log isolation**: each thread gets its own ``sub_task_context``.
        """
        if not tasks:
            return []

        results: List[TaskResult] = []
        consecutive_failures = 0
        completed_count = 0
        total = len(tasks)

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            active: dict[Future, dict] = {}

            for task in tasks:
                # ── Circuit breaker ──
                if consecutive_failures >= self._cb_threshold:
                    results.append(TaskResult(
                        task_id=task.get("task_id", task.get("dir_path", "?")),
                        status="skipped",
                        error=f"circuit breaker tripped after {consecutive_failures} consecutive failures",
                    ))
                    continue

                # ── Back-pressure: wait if too many in-flight ──
                while len(active) >= self._max_pending:
                    done, _ = wait(active.keys(), return_when=FIRST_COMPLETED)
                    for f in done:
                        r = self._collect(f, active.pop(f))
                        results.append(r)
                        completed_count += 1
                        consecutive_failures = consecutive_failures + 1 if r.status == "failed" else 0
                        if on_progress:
                            on_progress(completed_count, total, r)

                # ── Submit ──
                task["_start_time"] = time.monotonic()
                future = executor.submit(self._run_one, task, agent_tool)
                active[future] = task

            # ── Drain remaining futures ──
            for f in as_completed(active.keys()):
                r = self._collect(f, active[f])
                results.append(r)
                completed_count += 1
                consecutive_failures = consecutive_failures + 1 if r.status == "failed" else 0
                if on_progress:
                    on_progress(completed_count, total, r)

        return results

    def execute_groups(
        self,
        task_groups: List[List[dict]],
        agent_tool: Callable,
        on_progress: Optional[Callable[[int, int, TaskResult], None]] = None,
    ) -> List[List[TaskResult]]:
        """
        Execute groups sequentially; within each group tasks run in parallel.

        This supports dependency patterns like bottom-up analysis where
        deeper directories must complete before their parents.
        """
        all_results: List[List[TaskResult]] = []
        for group in task_groups:
            if not group:
                all_results.append([])
                continue
            group_results = self.execute_batch(group, agent_tool, on_progress)
            all_results.append(group_results)
        return all_results

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _run_one(self, task: dict, agent_tool: Callable) -> Any:
        """Execute a single task with log-context isolation."""
        try:
            from src.trace.task_context import sub_task_context
        except ImportError:
            # Graceful fallback if trace module not available (e.g. in unit tests)
            import contextlib
            sub_task_context = contextlib.contextmanager(lambda name: (yield name))

        task_id = task.get("task_id", task.get("dir_path", "unknown"))
        tool_name = getattr(agent_tool, "__name__", "agent_tool")

        # Filter out internal keys before calling
        call_kwargs = {k: v for k, v in task.items() if not k.startswith("_")}

        with sub_task_context(f"{tool_name}_{task_id}"):
            return agent_tool(**call_kwargs)

    def _collect(self, future: Future, task: dict) -> TaskResult:
        """Harvest a completed future into a TaskResult."""
        task_id = task.get("task_id", task.get("dir_path", "?"))
        start = task.get("_start_time", time.monotonic())
        duration = time.monotonic() - start
        try:
            result = future.result()
            return TaskResult(
                task_id=task_id,
                status="completed",
                result=result,
                duration_seconds=duration,
            )
        except Exception as e:
            return TaskResult(
                task_id=task_id,
                status="failed",
                error=str(e),
                error_trace=traceback.format_exc(),
                duration_seconds=duration,
            )

    @staticmethod
    def _auto_max_workers(model_type: str) -> int:
        """Derive max_workers from RPM configuration.

        Formula: ``min(RPM, 10)``.

        Actual request pacing is handled by the rate limiter which enforces
        ``interval = 60 / RPM`` between consecutive calls.  The thread count
        only needs to be large enough to keep the pipeline saturated while
        staying within a reasonable bound for Python's GIL + API limits.

        Upper cap of 10 is conservative; the rate limiter prevents exceeding
        the API quota regardless of how many threads are active.
        """
        try:
            from src.lib.concurrency.rate_limiter import GlobalRateLimiterRegistry
            limiter = GlobalRateLimiterRegistry.get_limiter(model_type)
            rpm = limiter.rpm
        except Exception:
            rpm = 10  # safe default
        return max(1, min(rpm, 10))
