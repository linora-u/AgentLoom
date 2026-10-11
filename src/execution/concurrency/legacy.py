"""Deprecated dictionary/TaskResult adaptation to the Application batch owner."""
from __future__ import annotations

import hashlib
import warnings
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path

from agentloom.execution.concurrency.batch_models import BatchEvent, WorkerOutcome, WorkItem
from agentloom.execution.concurrency.models import TaskResult
from agentloom.execution.concurrency.policy import resolve_worker_execution


def legacy_result(outcome: WorkerOutcome, payload: dict) -> TaskResult:
    """Keep the business receipt identity separate from the map's ordinal ID."""
    return TaskResult(
        task_id=payload.get('task_id', payload.get('dir_path', '?')),
        status={'success': 'completed', 'failed': 'failed', 'canceled': 'skipped'}[outcome.status],
        result=outcome.to_dict()['output'],
        error=outcome.cancellation_reason if outcome.status == 'canceled' else (
            outcome.error.message if outcome.error else None),
        error_trace=outcome.error.traceback if outcome.error else None,
        duration_seconds=outcome.duration_seconds,
    )


@contextmanager
def legacy_owner(*, source_path=None, project_config=None, name='callable'):
    """Borrow a current owner or open one compatibility Run, never one per item."""
    from agentloom.app.application import _CURRENT_APPLICATION, Application
    from agentloom.config.config import get_config
    from agentloom.config.paths import configuration_root

    current = _CURRENT_APPLICATION.get()
    if current is not None:
        current._require_open()
        yield current
        return
    base = project_config or get_config()
    project_root = Path(base.agent_root)
    application_root = configuration_root(source_path, project_root) if source_path else project_root
    if application_root != project_root:
        with Application._open_scope(application_root,
                application_id=application_root.relative_to(project_root / 'applications').as_posix(),
                project_config=base, file_logging=False) as app:
            yield app
    else:
        identity = hashlib.sha256(str(name).encode()).hexdigest()[:16]
        with Application._open_scope(project_root, application_id=f'legacy/{identity}',
                project_config=base, application_config={}, file_logging=False) as app:
            yield app


def execute_legacy_batch(tasks: Sequence[dict], *, concurrency=None, source_path=None,
                         project_config=None, name='callable', invoke: Callable,
                         input_validator=None, definition_revision=None, config_revision=None,
                         on_progress=None, failure_mode='collect', stop_token=None) -> list[TaskResult]:
    """One adapter for dictionary callers; scheduling and drain belong to map."""
    warnings.warn('legacy batch APIs are deprecated; use Application.worker(path).map(WorkItems). '
                  'Results now follow input order; auto/RPM concurrency and automatic circuit breaking are removed.',
                  DeprecationWarning, stacklevel=3)
    if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes, bytearray)):
        raise ValueError('legacy tasks must be a finite sequence of dictionaries')
    if any(not isinstance(payload, dict) for payload in tasks):
        raise ValueError('each legacy task must be a dictionary')
    items = [WorkItem(f'legacy-{index}', payload) for index, payload in enumerate(tasks)]
    payloads = [item.mutable_input() for item in items]
    with legacy_owner(source_path=source_path, project_config=project_config, name=name) as app:
        identity = (Path(source_path).relative_to(app.application_root).as_posix()
                    if source_path and Path(source_path).is_relative_to(app.application_root) else f'legacy/{name}')
        execution = resolve_worker_execution(identity, project=app.project_config.raw.get('worker_execution'),
            application=app.application_config.get('worker_execution'), concurrency=concurrency)

        def progress(event: BatchEvent):
            if event.outcome is not None and on_progress is not None:
                on_progress(event.completed_count, event.total_count,
                    legacy_result(event.outcome, payloads[event.outcome.index]))

        result = app._map_callback(items, worker_identity=identity, execution=execution,
            invoke=lambda item, index: invoke(app, item, index), input_validator=input_validator,
            definition_revision=definition_revision, config_revision=config_revision,
            failure_mode=failure_mode, stop_token=stop_token, event_sink=progress if on_progress else None)
        return [legacy_result(outcome, payloads[outcome.index]) for outcome in result.outcomes]
