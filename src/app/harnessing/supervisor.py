"""Adapt the existing complete Supervisor Run to the Application controller."""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .models import ApplicationRequest, ExecutionReceipt, PreparedInvocation, json_value
from .usage import read_run_usage


class HarnessingApplicationError(RuntimeError):
    """An experiment ended without a selected, delivered Application Run."""

    def __init__(self, result):
        self.result = result
        super().__init__(f"Application experiment {result.experiment_id}: {result.status}; "
                         + "; ".join(result.errors))


def _write(path, value):
    from .process import _atomic
    _atomic(path, json_value(value))


def _definition(project_root, application_root, relative):
    from agentloom.app.definition import inspect_supervisor_definition
    from agentloom.config.config import load_project_config

    path = application_root / relative
    inspection = inspect_supervisor_definition(
        project_root, path.relative_to(project_root).as_posix(),
        base_config=load_project_config(project_root),
    )
    if inspection.errors:
        raise ValueError("\n".join(inspection.errors))
    return inspection


def _contract(definition):
    # All other prompt, topology and tool surfaces remain editable. These
    # values define the original Task and its externally observable contract.
    return {key: definition.get(key) for key in ("task", "goal", "output_schema")}


def _run_dict(run):
    return {key: str(value) if isinstance(value, Path) else value
            for key, value in asdict(run).items()}


class SupervisorDriver:
    """One pinned YAML Supervisor, including its complete delegation tree."""

    def preflight(self, request, policy):
        relative = request.driver_options.get("definition")
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Supervisor definition must be Application-relative YAML")
        inspection = _definition(request.project_root, request.application_root, relative)
        if inspection.application_id != request.application_id:
            raise ValueError("Supervisor belongs to a different Application")
        if request.task is not None and json_value(request.task) != inspection.definition["task"]:
            raise ValueError("Supervisor Task must match its original YAML Task")

    def prepare(self, request, base_bundle):
        inspection = _definition(base_bundle.project_root, base_bundle.application_root,
                                 request.driver_options["definition"])
        contract = _contract(inspection.definition)
        objective = request.objective or (contract["task"] if isinstance(contract["task"], str)
                                          else json.dumps(contract["task"], ensure_ascii=False))
        return PreparedInvocation(request, objective, {"task": contract["task"]},
                                  {"contract": contract}, {"definition": request.driver_options["definition"]})

    def validate_candidate(self, invocation, project_root, application_root):
        inspection = _definition(project_root, application_root, invocation.data["definition"])
        if _contract(inspection.definition) != json_value(invocation.acceptance["contract"]):
            raise ValueError("Candidate changed the original Task, Goal or output contract")

    def memory_snapshot(self, invocation):
        from agentloom.config.config import bind_config, load_project_config
        from agentloom.self_learning.paths import self_learning_enabled
        from agentloom.self_learning.persistence.memory_store import MemoryStore

        request = invocation.request
        with bind_config(load_project_config(request.project_root)):
            inspection = _definition(request.project_root, request.application_root, invocation.data["definition"])
            config = inspection.prepared_definition
            if not self_learning_enabled(config):
                return ""
            return MemoryStore(agent_config=config).snapshot_for_prompt(agent_config=config)

    def _receipt(self, invocation, context, record, *, error=""):
        run = record["run"]
        run_dir = Path(run["run_dir"])
        manifest = json.loads(Path(run["manifest_path"]).read_text())
        terminal = manifest.get("status") == "completed"
        cleanup = manifest.get("cleanup_complete") is True
        goal = manifest.get("goal")
        enabled_goal = invocation.acceptance["contract"].get("goal")
        enabled_goal = enabled_goal.get("enabled", False) if isinstance(enabled_goal, Mapping) else bool(enabled_goal)
        goal_complete = isinstance(goal, dict) and goal.get("status") == "complete"
        answer = manifest.get("answer_present") is True
        artifact = manifest.get("result_json_artifact")
        output = None
        if terminal and artifact == "artifacts/result.json":
            output = json.loads((run_dir / artifact).read_text())
        accepted = terminal and cleanup and artifact == "artifacts/result.json" and (
            goal_complete if enabled_goal else answer)
        status = "completed" if terminal and cleanup else (
            "cancelled" if manifest.get("status") == "interrupted" else "incomplete")
        acceptance = {"answer_present": answer, "goal": goal, "original_contract": True,
                      "runtime_cleanup": cleanup,
                      "basis": "existing complete Supervisor lifecycle and output contract"}
        full_run = {**run, "started_at": record["started_at"],
                    "ended_at": manifest.get("ended_at", record["started_at"]),
                    "answer_present": answer, "goal": goal}
        return ExecutionReceipt(context.trial_id, context.candidate_id, status, output,
            accepted=accepted, acceptance=acceptance, runs=(full_run,),
            artifacts=({"path": str(run_dir / "artifacts/result.json"), "kind": "typed_result"},) if terminal else (),
            usage=read_run_usage(run_dir), cleanup_complete=cleanup,
            error=error or ("" if accepted else str(manifest.get("error") or "Task acceptance or cleanup incomplete")))

    def reconcile(self, invocation, bundle, context):
        record_path = context.business_root / "supervisor-run.json"
        if not record_path.is_file():
            return None
        record = json.loads(record_path.read_text())
        manifest_path = Path(record["run"]["manifest_path"])
        if not manifest_path.resolve().is_relative_to(context.runtime_root):
            raise ValueError("Supervisor receipt escaped its Trial Runtime")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("status") == "completed":
            # Even a crash in the cleanup tail must not replay a completed
            # business Task merely to improve bookkeeping.
            return self._receipt(invocation, context, record)
        if manifest.get("status") in {"failed", "interrupted"}:
            return self._receipt(invocation, context, record)
        # A killed process may resume only its existing checkpoint. execute_once
        # supplies that Task ID; the ordinary runner validates all fingerprints.
        return None

    def execute_once(self, invocation, bundle, context):
        from agentloom.app.runner import _execute_app_once
        from agentloom.app.run import ApplicationRunError, ApplicationRunInterrupted

        record_path = context.business_root / "supervisor-run.json"
        context.business_root.mkdir(parents=True, exist_ok=True)
        previous = json.loads(record_path.read_text()) if record_path.is_file() else None

        def observe(event):
            if event.event == "run.started":
                _write(record_path, {"run": _run_dict(event.run), "started_at": event.occurred_at.isoformat()})

        try:
            relative = (bundle.application_root / invocation.data["definition"]).relative_to(bundle.project_root)
            result = _execute_app_once(relative.as_posix(),
                resume_task_id=previous["run"]["task_id"] if previous else None,
                file_logging=invocation.request.driver_options.get("file_logging"),
                event_sink=observe, require_valid_supervisor_target=True)
        except (ApplicationRunError, ApplicationRunInterrupted) as error:
            if not record_path.is_file():
                raise
            return self._receipt(invocation, context, json.loads(record_path.read_text()), error=str(error))
        record = json.loads(record_path.read_text())
        record["started_at"] = result.started_at.isoformat()
        _write(record_path, record)
        return self._receipt(invocation, context, record)

    def publish(self, invocation, selected, decision_id):
        # The selected artifact already exists. No second model execution or
        # replacement of a live business database is needed to return it.
        return {"decision_id": decision_id, "trial_id": selected.trial_id,
                "candidate_id": selected.candidate_id, "runs": json_value(selected.runs)}

    def execute_ordinary(self, request, *, events=None, stop_token=None, resume_id=None):
        from agentloom.app.runner import _execute_app_once
        if stop_token is not None and stop_token.is_set():
            raise InterruptedError("Application stopped before execution")
        return _execute_app_once(request.application_root / request.driver_options["definition"],
            resume_task_id=resume_id, file_logging=request.driver_options.get("file_logging"), event_sink=events)


def dispatch_supervisor(yaml_path, *, resume_task_id, file_logging, event_sink,
                        trace_exporter, harness_resume_id, stop_token):
    """Return None for ordinary apps; otherwise return the selected real Run."""
    from agentloom.app.application import _system_settings
    from agentloom.app.harnessing.policy import resolve_harnessing_policy
    from agentloom.app.run import ApplicationRunResult, RunLifecycleEvent
    from agentloom.app.runner import _emit_lifecycle_event, _resolve_yaml_path
    from agentloom.config.config import get_config
    from agentloom.config.paths import configuration_root

    base = get_config()
    path = _resolve_yaml_path(yaml_path)
    root = configuration_root(path, base.agent_root)
    app_config, _ = _system_settings(base, root)
    policy = resolve_harnessing_policy(base.raw, app_config)
    if not policy.enabled:
        if harness_resume_id is not None:
            raise ValueError("harness_resume_id requires Application harnessing.enabled")
        return None
    if resume_task_id is not None:
        raise ValueError("Harness experiments resume with harness_resume_id, not a child Task ID")
    request = ApplicationRequest(root.relative_to(base.agent_root / "applications").as_posix(),
        base.agent_root, root, "agentloom.app.harnessing.supervisor:SupervisorDriver",
        driver_options={"definition": path.relative_to(root).as_posix(), "file_logging": file_logging})
    from .controller import run_application
    environment = {key: os.environ[key] for key in (
        "PATH", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR") if key in os.environ}
    result = run_application(request, policy=policy, resume_id=harness_resume_id,
                             stop_token=stop_token, environment=environment)
    # Observers receive the original child identities/timestamps. The process
    # boundary never transports arbitrary Python callbacks into a candidate.
    for trial in result.trials:
        for record in trial.runs:
            run = _public_run(record)
            start = datetime.fromisoformat(record["started_at"])
            end = datetime.fromisoformat(record["ended_at"])
            metadata = {"experiment_id": result.experiment_id, "trial_id": trial.trial_id,
                        "candidate_id": trial.candidate_id,
                        "selected": trial.trial_id == result.selected_trial_id}
            _emit_lifecycle_event(event_sink, RunLifecycleEvent("run.started", run, start,
                                                              harnessing=metadata))
            event = "run.completed" if trial.status == "completed" else "run.failed"
            _emit_lifecycle_event(event_sink, RunLifecycleEvent(event, run, end,
                output=json_value(trial.output), answer_present=record.get("answer_present"),
                goal=record.get("goal"), error=trial.error or None,
                harnessing={**result.to_dict(), **metadata} if metadata["selected"] else metadata))
            if trace_exporter is not None:
                _export_trace(trace_exporter, run)
    if not result.selected_trial_id or not result.delivery:
        raise HarnessingApplicationError(result)
    selected = next(trial for trial in result.trials if trial.trial_id == result.selected_trial_id)
    record = selected.runs[-1]
    return ApplicationRunResult(json_value(selected.output), _public_run(record),
        datetime.fromisoformat(record["started_at"]), datetime.fromisoformat(record["ended_at"]),
        goal=record.get("goal"), answer_present=record.get("answer_present", False),
        harnessing=result)


def _public_run(record):
    from agentloom.app.run import RunInfo
    return RunInfo(**{key: Path(record[key]) if record.get(key) is not None else None
                      for key in ("run_dir", "manifest_path", "log_path", "trace_dir")},
                   **{key: record[key] for key in ("application_id", "task_id", "run_id")})


def _export_trace(exporter, run):
    if run.trace_dir is None:
        return
    from agentloom.execution.storage import SecureDirectory
    from agentloom.execution.observability import RunTrace
    from agentloom.execution.trace_export import AsyncTraceExport
    with SecureDirectory(run.trace_dir) as storage:
        delivery = AsyncTraceExport(exporter, storage)
        try:
            for event in RunTrace(storage, run.run_id).events():
                delivery.enqueue(event)
        finally:
            delivery.close()
