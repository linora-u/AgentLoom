"""Deprecated callable adapter to the Application-owned finite batch scheduler."""
from __future__ import annotations

from time import monotonic

from agentloom.execution.concurrency.batch_models import WorkerError, WorkerOutcome
from agentloom.execution.concurrency.legacy import execute_legacy_batch
from agentloom.execution.concurrency.policy import validate_concurrency


class ParallelAgentExecutor:
    """Compatibility adapter; prefer Application.worker(path).map(WorkItems).

    None uses configured Worker defaults, or one. model_type remains accepted
    for compatibility but never derives Worker capacity from Provider RPM.
    Retired pending-window/circuit-breaker options require caller migration.
    """

    def __init__(self, max_workers=None, model_type="powerful", max_pending=None,
                 circuit_breaker_threshold=None):
        if max_workers is not None:
            validate_concurrency(max_workers, source='max_workers')
        if max_pending is not None or circuit_breaker_threshold is not None:
            raise ValueError('max_pending/circuit_breaker_threshold are retired; use explicit concurrency '
                             'and failure_mode="collect" or "fail_fast"')
        self._max_workers = max_workers

    def execute_batch(self, tasks, agent_tool, on_progress=None, *, failure_mode='collect', stop_token=None):
        """Return ordered TaskResult receipts without changing task dictionaries."""
        worker_batch = getattr(agent_tool, '_agentloom_batch_adapter', None)
        if worker_batch is not None:
            return worker_batch(tasks, concurrency=self._max_workers, on_progress=on_progress,
                failure_mode=failure_mode, stop_token=stop_token)
        from agentloom.execution import generate_runtime_id
        from agentloom.execution.concurrency.batch_runner import InvocationControlError
        from agentloom.execution.trace.task_context import (
            bind_explicit_execution_context,
            capture_explicit_execution_context,
            sub_task_context,
        )
        caller_context = capture_explicit_execution_context()
        tool_name = getattr(agent_tool, '__name__', 'agent_tool')

        def invoke(app, item, index):
            started = monotonic()
            invocation_id = generate_runtime_id('call')
            payload = item.mutable_input()
            task_id = payload.get('task_id', payload.get('dir_path', '?'))
            output, error = None, None
            try:
                with bind_explicit_execution_context(caller_context), sub_task_context(f'{tool_name}_{task_id}'):
                    output = agent_tool(**payload)
            except BaseException as exc:
                error = exc
            outcome = WorkerOutcome(item.id, index, 'success' if error is None else 'failed',
                invocation_id=invocation_id, output=output,
                error=WorkerError.from_exception(error) if error else None,
                root_run_id=app.run.run_id, task_id=app.run.task_id,
                duration_seconds=monotonic() - started)
            if error is not None and not isinstance(error, Exception):
                raise InvocationControlError(error, outcome)
            return outcome

        return execute_legacy_batch(tasks, concurrency=self._max_workers, name=tool_name,
            invoke=invoke, on_progress=on_progress, failure_mode=failure_mode, stop_token=stop_token)

    def execute_groups(self, task_groups, agent_tool, on_progress=None, **options):
        """Business groups remain sequential; each group gets its own map call."""
        return [self.execute_batch(group, agent_tool, on_progress, **options) for group in task_groups]
