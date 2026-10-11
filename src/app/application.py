"""Application-owned finite Worker execution, independent of any Supervisor."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar, copy_context
from copy import deepcopy
from pathlib import Path
from threading import Condition, Event
from time import monotonic
from typing import Any

from agentloom.app.definition import (
    _prepare_inspected_definition, inspect_application_definition, load_prepared_agent_definition,
)
from agentloom.app.invocation import InvocationEvidence
from agentloom.app.lifecycle import ApplicationRunFinalization, ApplicationRunLifecycle, ApplicationRunResources
from agentloom.app.run import RunInfo
from agentloom.app.validation import AgentConfigNormalizer
from agentloom.config.config import (
    UnifiedConfig, bind_config, fresh_invocation_config, get_config, load_project_config,
    _reject_application_global_only_keys,
)
from agentloom.config.config_validation import validate_system_snapshot
from agentloom.config.layered_builder import LayeredConfigBuilder
from agentloom.config.paths import normalize_config_paths
from agentloom.config.system_loader import filter_llm_only_top_level_keys, load_config_mapping
from agentloom.execution import generate_runtime_id, resolve_runtime_home
from agentloom.execution.concurrency import (
    BatchEvent, BatchOutcome, CleanupDiagnostic, ObserverDiagnostic, ResolvedWorkerExecution,
    WorkerBatchError, WorkerError, WorkerOutcome, WorkItem, WorkPlan,
    resolve_worker_execution, validate_worker_execution,
)
from agentloom.execution.concurrency.batch_runner import BatchControl, InvocationControlError, run_batch
from agentloom.execution.trace import bind_explicit_execution_context, bind_root_run, capture_explicit_execution_context

_CURRENT_APPLICATION: ContextVar[Application | None] = ContextVar('agentloom_application', default=None)
_ACTIVE_BATCH_OWNER: ContextVar[Application | None] = ContextVar('agentloom_batch_owner', default=None)


def current_run_app() -> Application:
    """Return the facade owned by the active Application Run, never allocate a Run."""
    app = _CURRENT_APPLICATION.get()
    if app is None:
        raise RuntimeError('no Application owner is bound; use Application.open(...) or execute_app(...)')
    app._require_open()
    return app


def _revision(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()
    return 'sha256:' + hashlib.sha256(payload).hexdigest()


def _worker_path(application_root: Path, value: str | Path) -> tuple[str, Path]:
    raw = str(value)
    path = Path(raw)
    if not raw.strip() or path.is_absolute() or '..' in path.parts:
        raise ValueError('Worker identity must be an Application-relative YAML path without traversal')
    target = application_root / path
    current = application_root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f'Worker path contains a symlink: {current}')
    target = target.resolve(strict=True)
    if not target.is_relative_to(application_root) or not target.is_file() or target.suffix.lower() not in {'.yaml', '.yml'}:
        raise ValueError('Worker identity must target a real YAML definition inside its Application')
    return target.relative_to(application_root).as_posix(), target


def _system_settings(base: UnifiedConfig, application_root: Path, captured: Mapping | None = None) -> tuple[dict, dict]:
    path = application_root / 'config/system.yaml'
    raw = load_config_mapping(path) if captured is None else deepcopy(dict(captured))
    application = _reject_application_global_only_keys(filter_llm_only_top_level_keys(raw, source_name=str(path)), source_name=str(path))
    validate_worker_execution(base.raw.get('worker_execution'), scope='project')
    settings = validate_worker_execution(application.get('worker_execution'), scope='application')
    canonical = {}
    for identity, policy in settings.workers.items():
        key, target = _worker_path(application_root, identity)
        if key in canonical:
            raise ValueError(f'duplicate canonical Worker policy identity: {key}')
        canonical[key] = policy.model_dump(exclude_unset=True)
        inspection = inspect_application_definition(base.agent_root, str(target), load_prepared_agent_definition(target, project_root=base.agent_root),
            root_is_worker=True, base_config=base, application_config=application)
        if inspection.errors:
            raise ValueError('\n'.join(inspection.errors))
    if 'worker_execution' in application:
        application['worker_execution'] = settings.model_dump(exclude_unset=True)
        if 'workers' in application['worker_execution']:
            application['worker_execution']['workers'] = canonical
    builder = LayeredConfigBuilder(validate_hook=lambda snapshot, overlay: validate_system_snapshot(snapshot, overlay.name))
    builder.apply_mapping('project system', normalize_config_paths(base.raw, base.agent_root))
    builder.apply_mapping(str(path), normalize_config_paths(application, application_root))
    return application, builder.build()


class Application:
    """One Run owner; bindings and registered batches end with this scope."""

    def __init__(self, *, project_config: UnifiedConfig, application_root: Path, application_config: Mapping,
                 runtime_context: Any, lifecycle: ApplicationRunLifecycle, logger: Any = None):
        self.project_config = deepcopy(project_config)
        self.project_root = Path(project_config.agent_root).resolve()
        self.application_root = application_root
        self.application_config = deepcopy(dict(application_config))
        self.runtime_context = runtime_context
        self.lifecycle = lifecycle
        self.logger = logger
        self._condition = Condition()
        self._state = 'open'
        self._batches: set[BatchControl] = set()
        self._bindings: dict[str, BoundWorker] = {}

    @property
    def run(self) -> RunInfo:
        context = self.runtime_context
        return RunInfo(application_id=context.application_id, task_id=context.task_id, run_id=context.run_id,
            run_dir=context.run_dir.absolute(), manifest_path=context.manifest_path.absolute(),
            log_path=context.log_path.absolute() if context.log_path.exists() else None, trace_dir=context.trace_dir.absolute())

    def _require_open(self) -> None:
        with self._condition:
            if self._state != 'open':
                raise RuntimeError('Application scope is closing or closed; open a new scope')

    @contextmanager
    def bind(self) -> Iterator[Application]:
        token = _CURRENT_APPLICATION.set(self)
        try:
            yield self
        finally:
            _CURRENT_APPLICATION.reset(token)

    def close(self) -> None:
        """Reject new batches, stop admissions and drain before root teardown."""
        if _ACTIVE_BATCH_OWNER.get() is self:
            raise RuntimeError('a batch invocation/controller cannot synchronously close its own Application owner')
        with self._condition:
            if self._state == 'closed':
                return
            self._state = 'closing'
            for control in self._batches:
                control.reason = 'Application scope closed'
                control.stop.set()
            interruption = None
            while self._batches:
                try:
                    self._condition.wait()
                except BaseException as exc:
                    interruption = interruption or exc
            self._state = 'closed'
            self._condition.notify_all()
            if interruption is not None:
                raise interruption

    def worker(self, path: str | Path) -> BoundWorker:
        with self._condition:
            self._require_open()
            identity, target = _worker_path(self.application_root, path)
            if identity not in self._bindings:
                with bind_config(self.project_config):
                    parsed = load_prepared_agent_definition(target, project_root=self.project_root)
                    inspection = inspect_application_definition(self.project_root, str(target), parsed, root_is_worker=True,
                        base_config=self.project_config, application_config=self.application_config)
                    if inspection.errors:
                        raise ValueError('\n'.join(inspection.errors))
                    prepared = _prepare_inspected_definition(self.project_root, target, inspection)
                    normalized = AgentConfigNormalizer.build_worker_normalized_config(prepared, agent_root=self.project_root, source_name=identity)
                    self._bindings[identity] = BoundWorker(self, identity, prepared, normalized.input_validator)
            return self._bindings[identity]

    @classmethod
    @contextmanager
    def open(cls, application_path: str | Path, *, project_root: str | Path | None = None,
             file_logging: bool | None = None) -> Iterator[Application]:
        """Allocate a standalone Run through the same lifecycle used by execute_app."""
        current = get_config()
        root = Path(project_root or current.agent_root).expanduser().resolve()
        base = fresh_invocation_config(current) if root == current.agent_root else load_project_config(root)
        candidate = Path(application_path)
        candidate = candidate if candidate.is_absolute() else root / candidate
        if candidate.is_symlink():
            raise ValueError('Application root must not be a symlink')
        application = candidate.resolve(strict=True)
        if not application.is_dir() or not application.is_relative_to(root / 'applications'):
            raise ValueError('Application.open requires a directory inside project applications/')
        app_config, effective = _system_settings(base, application)
        application_id = application.relative_to(root / 'applications').as_posix()
        runtime_home = resolve_runtime_home(effective, agent_root=root)
        context = runtime_home.context(application_id=application_id, task_id=generate_runtime_id('task'), run_id=generate_runtime_id('run'))
        lifecycle = ApplicationRunLifecycle()
        lease = lifecycle.initialize_run(context, agent_name=application_id, mode='new', application_revision=_revision(app_config),
            task_tree_observation={'enabled': False, 'worker_agents_configured': False})
        checkpoint_manager = None
        task_lease = None
        coordinator = None
        app = None
        try:
            with bind_config(base), lifecycle.bind_services(context, effective, file_logging=file_logging) as services, bind_root_run(context.run_id):
                with bind_explicit_execution_context(capture_explicit_execution_context().__class__(
                    task_id=context.task_id, sub_task_id=None, agent_id=None, agent_name=None, agent_config=None,
                    skill_catalog=None, hook_run=None, runtime_agent_path=None, root_run_id=context.run_id, local_run_id=None,
                    root_run_state=capture_explicit_execution_context().root_run_state)):
                    from agentloom.execution.context_engine.runtime import ensure_task_context_engine
                    app = cls(project_config=base, application_root=application, application_config=app_config,
                        runtime_context=context, lifecycle=lifecycle, logger=services.logger_backend)
                    lifecycle.enter_execution()
                    try:
                        with app.bind(), ensure_task_context_engine(effective):
                            try:
                                yield app
                            finally:
                                app.close()
                        lifecycle.complete_execution(None)
                    except BaseException as exc:
                        app.close()
                        lifecycle.fail_execution(exc)
                        raise
                    finally:
                        try:
                            lifecycle.finalize_run(ApplicationRunFinalization(context, checkpoint_manager, context.task_id, None, {}, False, services.log,
                                record_final_answer=services.recorder.record_final_answer))
                        finally:
                            lifecycle.close_resources(ApplicationRunResources(None, None, None, services.log, task_lease, checkpoint_manager))
        except BaseException as exc:
            lifecycle.persist_uncaught_failure(runtime_context=context, manifest_initialized=context.manifest_path.is_file(), manifest_updates={}, error=exc)
            raise
        finally:
            lifecycle.release_run_lease(lease)

    def _map_callback(self, items: Sequence[WorkItem] | WorkPlan, *, worker_identity: str,
                      execution: ResolvedWorkerExecution, invoke: Callable[[WorkItem, int], WorkerOutcome],
                      input_validator: Callable[[Any], None] | None = None,
                      definition_revision: str | None = None, config_revision: str | None = None,
                      failure_mode: str = 'collect', stop_token: Any = None, event_sink: Callable[[BatchEvent], None] | None = None) -> BatchOutcome:
        """Internal callback seam shared by typed Workers and legacy adapters."""
        self._require_open()
        plan = items if isinstance(items, WorkPlan) else WorkPlan(items)
        if failure_mode not in {'collect', 'fail_fast'}:
            raise ValueError('failure_mode must be collect or fail_fast')
        if stop_token is not None and not callable(getattr(stop_token, 'is_set', None)):
            raise ValueError('stop_token must expose is_set(), such as threading.Event')
        for item in plan.items:
            if input_validator is not None:
                input_validator(item.mutable_input())
        batch_id = generate_runtime_id('batch')
        control = BatchControl(Event())
        with self._condition:
            self._require_open()
            self._batches.add(control)
        active = _ACTIVE_BATCH_OWNER.set(self)
        initial = True
        def persist(record):
            nonlocal initial
            self.runtime_context.write_batch_record(batch_id, record)
            if initial:
                self.lifecycle.register_batch_record(self.runtime_context, batch_id)
                initial = False
        try:
            return run_batch(plan=plan, batch_id=batch_id, run_id=self.runtime_context.run_id,
                worker_identity=worker_identity, execution=execution, control=control, invoke=invoke, persist=persist,
                failure_mode=failure_mode, stop_token=stop_token, event_sink=event_sink,
                definition_revision=definition_revision, config_revision=config_revision)
        finally:
            _ACTIVE_BATCH_OWNER.reset(active)
            with self._condition:
                self._batches.remove(control)
                self._condition.notify_all()


class BoundWorker:
    """Definition snapshot bound to exactly one Application execution scope."""

    def __init__(self, owner: Application, identity: str, definition: dict, input_validator: Callable | None):
        self.owner = owner
        self.identity = identity
        self._definition = deepcopy(definition)
        self._input_validator = input_validator
        from agentloom.app.revision import agent_definition_revision
        self.definition_revision = agent_definition_revision(definition)
        snapshot = definition['_effective_agent_config_snapshot']
        self.config_revision = _revision({'values': snapshot.values, 'models': snapshot.llm.model_dump() if snapshot.llm else {}})

    def _invoke(self, item: WorkItem, index: int) -> WorkerOutcome:
        from agentloom.app.factory import YamlConfiguredAgent
        started = monotonic()
        evidence = InvocationEvidence()
        output = None
        error = None
        agent = None
        with bind_config(self.owner.project_config):
            try:
                agent = YamlConfiguredAgent(config=deepcopy(self._definition), logger=self.owner.logger)
                output = agent.run(additional_args=item.mutable_input(), invocation_evidence=evidence)
            except BaseException as exc:
                error = exc
        report = evidence.report
        cleanup = () if report is None else tuple(CleanupDiagnostic(entry.phase, type(entry.error).__name__, str(entry.error)) for entry in report.cleanup_errors)
        result = WorkerOutcome(item.id, index, 'success' if error is None else 'failed',
            invocation_id=report.agent_id if report else agent.get_agent_id() if agent else None, output=output,
            error=WorkerError.from_exception((report.primary_error if report else None) or error) if error else None,
            usage={} if report is None else {name: getattr(report.usage, name) for name in ('input_tokens','output_tokens','total_tokens','cached_input_tokens','reasoning_tokens')},
            cleanup_errors=cleanup, local_run_id=report.local_run_id if report else None, root_run_id=report.root_run_id if report else None,
            task_id=report.task_id if report else self.owner.runtime_context.task_id, duration_seconds=monotonic()-started)
        if cleanup:
            raise WorkerBatchError('Worker cleanup did not complete', phase='cleanup', original_error=error or report.cleanup_errors[0].error,
                outcomes=(result,), cleanup_errors=cleanup)
        if error is not None and not isinstance(error, Exception):
            raise InvocationControlError(error, result)
        return result

    def map(self, items: Sequence[WorkItem] | WorkPlan, *, concurrency: int | None = None, failure_mode: str = 'collect',
            stop_token: Any = None, event_sink: Callable[[BatchEvent], None] | None = None) -> BatchOutcome:
        self.owner._require_open()
        execution = resolve_worker_execution(self.identity, project=self.owner.project_config.raw.get('worker_execution'),
            application=self.owner.application_config.get('worker_execution'), concurrency=concurrency)
        return self.owner._map_callback(items, worker_identity=self.identity, execution=execution, invoke=self._invoke,
            input_validator=self._input_validator, definition_revision=self.definition_revision, config_revision=self.config_revision,
            failure_mode=failure_mode, stop_token=stop_token, event_sink=event_sink)
