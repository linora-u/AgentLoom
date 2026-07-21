"""Project and runtime projections exposed through the TUI bridge."""

from __future__ import annotations

import copy
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import yaml

from src.lib.runtime.context import (
    RuntimeRunLease,
    safe_application_id,
    validate_runtime_id,
)
from src.lib.runtime.storage import SecureDirectory

if TYPE_CHECKING:
    from src.tui_bridge.definition import AgentDefinitionCache

# ``run.detail`` is refreshed while its panel is open.  These are response and
# filesystem work budgets, not pagination defaults: one refresh must remain
# bounded even when a task has accumulated years of events or thousands of
# files.  The DTO reports every limit so the UI never presents a preview as the
# complete record.
RUN_DETAIL_EVENT_SCAN_MAX_BYTES = 1024 * 1024
RUN_DETAIL_EVENT_MAX_COUNT = 256
RUN_DETAIL_EVENT_MAX_BYTES = 256 * 1024
RUN_DETAIL_WORKER_MAX_COUNT = 256
RUN_DETAIL_LOG_MAX_FILES = 16
RUN_DETAIL_LOG_MAX_TOTAL_BYTES = 128 * 1024
RUN_DETAIL_LOG_MAX_BYTES_PER_FILE = 16 * 1024
RUN_DETAIL_ARTIFACT_MAX_FILES = 256
RUN_DETAIL_FILE_SCAN_MAX_ENTRIES = 4096
RUN_DETAIL_RESULT_MAX_BYTES = 256 * 1024
RUNTIME_TASK_PROJECTION_MAX_BYTES = 1024 * 1024
RUN_MANIFEST_MAX_BYTES = 256 * 1024
RUNTIME_LIVE_RUN_MAX_COUNT = 256
# A live poll reads at most 24 manifests (256 KiB each) and their 64 KiB task
# projections: 7.5 MiB of requested input even under an adversarial Run burst.
RUNTIME_LIVE_RECORD_READ_MAX_COUNT = 24
RUNTIME_LIVE_TASK_PROJECTION_MAX_BYTES = 64 * 1024
RUNTIME_LIVE_PENDING_RETRY_MAX_COUNT = 2

_UNSCANNED_DIRECTORY_VERSION = (-1, -1, -1, -1)
_TERMINAL_RUN_STATUS_ALIASES = {
    "completed": "completed",
    "success": "completed",
    "succeeded": "completed",
    "failed": "failed",
    "error": "failed",
    "interrupted": "interrupted",
    "cancelled": "interrupted",
    "canceled": "interrupted",
    "crashed": "crashed",
    "unknown": "unknown",
}


@dataclass
class _RuntimeLiveIndex:
    """Bootstrap-owned Run index incrementally refreshed by ``runtime.summary``."""

    records: dict[tuple[str, str], dict[str, Any]]
    ordered_keys: list[tuple[str, str]]
    active_keys: set[tuple[str, str]]
    latest_by_system: dict[str, tuple[str, str]]
    application_ids: set[str]
    systems_by_application: dict[str, list[str]]
    directory_versions: dict[str, tuple[int, int, int, int] | None]
    directory_run_cursors: dict[str, str | None]
    pending_run_dirs: dict[tuple[str, str], Path]
    pending_attempts: dict[tuple[str, str], int]
    worker_invocations: list[dict[str, Any]]
    worker_invocations_incomplete: bool


class BridgeError(RuntimeError):
    """A stable, user-safe RPC error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TuiBridge:
    """Project projection and bounded Builder operations for one project."""

    def __init__(self, project_root: Path, *, builder_service: Any | None = None) -> None:
        self.project_root = project_root.expanduser().resolve()
        # Model/Agent construction is intentionally lazy. Read-only workspace
        # observation must not import LiteLLM or the complete execution stack.
        self._builder = builder_service
        # ``runtime.summary`` is a live projection, not a second bootstrap.  A
        # successful bootstrap records the validated static Agent identities
        # and resolved runtime root so later refreshes never rescan YAML,
        # Applications, or Skills.
        self._runtime_seed: (
            tuple[
                list[dict[str, Any]],
                Path,
                dict[str, Path],
            ]
            | None
        ) = None
        self._runtime_index: _RuntimeLiveIndex | None = None

    def _builder_service(self) -> Any:
        if self._builder is None:
            from src.tui_bridge.builder import BuilderService

            self._builder = BuilderService(self.project_root)
        return self._builder

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch(method, params, event_sink=None)

    def dispatch_with_events(
        self,
        method: str,
        params: dict[str, Any],
        event_sink: Any,
    ) -> dict[str, Any]:
        return self._dispatch(method, params, event_sink=event_sink)

    def _dispatch(
        self,
        method: str,
        params: dict[str, Any],
        *,
        event_sink: Any | None,
    ) -> dict[str, Any]:
        if method == "bootstrap":
            return self.bootstrap()
        if method == "runtime.summary":
            self._exact_params(params, set(), method="runtime.summary")
            return self.runtime_summary()
        if method == "system.detail":
            system_id = params.get("system_id")
            if not isinstance(system_id, str) or not system_id:
                raise BridgeError("invalid_params", "system_id must be a non-empty string")
            return self.system_detail(system_id)
        if method == "application.detail":
            self._exact_params(params, {"application_id"}, method="application.detail")
            application_id = self._required_wire_string(
                params.get("application_id"), field="application_id"
            )
            try:
                canonical = safe_application_id(application_id)
            except ValueError as error:
                raise BridgeError("invalid_params", str(error)) from error
            if canonical != application_id:
                raise BridgeError("invalid_params", "application_id is not canonical")
            from src.tui_bridge.application_studio import application_detail

            try:
                return application_detail(
                    self.project_root,
                    application_id,
                    systems=self._scan_systems(),
                )
            except FileNotFoundError as error:
                raise BridgeError("not_found", f"application not found: {application_id}") from error
        if method == "run.detail":
            run_id = params.get("run_id")
            application_id = params.get("application_id")
            system_id = params.get("system_id")
            if not isinstance(run_id, str) or not run_id:
                raise BridgeError("invalid_params", "run_id must be a non-empty string")
            if not isinstance(application_id, str) or not application_id:
                raise BridgeError("invalid_params", "application_id must be a non-empty string")
            if system_id is not None and (not isinstance(system_id, str) or not system_id):
                raise BridgeError("invalid_params", "system_id must be a non-empty string")
            return self.run_detail(
                run_id,
                application_id=application_id,
                system_id=system_id,
            )
        if method == "schedule.add":
            return self._schedule_add(params)
        if method in {"schedule.pause", "schedule.resume", "schedule.remove"}:
            return self._schedule_mutation(method, params)
        if method in {"assistant.send", "builder.send"}:
            session_id = params.get("session_id")
            message = params.get("message")
            model_type = params.get("model_type")
            if not isinstance(session_id, str) or not session_id.strip():
                raise BridgeError("invalid_params", "session_id must be a non-empty string")
            if not isinstance(message, str) or not message.strip():
                raise BridgeError("invalid_params", "message must be a non-empty string")
            if model_type is not None and (not isinstance(model_type, str) or not model_type.strip()):
                raise BridgeError("invalid_params", "model_type must be a non-empty string")
            try:
                send_params: dict[str, Any] = {
                    "session_id": session_id,
                    "message": message,
                    "model_type": model_type,
                }
                if event_sink is not None:
                    send_params["on_event"] = event_sink
                return self._builder_service().send(
                    **send_params,
                )
            except Exception as error:
                # ChatAgentError is the only provider failure whose code and
                # message are explicitly safe for the UI. Keep the import lazy
                # so read-only workspace observation does not load an SDK.
                from src.tui_bridge.chat_agent import ChatAgentError

                if isinstance(error, ChatAgentError):
                    raise BridgeError(error.code, str(error)) from error
                if isinstance(error, ValueError):
                    raise BridgeError("builder_failed", str(error)) from error
                raise BridgeError(
                    "builder_failed",
                    "Builder model call failed; retry or select another configured model.",
                ) from error
        if method == "builder.draft":
            session_id = params.get("session_id")
            if not isinstance(session_id, str) or not session_id.strip():
                raise BridgeError("invalid_params", "session_id must be a non-empty string")
            return self._builder_service().get_draft(session_id)
        if method == "draft.apply":
            from src.tui_bridge.builder import DraftConflictError

            session_id = params.get("session_id")
            expected_revision = params.get("expected_revision")
            if not isinstance(session_id, str) or not session_id.strip():
                raise BridgeError("invalid_params", "session_id must be a non-empty string")
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise BridgeError("invalid_params", "expected_revision must be an integer")
            try:
                return self._builder_service().apply_draft(
                    session_id=session_id,
                    expected_revision=expected_revision,
                )
            except DraftConflictError as error:
                raise BridgeError("draft_conflict", str(error)) from error
            except ValueError as error:
                raise BridgeError("builder_failed", str(error)) from error
            except RuntimeError as error:
                message = str(error)
                if message.startswith("Agent draft apply failed and rollback was incomplete:"):
                    raise BridgeError("builder_failed", message) from error
                raise BridgeError(
                    "builder_failed",
                    "Agent draft apply failed; no recovery details are available.",
                ) from error
        raise BridgeError("method_not_found", f"unknown method: {method}")

    @staticmethod
    def _exact_params(
        params: dict[str, Any],
        expected: set[str],
        *,
        method: str,
    ) -> None:
        actual = set(params)
        if actual == expected:
            return
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise BridgeError(
            "invalid_params",
            f"{method} params are invalid ({'; '.join(details)})",
        )

    @staticmethod
    def _required_wire_string(value: Any, *, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise BridgeError("invalid_params", f"{field} must be a non-empty string")
        return value

    def _schedule_target(self, yaml_path: str) -> Path:
        relative = Path(yaml_path)
        parts = relative.parts
        candidate = self.project_root / relative
        try:
            workflow_index = parts.index("workflows")
        except ValueError:
            workflow_index = -1
        structurally_valid = (
            yaml_path == yaml_path.strip()
            and "\\" not in yaml_path
            and not relative.is_absolute()
            and len(parts) >= 4
            and parts[0] == "applications"
            and workflow_index >= 2
            and workflow_index < len(parts) - 1
            and "worker_agents" not in parts
            and relative.suffix.lower() in {".yaml", ".yml"}
        )
        try:
            if not structurally_valid or self._has_symlink_component(candidate, self.project_root):
                raise ValueError
            resolved = candidate.resolve(strict=True)
            canonical = resolved.relative_to(self.project_root).as_posix()
        except (OSError, ValueError) as error:
            raise BridgeError(
                "invalid_params",
                "yaml_path must identify a real, non-symlink project supervisor YAML",
            ) from error
        if canonical != yaml_path or not resolved.is_file():
            raise BridgeError(
                "invalid_params",
                "yaml_path must identify a real, non-symlink project supervisor YAML",
            )
        try:
            summary, validated_path, _ = self._direct_system_summary(yaml_path)
        except BridgeError as error:
            raise BridgeError(
                "invalid_params",
                "yaml_path must identify a real, non-symlink project supervisor YAML",
            ) from error
        validation = summary.get("validation")
        if not isinstance(validation, dict) or validation.get("valid") is not True:
            raise BridgeError(
                "invalid_params",
                "yaml_path must identify a valid supervisor Agent",
            )
        if validated_path != resolved:
            raise BridgeError(
                "invalid_params",
                "yaml_path changed while it was being validated",
            )
        return resolved

    @classmethod
    def _schedule_from_wire(cls, raw: Any) -> dict[str, Any]:
        from src.schedules.schedule import cron_schedule, interval_schedule, once_schedule

        if not isinstance(raw, dict):
            raise BridgeError("invalid_params", "schedule must be an object")
        kind = raw.get("kind")
        if not isinstance(kind, str):
            raise BridgeError("invalid_params", "schedule.kind must be a string")
        expected_by_kind = {
            "once": {"kind", "at", "timezone"},
            "interval": {"kind", "every", "timezone"},
            "cron": {"kind", "expression", "timezone"},
        }
        expected = expected_by_kind.get(kind)
        if expected is None:
            raise BridgeError("invalid_params", f"unknown schedule kind: {kind!r}")
        cls._exact_params(raw, expected, method="schedule")
        timezone = cls._required_wire_string(raw["timezone"], field="schedule.timezone")
        if timezone != timezone.strip():
            raise BridgeError(
                "invalid_params",
                "schedule.timezone must not contain surrounding whitespace",
            )
        try:
            if kind == "once":
                at = cls._required_wire_string(raw["at"], field="schedule.at")
                if at != at.strip():
                    raise ValueError("schedule.at must not contain surrounding whitespace")
                return once_schedule(at, timezone=timezone)
            if kind == "interval":
                every = cls._required_wire_string(raw["every"], field="schedule.every")
                if every != every.strip():
                    raise ValueError("schedule.every must not contain surrounding whitespace")
                return interval_schedule(every, timezone=timezone)
            expression = cls._required_wire_string(
                raw["expression"],
                field="schedule.expression",
            )
            if expression != expression.strip():
                raise ValueError("schedule.expression must not contain surrounding whitespace")
            return cron_schedule(expression, timezone=timezone)
        except ValueError as error:
            raise BridgeError("invalid_params", str(error)) from error

    @staticmethod
    def _schedule_result(action: str, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": action,
            "job_id": str(job["id"]),
            "name": str(job["name"]),
            "state": str(job["state"]),
        }

    @staticmethod
    def _raise_schedule_store_error(error: Exception) -> NoReturn:
        from src.schedules.store import JobBusyError, JobNotFoundError

        if isinstance(error, JobNotFoundError):
            raise BridgeError("not_found", str(error)) from error
        if isinstance(error, JobBusyError):
            raise BridgeError("busy", str(error)) from error
        raise BridgeError("schedule_failed", str(error)) from error

    def _schedule_add(self, params: dict[str, Any]) -> dict[str, Any]:
        from src.schedules.store import ScheduleStore, ScheduleStoreError

        self._exact_params(
            params,
            {"yaml_path", "name", "schedule"},
            method="schedule.add",
        )
        yaml_path = self._required_wire_string(params["yaml_path"], field="yaml_path")
        name = params["name"]
        if not isinstance(name, str):
            raise BridgeError("invalid_params", "name must be a string")
        target = self._schedule_target(yaml_path)
        schedule = self._schedule_from_wire(params["schedule"])

        def validate_before_commit(candidate: dict[str, Any]) -> None:
            persisted_target = self._schedule_target(yaml_path)
            if persisted_target != target or candidate.get("yaml_path") != yaml_path:
                raise BridgeError(
                    "invalid_params",
                    "yaml_path changed while the schedule was being added",
                )

        try:
            job = ScheduleStore(self.project_root).add_job(
                name=name,
                yaml_path=target,
                schedule=schedule,
                validate_before_commit=validate_before_commit,
            )
        except ValueError as error:
            raise BridgeError("invalid_params", str(error)) from error
        except ScheduleStoreError as error:
            self._raise_schedule_store_error(error)
        return self._schedule_result("add", job)

    def _schedule_mutation(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        from src.schedules.store import ScheduleStore, ScheduleStoreError

        self._exact_params(params, {"job_id"}, method=method)
        job_id = self._required_wire_string(params["job_id"], field="job_id")
        if job_id != job_id.strip():
            raise BridgeError(
                "invalid_params",
                "job_id must not contain surrounding whitespace",
            )
        action = method.removeprefix("schedule.")
        store = ScheduleStore(self.project_root)
        try:
            mutation = getattr(store, action)
            job = mutation(job_id)
        except ValueError as error:
            raise BridgeError("invalid_params", str(error)) from error
        except ScheduleStoreError as error:
            self._raise_schedule_store_error(error)
        return self._schedule_result(action, job)

    def bootstrap(self) -> dict[str, Any]:
        definition_cache: AgentDefinitionCache = {}
        runtime_root = self._runtime_root()
        systems, runs, run_records = self._snapshot(
            definition_cache=definition_cache,
            runtime_root=runtime_root,
        )
        from src.tui_bridge.catalog import project_catalog

        catalog = project_catalog(
            self.project_root,
            systems,
            runs,
            definition_cache=definition_cache,
        )
        worker_invocations, worker_invocations_incomplete = self._latest_worker_invocations(
            runs,
            run_records,
        )
        result = {
            "project": {
                "root": str(self.project_root),
                "name": self.project_root.name,
            },
            "models": self._model_catalog(),
            "systems": systems,
            "runs": runs,
            "worker_invocations": worker_invocations,
            "worker_invocations_incomplete": worker_invocations_incomplete,
            **catalog,
        }
        # Assign one immutable seed reference only after every bootstrap
        # projection succeeded.  Reset runtime-owned fields so each refresh is
        # derived solely from the newly scanned Run summaries.
        static_systems = copy.deepcopy(systems)
        for system in static_systems:
            system["state"] = "never_run"
            system["latest_run"] = None
        self._runtime_seed = (
            static_systems,
            runtime_root,
            self._system_identity_paths(static_systems),
        )
        self._runtime_index = self._build_runtime_live_index(
            systems=static_systems,
            runs=runs,
            records=run_records,
            worker_invocations=worker_invocations,
            worker_invocations_incomplete=worker_invocations_incomplete,
        )
        return result

    def runtime_summary(self) -> dict[str, Any]:
        seed = self._runtime_seed
        if seed is None:
            raise BridgeError(
                "not_ready",
                "runtime.summary requires a successful bootstrap",
            )
        index = self._runtime_index
        if index is None:
            raise BridgeError(
                "not_ready",
                "runtime.summary requires a successful bootstrap",
            )
        cached_systems, runtime_root, system_paths = seed
        systems = copy.deepcopy(cached_systems)
        changed_keys, removed_keys, discovery_incomplete = self._refresh_runtime_live_index(
            index,
            runtime_root=runtime_root,
            system_paths=system_paths,
        )
        self._merge_runtime_index_state(systems, index)
        runs, runs_incomplete = self._runtime_live_run_window(
            index,
            changed_keys=changed_keys,
            discovery_incomplete=discovery_incomplete,
        )
        if changed_keys or removed_keys:
            indexed_runs = [index.records[key]["summary"] for key in index.ordered_keys if key in index.records]
            worker_invocations, worker_invocations_incomplete = self._latest_worker_invocations(
                indexed_runs,
                index.records,
            )
            index.worker_invocations = copy.deepcopy(worker_invocations)
            index.worker_invocations_incomplete = worker_invocations_incomplete

        from src.tui_bridge.catalog import schedule_catalog

        return {
            "systems": systems,
            "runs": runs,
            "runs_incomplete": runs_incomplete,
            "removed_runs": [
                {"application_id": application_id, "run_id": run_id} for application_id, run_id in sorted(removed_keys)
            ],
            "worker_invocations": copy.deepcopy(index.worker_invocations),
            "worker_invocations_incomplete": (index.worker_invocations_incomplete or discovery_incomplete),
            "schedules": schedule_catalog(self.project_root),
        }

    def system_detail(self, system_id: str) -> dict[str, Any]:
        summary, supervisor_path, definition = self._direct_system_summary(system_id)
        runs, run_records = self._scan_runs([summary])
        self._merge_runtime_state([summary], runs)
        worker_files = self._worker_files(supervisor_path, definition)
        files = [self._file_summary(supervisor_path, "supervisor")]
        workers: list[dict[str, Any]] = []
        for worker_path in worker_files:
            worker_definition, _ = self._read_definition(worker_path)
            files.append(self._file_summary(worker_path, "worker"))
            workers.append(
                {
                    "name": str(worker_definition.get("name") or worker_path.stem),
                    "path": worker_path.relative_to(self.project_root).as_posix(),
                    "description": str(worker_definition.get("description") or ""),
                }
            )

        latest_run = summary["latest_run"]
        if latest_run is None:
            result_state = "never_run"
        elif latest_run["status"] == "running":
            result_state = "running"
        else:
            record = run_records.get((latest_run["application_id"], latest_run["run_id"]))
            result_state = "available" if record is not None and self._run_result(record)[0] else "unavailable"

        return {
            "summary": summary,
            "definition": {
                "name": str(definition.get("name") or supervisor_path.stem),
                "description": str(definition.get("description") or ""),
                "workflow": self._workflow_text(definition.get("workflow")),
                "model_type": str(definition.get("model_type") or ""),
                "path": system_id,
            },
            "files": files,
            "topology": {
                "supervisor": {
                    "name": str(definition.get("name") or supervisor_path.stem),
                    "path": system_id,
                },
                "workers": workers,
            },
            "execution": {
                "state": summary["state"],
                "latest_run": summary["latest_run"],
            },
            "result_state": result_state,
        }

    def _direct_system_summary(
        self,
        system_id: str,
    ) -> tuple[dict[str, Any], Path, dict[str, Any]]:
        from src.tui_bridge.definition import model_types, validate_agent_definition

        relative = Path(system_id)
        candidate = self.project_root / relative
        applications_root = self.project_root / "applications"
        try:
            if relative.is_absolute() or self._has_symlink_component(candidate, self.project_root):
                raise ValueError
            resolved = candidate.resolve(strict=True)
            canonical = resolved.relative_to(self.project_root).as_posix()
        except (OSError, ValueError) as error:
            raise BridgeError("not_found", f"system not found: {system_id}") from error
        if (
            canonical != system_id
            or not resolved.is_file()
            or applications_root not in resolved.parents
            or "workflows" not in relative.parts
            or "worker_agents" in relative.parts
            or resolved.suffix.lower() not in {".yaml", ".yml"}
        ):
            raise BridgeError("not_found", f"system not found: {system_id}")

        definition, read_errors = self._read_definition(resolved)
        errors = read_errors + validate_agent_definition(
            self.project_root,
            system_id,
            definition,
            catalog=model_types(self.project_root),
        )
        return (
            {
                "id": system_id,
                "path": system_id,
                "application_id": self._application_id(resolved),
                "name": str(definition.get("name") or resolved.stem),
                "description": str(definition.get("description") or ""),
                "state": "never_run",
                "validation": {"valid": not errors, "errors": errors},
                "latest_run": None,
            },
            resolved,
            definition,
        )

    def run_detail(
        self,
        run_id: str,
        *,
        application_id: str,
        system_id: str | None,
    ) -> dict[str, Any]:
        record = self._run_record(
            application_id=application_id,
            run_id=run_id,
            system_id=system_id,
        )
        summary = record["summary"]
        events, semantic_events, event_limits = self._task_events(record)
        workers, workers_truncated = self._workers(record, events=semantic_events)
        workers_truncated = workers_truncated or event_limits["truncated"]
        available, result, result_limits = self._run_result(
            record,
            events=semantic_events,
            events_incomplete=event_limits["source_incomplete"],
        )
        logs, log_limits = self._log_files(record["run_dir"] / "logs")
        artifacts, artifact_limits = self._artifact_files(record["run_dir"] / "artifacts")
        if summary["status"] == "running":
            result_state = "running"
            result = None
            result_limits = self._result_limits()
        elif available:
            result_state = "available"
        else:
            result_state = "unavailable"
            result = None
        return {
            "summary": summary,
            "error": self._run_error(record, events=semantic_events),
            "workers": workers,
            "events": events,
            "logs": logs,
            "artifacts": artifacts,
            "result_state": result_state,
            "result": result,
            "limits": {
                "workers": {
                    "truncated": workers_truncated,
                    "returned_count": len(workers),
                    "max_count": RUN_DETAIL_WORKER_MAX_COUNT,
                },
                "events": event_limits,
                "logs": log_limits,
                "artifacts": artifact_limits,
                "result": result_limits,
            },
        }

    def _run_record(
        self,
        *,
        application_id: str,
        run_id: str,
        system_id: str | None,
    ) -> dict[str, Any]:
        try:
            canonical_application = safe_application_id(application_id)
            canonical_run = validate_runtime_id(run_id, field="run_id")
        except ValueError as error:
            raise BridgeError("invalid_params", str(error)) from error
        if (canonical_application, canonical_run) != (application_id, run_id):
            raise BridgeError("invalid_params", "application_id or run_id is not canonical")

        runtime_root = self._runtime_root()
        runs_root = runtime_root / "runs"
        run_dir = runs_root / Path(*application_id.split("/")) / run_id
        manifest_path = run_dir / "manifest.json"
        manifest = self._read_json_object_bounded_secure(
            runs_root,
            Path(*application_id.split("/")) / run_id / "manifest.json",
            max_bytes=RUN_MANIFEST_MAX_BYTES,
        )
        if manifest is None or self._canonical_run_dir(runs_root, manifest_path, manifest) is None:
            raise BridgeError("not_found", f"run not found: {run_id}")

        task_id = str(manifest["task_id"])
        task = self._run_task_projection(
            manifest,
            run_dir=run_dir,
            runtime_root=runtime_root,
            application_id=application_id,
            task_id=task_id,
            checkpoint_cache={},
        )

        linked_system_id = self._validate_run_system(
            manifest,
            application_id=application_id,
            system_id=system_id,
        )
        summary = {
            "run_id": run_id,
            "system_id": linked_system_id,
            "application_id": application_id,
            "task_id": task_id,
            "agent_name": str(manifest.get("agent_name") or (task or {}).get("agent_name") or ""),
            "status": self._run_status(manifest, task=task, run_dir=run_dir),
            "started_at": self._optional_string(manifest.get("started_at")),
            "ended_at": self._optional_string(manifest.get("ended_at")),
        }
        return {
            "summary": summary,
            "manifest": manifest,
            "run_dir": run_dir,
            "task": task,
            "runtime_root": runtime_root,
        }

    def _validate_run_system(
        self,
        manifest: dict[str, Any],
        *,
        application_id: str,
        system_id: str | None,
    ) -> str | None:
        if system_id is None:
            return None
        relative_candidate = Path(system_id)
        candidate = self.project_root / relative_candidate
        try:
            if relative_candidate.is_absolute() or self._has_symlink_component(
                candidate,
                self.project_root,
            ):
                raise ValueError
            resolved = candidate.resolve(strict=True)
            relative = resolved.relative_to(self.project_root)
        except (OSError, ValueError) as error:
            raise BridgeError("invalid_params", "system_id must identify a project Agent") from error
        applications_root = self.project_root / "applications"
        if (
            relative.as_posix() != system_id
            or not resolved.is_file()
            or applications_root not in resolved.parents
            or "workflows" not in relative.parts
            or "worker_agents" in relative.parts
            or resolved.suffix.lower() not in {".yaml", ".yml"}
            or self._application_id(resolved) != application_id
        ):
            raise BridgeError("invalid_params", "system_id must identify the Run's Agent System")

        yaml_path = manifest.get("yaml_path")
        if isinstance(yaml_path, str) and yaml_path:
            configured = Path(yaml_path).expanduser()
            if not configured.is_absolute():
                configured = self.project_root / configured
            try:
                manifest_system = configured.resolve()
            except OSError:
                manifest_system = configured.absolute()
            if manifest_system != resolved:
                raise BridgeError("not_found", f"run not found for system: {system_id}")
        return system_id

    def _run_error(
        self,
        record: dict[str, Any],
        *,
        events: list[dict[str, Any]],
    ) -> str | None:
        manifest_error = self._optional_string(record["manifest"].get("error"))
        if manifest_error is not None:
            return manifest_error
        for event in reversed(events):
            if event.get("type") == "task_status_changed":
                event_error = self._optional_string(event.get("error"))
                if event_error is not None:
                    return event_error
        task = record.get("task")
        if isinstance(task, dict):
            task_error = self._optional_string(task.get("error"))
            if task_error is not None:
                return task_error
        status = str(record["summary"].get("status") or "").strip().lower()
        if status == "interrupted":
            return "Execution was interrupted before completion."
        if status == "crashed":
            return "Execution stopped unexpectedly before completion."
        if status == "unknown":
            return "Run status could not be determined from stored metadata."
        return None

    def _snapshot(
        self,
        *,
        definition_cache: AgentDefinitionCache | None = None,
        runtime_root: Path | None = None,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[tuple[str, str], dict[str, Any]],
    ]:
        systems = self._scan_systems(definition_cache=definition_cache)
        runs, records = self._scan_runs(systems, runtime_root=runtime_root)
        self._merge_runtime_state(systems, runs)
        return systems, runs, records

    @staticmethod
    def _merge_runtime_state(
        systems: list[dict[str, Any]],
        runs: list[dict[str, Any]],
    ) -> None:
        runs_by_system: dict[str, list[dict[str, Any]]] = {}
        for run in runs:
            linked_system = run["system_id"]
            if linked_system is not None:
                runs_by_system.setdefault(linked_system, []).append(run)
        for system in systems:
            linked_runs = runs_by_system.get(system["id"], [])
            latest = linked_runs[0] if linked_runs else None
            system["latest_run"] = latest
            if any(run["status"] == "running" for run in linked_runs):
                system["state"] = "running"
            elif latest is not None:
                system["state"] = latest["status"]
            else:
                system["state"] = "never_run"

    def _latest_worker_invocations(
        self,
        runs: list[dict[str, Any]],
        records: dict[tuple[str, str], dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        """Project the newest known invocation for every configured Run worker.

        Runtime summaries deliberately avoid the heavier event/log scan used by
        ``run.detail``. The bounded task-tree projection already read by
        ``_scan_runs`` contains the current/latest call state, step, and error.
        """

        selected: dict[
            tuple[str, str],
            tuple[tuple[int, str, int, str, str], dict[str, Any]],
        ] = {}
        runs_by_key = {(str(run["application_id"]), str(run["run_id"])): run for run in runs}
        processed_tasks: set[int] = set()
        incomplete = False
        for run in runs:
            record = records.get((str(run["application_id"]), str(run["run_id"])))
            if record is None:
                continue
            task = record.get("task")
            if not isinstance(task, dict) or id(task) in processed_tasks:
                continue
            processed_tasks.add(id(task))
            # The detail projection may omit old calls after preserving one
            # representative per Worker entity. That does not make the global
            # status incomplete: it only needs those representatives. Global
            # incompleteness means even that catalog could not be projected.
            incomplete = incomplete or bool(task.get("_worker_entities_truncated"))
            raw_workers = task.get("_worker_representatives", task.get("workers"))
            if not isinstance(raw_workers, list):
                continue
            task_run_id = self._optional_string(task.get("run_id"))
            for raw_worker in raw_workers:
                if not isinstance(raw_worker, dict):
                    continue
                application_id = str(run["application_id"])
                explicit_run_id = self._optional_string(raw_worker.get("cached_claim_run_id")) or self._optional_string(
                    raw_worker.get("attempt_run_id")
                )
                if explicit_run_id is not None:
                    candidate_run = runs_by_key.get((application_id, explicit_run_id))
                    if candidate_run is None:
                        # Retention may remove an older Run while its cumulative
                        # call remains in the current task tree. Its explicit
                        # scope is authoritative: reassigning it to the current
                        # Run could turn a stale "running" call into a false
                        # active Worker.
                        incomplete = True
                        continue
                else:
                    candidate_run = next(
                        (
                            runs_by_key.get((application_id, candidate_run_id))
                            for candidate_run_id in (task_run_id, str(run["run_id"]))
                            if candidate_run_id is not None and (application_id, candidate_run_id) in runs_by_key
                        ),
                        None,
                    )
                if candidate_run is None:
                    continue
                system_id = candidate_run.get("system_id")
                if not isinstance(system_id, str) or not system_id:
                    continue
                worker = self._checkpoint_worker_summary(raw_worker)
                parent_status = str(candidate_run.get("status") or "")
                worker_status = str(worker.get("status") or "").strip().lower()
                if parent_status in {"completed", "failed", "interrupted", "crashed"} and worker_status in {
                    "running",
                    "claimed",
                    "in_progress",
                }:
                    # Task trees are recovery checkpoints and may retain an
                    # active worker state after the canonical Run lease or
                    # manifest has become terminal. A child cannot remain live
                    # after its owning Run, so project the parent's terminal
                    # outcome without rewriting the checkpoint evidence.
                    worker = {**worker, "status": parent_status}
                    worker_status = parent_status
                agent_name = str(worker.get("agent_name") or "")
                if not agent_name:
                    continue
                key = (system_id, agent_name)
                # A live parallel call must keep the global Worker active even
                # when a newer Run/call already reached a terminal state. Once
                # none are active, timestamp then call index selects the latest
                # terminal invocation.
                rank = self._worker_projection_rank(worker, run=candidate_run)
                current = selected.get(key)
                if current is None or rank > current[0]:
                    selected[key] = (
                        rank,
                        {
                            "run_id": str(candidate_run["run_id"]),
                            "system_id": system_id,
                            "application_id": str(candidate_run["application_id"]),
                            "parent_agent_name": str(candidate_run.get("agent_name") or ""),
                            **worker,
                        },
                    )
        ordered = sorted(
            selected.values(),
            key=lambda selected_worker: (
                selected_worker[0],
                str(selected_worker[1].get("system_id") or ""),
                str(selected_worker[1].get("agent_name") or ""),
            ),
            reverse=True,
        )
        incomplete = incomplete or len(ordered) > RUN_DETAIL_WORKER_MAX_COUNT
        return (
            [candidate for _, candidate in ordered[:RUN_DETAIL_WORKER_MAX_COUNT]],
            incomplete,
        )

    @staticmethod
    def _worker_projection_rank(
        worker: dict[str, Any],
        *,
        run: dict[str, Any] | None = None,
    ) -> tuple[int, str, int, str, str]:
        run = run or {}
        status = str(worker.get("status") or "").strip().lower()
        active = status in {"running", "claimed", "in_progress"}
        if active:
            invocation_at = (
                worker.get("started_at")
                or worker.get("resume_claimed_at")
                or worker.get("claimed_at")
                or run.get("started_at")
            )
        else:
            invocation_at = (
                worker.get("ended_at")
                or worker.get("finished_at")
                or worker.get("cached_claimed_at")
                or run.get("ended_at")
                or worker.get("started_at")
                or run.get("started_at")
            )
        try:
            call_index = int(worker.get("call_index") or 0)
        except (TypeError, ValueError):
            call_index = 0
        run_id = worker.get("cached_claim_run_id") or worker.get("attempt_run_id") or run.get("run_id") or ""
        return (
            int(active),
            str(invocation_at or ""),
            call_index,
            str(run.get("started_at") or ""),
            str(run_id),
        )

    def _checkpoint_worker_summary(self, worker: dict[str, Any]) -> dict[str, Any]:
        try:
            call_index = int(worker.get("call_index") or 0)
        except (TypeError, ValueError):
            call_index = 0
        return {
            "agent_name": str(worker.get("agent_name") or ""),
            "call_index": call_index,
            "status": str(worker.get("status") or "unknown"),
            "step": worker.get("step") if isinstance(worker.get("step"), int) else None,
            "started_at": self._optional_string(
                worker.get("started_at") or worker.get("resume_claimed_at") or worker.get("claimed_at")
            ),
            "ended_at": self._optional_string(
                worker.get("finished_at") or worker.get("ended_at") or worker.get("cached_claimed_at")
            ),
            "error": self._optional_string(worker.get("error")),
        }

    def _build_runtime_live_index(
        self,
        *,
        systems: list[dict[str, Any]],
        runs: list[dict[str, Any]],
        records: dict[tuple[str, str], dict[str, Any]],
        worker_invocations: list[dict[str, Any]],
        worker_invocations_incomplete: bool,
    ) -> _RuntimeLiveIndex:
        compact_records = {key: self._compact_runtime_record(record) for key, record in records.items()}
        ordered_keys = [
            (str(run["application_id"]), str(run["run_id"]))
            for run in runs
            if (str(run["application_id"]), str(run["run_id"])) in compact_records
        ]
        active_keys = {key for key, record in compact_records.items() if record["summary"]["status"] == "running"}
        latest_by_system: dict[str, tuple[str, str]] = {}
        for key in ordered_keys:
            system_id = compact_records[key]["summary"].get("system_id")
            if isinstance(system_id, str) and system_id and system_id not in latest_by_system:
                latest_by_system[system_id] = key
        systems_by_application: dict[str, list[str]] = {}
        for system in systems:
            systems_by_application.setdefault(str(system["application_id"]), []).append(str(system["id"]))
        application_ids = set(systems_by_application)
        application_ids.update(key[0] for key in compact_records)
        return _RuntimeLiveIndex(
            records=compact_records,
            ordered_keys=ordered_keys,
            active_keys=active_keys,
            latest_by_system=latest_by_system,
            application_ids=application_ids,
            systems_by_application=systems_by_application,
            # Force one name-only reconciliation after bootstrap. This closes
            # the race where a Run directory appears during the full scan but
            # before the bootstrap index is published.
            directory_versions={application_id: _UNSCANNED_DIRECTORY_VERSION for application_id in application_ids},
            directory_run_cursors={application_id: None for application_id in application_ids},
            pending_run_dirs={},
            pending_attempts={},
            worker_invocations=copy.deepcopy(worker_invocations),
            worker_invocations_incomplete=worker_invocations_incomplete,
        )

    @staticmethod
    def _compact_runtime_record(record: dict[str, Any]) -> dict[str, Any]:
        task = record.get("task")
        compact_task: dict[str, Any] | None = None
        if isinstance(task, dict):
            compact_task = {
                key: copy.deepcopy(task[key])
                for key in (
                    "run_id",
                    "status",
                    "agent_name",
                    "workers",
                    "_worker_representatives",
                    "_workers_truncated",
                    "_worker_entities_truncated",
                    "_task_source_incomplete",
                    "_task_owned_by_run",
                )
                if key in task
            }
        return {
            "summary": copy.deepcopy(record["summary"]),
            "run_dir": record["run_dir"],
            "task": compact_task,
        }

    def _refresh_runtime_live_index(
        self,
        index: _RuntimeLiveIndex,
        *,
        runtime_root: Path,
        system_paths: dict[str, Path],
    ) -> tuple[
        set[tuple[str, str]],
        set[tuple[str, str]],
        bool,
    ]:
        runs_root = runtime_root / "runs"
        discovery_incomplete, removed_keys = self._discover_runtime_run_dirs(
            index,
            runs_root=runs_root,
        )
        needs_stable_follow_up = discovery_incomplete
        processing_incomplete = False
        changed_keys: set[tuple[str, str]] = set()
        read_budget = RUNTIME_LIVE_RECORD_READ_MAX_COUNT
        active_keys = sorted(
            index.active_keys,
            key=lambda key: self._runtime_run_rank(index.records[key]["summary"]),
            reverse=True,
        )
        pending_keys = sorted(index.pending_run_dirs, key=lambda key: key[1], reverse=True)
        candidates = [*active_keys, *(key for key in pending_keys if key not in index.active_keys)]
        if len(candidates) > read_budget:
            processing_incomplete = True

        task_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
        for key in candidates[:read_budget]:
            application_id, _ = key
            manifest_path = index.pending_run_dirs.get(key) or index.records[key]["run_dir"] / "manifest.json"
            result = self._run_record_from_manifest(
                manifest_path,
                runtime_root=runtime_root,
                runs_root=runs_root,
                paths_by_id=system_paths,
                systems_by_application=index.systems_by_application,
                task_cache=task_cache,
                task_projection_max_bytes=RUNTIME_LIVE_TASK_PROJECTION_MAX_BYTES,
            )
            if result is None:
                if key in index.pending_run_dirs:
                    self._record_pending_runtime_failure(index, key)
                if key in index.active_keys and self._runtime_manifest_is_safe_regular(
                    manifest_path, runs_root=runs_root
                ):
                    # A present but temporarily malformed/unreadable manifest
                    # is an incomplete observation, not evidence of a crash.
                    processing_incomplete = True
                elif key in index.active_keys and not self._runtime_run_is_held(index.records[key]["run_dir"]):
                    crashed = copy.deepcopy(index.records[key])
                    crashed["summary"]["status"] = "crashed"
                    if crashed != index.records[key]:
                        self._upsert_runtime_record(index, key, crashed)
                        changed_keys.add(key)
                continue
            fresh_key, record = result
            compact = self._compact_runtime_record(record)
            if fresh_key != key:
                # The canonical path validation normally makes this
                # impossible. Keep the unexpected entry pending/incomplete
                # instead of publishing a misidentified Run.
                processing_incomplete = True
                self._record_pending_runtime_failure(index, key)
                continue
            index.pending_run_dirs.pop(key, None)
            index.pending_attempts.pop(key, None)
            if compact != index.records.get(key):
                self._upsert_runtime_record(index, key, compact)
                changed_keys.add(key)

        # The first discovery pass schedules bounded new entries. Re-check
        # after parsing them so a stable, fully consumed directory is reported
        # as complete in this response, while races enqueue work for next poll.
        if needs_stable_follow_up:
            discovery_incomplete, follow_up_removed = self._discover_runtime_run_dirs(
                index,
                runs_root=runs_root,
            )
            removed_keys.update(follow_up_removed)
        changed_keys.intersection_update(index.records)
        incomplete = processing_incomplete or discovery_incomplete or bool(index.pending_run_dirs)
        return changed_keys, removed_keys, incomplete

    def _discover_runtime_run_dirs(
        self,
        index: _RuntimeLiveIndex,
        *,
        runs_root: Path,
    ) -> tuple[bool, set[tuple[str, str]]]:
        incomplete = False
        removed_keys: set[tuple[str, str]] = set()
        for application_id in sorted(index.application_ids):
            observed = self._runtime_application_directory_version(
                runs_root,
                application_id,
            )
            if observed == index.directory_versions.get(application_id):
                continue
            entries = self._runtime_application_run_dirs(runs_root, application_id)
            if entries is None:
                incomplete = True
                continue
            stable_version = self._runtime_application_directory_version(
                runs_root,
                application_id,
            )
            if stable_version != observed:
                incomplete = True
                continue

            entry_keys = {(application_id, entry.name) for entry in entries}
            known_keys = {key for key in index.records if key[0] == application_id}
            for removed_key in known_keys - entry_keys:
                self._remove_runtime_record(index, removed_key)
                removed_keys.add(removed_key)
            for vanished_pending in {
                key for key in index.pending_run_dirs if key[0] == application_id and key not in entry_keys
            }:
                index.pending_run_dirs.pop(vanished_pending, None)
                index.pending_attempts.pop(vanished_pending, None)

            entries.sort(key=lambda entry: entry.name, reverse=True)
            cursor = index.directory_run_cursors.get(application_id)
            if cursor is not None:
                # Continue after the previous bounded batch and wrap only
                # after every lower-sorted directory had a chance. This keeps
                # missing/malformed directories from starving a valid Run.
                entries = [entry for entry in entries if entry.name < cursor] + [
                    entry for entry in entries if entry.name >= cursor
                ]
            unknown = [
                (application_id, entry.name, Path(entry.path) / "manifest.json")
                for entry in entries
                if (application_id, entry.name) not in index.records
                and (application_id, entry.name) not in index.pending_run_dirs
            ]
            room = max(0, RUNTIME_LIVE_RECORD_READ_MAX_COUNT - len(index.pending_run_dirs))
            selected = unknown[:room]
            for candidate_application, run_id, manifest_path in selected:
                key = (candidate_application, run_id)
                index.pending_run_dirs[key] = manifest_path
                index.pending_attempts.setdefault(key, 0)
            if selected:
                index.directory_run_cursors[application_id] = selected[-1][1]
            application_pending = any(key[0] == application_id for key in index.pending_run_dirs)
            if unknown or application_pending:
                # Keep the old cursor: another bounded pass will reconcile
                # entries that appeared during enumeration or exceeded budget.
                incomplete = True
                continue
            index.directory_versions[application_id] = observed
        return incomplete, removed_keys

    @staticmethod
    def _record_pending_runtime_failure(
        index: _RuntimeLiveIndex,
        key: tuple[str, str],
    ) -> None:
        if key not in index.pending_run_dirs:
            return
        attempts = index.pending_attempts.get(key, 0) + 1
        if attempts < RUNTIME_LIVE_PENDING_RETRY_MAX_COUNT:
            index.pending_attempts[key] = attempts
            return
        # The per-application cursor already moved past this bounded batch.
        # Releasing it lets the next batch make progress; after one full pass
        # the cursor wraps and the directory becomes retryable again.
        index.pending_run_dirs.pop(key, None)
        index.pending_attempts.pop(key, None)

    def _runtime_application_run_dirs(
        self,
        runs_root: Path,
        application_id: str,
    ) -> list[os.DirEntry[str]] | None:
        try:
            canonical_application = safe_application_id(application_id)
        except ValueError:
            return None
        application_dir = runs_root / Path(*canonical_application.split("/"))
        if (
            canonical_application != application_id
            or runs_root.is_symlink()
            or runs_root.parent.is_symlink()
            or not runs_root.is_dir()
            or self._has_symlink_component(application_dir, runs_root)
        ):
            return None
        try:
            with os.scandir(application_dir) as iterator:
                return [
                    entry
                    for entry in iterator
                    if entry.is_dir(follow_symlinks=False)
                    and not entry.is_symlink()
                    and self._is_runtime_id(entry.name, field="run_id")
                ]
        except FileNotFoundError:
            return []
        except OSError:
            return None

    def _runtime_application_directory_version(
        self,
        runs_root: Path,
        application_id: str,
    ) -> tuple[int, int, int, int] | None:
        try:
            canonical_application = safe_application_id(application_id)
        except ValueError:
            return None
        application_dir = runs_root / Path(*canonical_application.split("/"))
        if (
            canonical_application != application_id
            or runs_root.is_symlink()
            or runs_root.parent.is_symlink()
            or self._has_symlink_component(application_dir, runs_root)
        ):
            return None
        try:
            file_stat = os.stat(application_dir, follow_symlinks=False)
        except OSError:
            return None
        if not stat.S_ISDIR(file_stat.st_mode):
            return None
        return (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_mtime_ns,
            file_stat.st_ctime_ns,
        )

    @staticmethod
    def _is_runtime_id(value: str, *, field: str) -> bool:
        try:
            return validate_runtime_id(value, field=field) == value
        except ValueError:
            return False

    @staticmethod
    def _runtime_run_is_held(run_dir: Path) -> bool:
        try:
            return RuntimeRunLease(run_dir).is_held()
        except (OSError, RuntimeError):
            return False

    def _runtime_manifest_is_safe_regular(
        self,
        manifest_path: Path,
        *,
        runs_root: Path,
    ) -> bool:
        if manifest_path.is_symlink() or self._has_symlink_component(manifest_path, runs_root):
            return False
        try:
            manifest_path.relative_to(runs_root)
            file_stat = os.stat(manifest_path, follow_symlinks=False)
        except (OSError, ValueError):
            return False
        return stat.S_ISREG(file_stat.st_mode)

    def _upsert_runtime_record(
        self,
        index: _RuntimeLiveIndex,
        key: tuple[str, str],
        record: dict[str, Any],
    ) -> None:
        is_new = key not in index.records
        index.records[key] = record
        if is_new:
            index.ordered_keys.append(key)
            index.ordered_keys.sort(
                key=lambda candidate: self._runtime_run_rank(index.records[candidate]["summary"]),
                reverse=True,
            )
            index.application_ids.add(key[0])
            index.directory_versions.setdefault(key[0], _UNSCANNED_DIRECTORY_VERSION)
        if record["summary"]["status"] == "running":
            index.active_keys.add(key)
        else:
            index.active_keys.discard(key)
        system_id = record["summary"].get("system_id")
        if isinstance(system_id, str) and system_id:
            current_key = index.latest_by_system.get(system_id)
            if current_key is None or self._runtime_run_rank(record["summary"]) > self._runtime_run_rank(
                index.records[current_key]["summary"]
            ):
                index.latest_by_system[system_id] = key

    def _remove_runtime_record(
        self,
        index: _RuntimeLiveIndex,
        key: tuple[str, str],
    ) -> None:
        record = index.records.pop(key, None)
        if record is None:
            return
        index.active_keys.discard(key)
        index.pending_run_dirs.pop(key, None)
        index.pending_attempts.pop(key, None)
        index.ordered_keys = [candidate for candidate in index.ordered_keys if candidate != key]
        system_id = record["summary"].get("system_id")
        if not isinstance(system_id, str) or index.latest_by_system.get(system_id) != key:
            return
        replacement = next(
            (
                candidate
                for candidate in index.ordered_keys
                if index.records[candidate]["summary"].get("system_id") == system_id
            ),
            None,
        )
        if replacement is None:
            index.latest_by_system.pop(system_id, None)
        else:
            index.latest_by_system[system_id] = replacement

    @staticmethod
    def _runtime_run_rank(summary: dict[str, Any]) -> tuple[str, str]:
        return (str(summary.get("started_at") or ""), str(summary.get("run_id") or ""))

    @staticmethod
    def _merge_runtime_index_state(
        systems: list[dict[str, Any]],
        index: _RuntimeLiveIndex,
    ) -> None:
        active_systems = {
            str(index.records[key]["summary"].get("system_id") or "")
            for key in index.active_keys
            if key in index.records
        }
        for system in systems:
            system_id = str(system["id"])
            latest_key = index.latest_by_system.get(system_id)
            latest = index.records[latest_key]["summary"] if latest_key is not None else None
            system["latest_run"] = copy.deepcopy(latest)
            if system_id in active_systems:
                system["state"] = "running"
            elif latest is not None:
                system["state"] = latest["status"]
            else:
                system["state"] = "never_run"

    def _runtime_live_run_window(
        self,
        index: _RuntimeLiveIndex,
        *,
        changed_keys: set[tuple[str, str]],
        discovery_incomplete: bool,
    ) -> tuple[list[dict[str, Any]], bool]:
        selected: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def extend(keys: list[tuple[str, str]]) -> None:
            for key in keys:
                if key in seen or key not in index.records:
                    continue
                if len(selected) >= RUNTIME_LIVE_RUN_MAX_COUNT:
                    return
                seen.add(key)
                selected.append(key)

        def by_rank(key: tuple[str, str]) -> tuple[str, str]:
            return self._runtime_run_rank(index.records[key]["summary"])

        extend(sorted(changed_keys, key=by_rank, reverse=True))
        extend(sorted(index.active_keys, key=by_rank, reverse=True))
        extend(
            sorted(
                set(index.latest_by_system.values()),
                key=by_rank,
                reverse=True,
            )
        )
        extend(index.ordered_keys[:RUNTIME_LIVE_RUN_MAX_COUNT])
        selected.sort(key=by_rank, reverse=True)
        incomplete = discovery_incomplete or len(selected) < len(index.records)
        return (
            [copy.deepcopy(index.records[key]["summary"]) for key in selected],
            incomplete,
        )

    def _scan_runs(
        self,
        systems: list[dict[str, Any]],
        *,
        runtime_root: Path | None = None,
        system_paths: dict[str, Path] | None = None,
    ) -> tuple[
        list[dict[str, Any]],
        dict[tuple[str, str], dict[str, Any]],
    ]:
        runtime_root = runtime_root or self._runtime_root()
        runs_root = runtime_root / "runs"
        if runtime_root.is_symlink() or not runs_root.is_dir():
            return [], {}
        task_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
        paths_by_id = dict(system_paths) if system_paths is not None else self._system_identity_paths(systems)
        systems_by_application: dict[str, list[str]] = {}
        for system in systems:
            systems_by_application.setdefault(system["application_id"], []).append(system["id"])

        records: dict[tuple[str, str], dict[str, Any]] = {}
        for manifest_path in sorted(runs_root.rglob("manifest.json")):
            result = self._run_record_from_manifest(
                manifest_path,
                runtime_root=runtime_root,
                runs_root=runs_root,
                paths_by_id=paths_by_id,
                systems_by_application=systems_by_application,
                task_cache=task_cache,
            )
            if result is not None:
                key, record = result
                records[key] = record
        ordered = sorted(
            (record["summary"] for record in records.values()),
            key=lambda summary: (summary["started_at"] or "", summary["run_id"]),
            reverse=True,
        )
        return ordered, records

    def _run_record_from_manifest(
        self,
        manifest_path: Path,
        *,
        runtime_root: Path,
        runs_root: Path,
        paths_by_id: dict[str, Path],
        systems_by_application: dict[str, list[str]],
        task_cache: dict[tuple[str, str], dict[str, Any] | None],
        task_projection_max_bytes: int = RUNTIME_TASK_PROJECTION_MAX_BYTES,
    ) -> tuple[tuple[str, str], dict[str, Any]] | None:
        if manifest_path.is_symlink() or self._has_symlink_component(manifest_path, runs_root):
            return None
        try:
            manifest_relative = manifest_path.relative_to(runs_root)
        except ValueError:
            return None
        manifest = self._read_json_object_bounded_secure(
            runs_root,
            manifest_relative,
            max_bytes=RUN_MANIFEST_MAX_BYTES,
        )
        if manifest is None:
            return None
        run_dir = self._canonical_run_dir(runs_root, manifest_path, manifest)
        if run_dir is None:
            return None
        application_id = str(manifest["application_id"])
        task_id = str(manifest["task_id"])
        run_id = str(manifest["run_id"])
        task = self._run_task_projection(
            manifest,
            run_dir=run_dir,
            runtime_root=runtime_root,
            application_id=application_id,
            task_id=task_id,
            checkpoint_cache=task_cache,
            max_bytes=task_projection_max_bytes,
        )
        system_id = self._match_system(
            manifest,
            paths_by_id=paths_by_id,
            systems_by_application=systems_by_application,
        )
        status = self._run_status(manifest, task=task, run_dir=run_dir)
        summary = {
            "run_id": run_id,
            "system_id": system_id,
            "application_id": application_id,
            "task_id": task_id,
            "agent_name": str(manifest.get("agent_name") or (task or {}).get("agent_name") or ""),
            "status": status,
            "started_at": self._optional_string(manifest.get("started_at")),
            "ended_at": self._optional_string(manifest.get("ended_at")),
        }
        return (
            (application_id, run_id),
            {
                "summary": summary,
                "manifest": manifest,
                "run_dir": run_dir,
                "task": task,
                "runtime_root": runtime_root,
            },
        )

    def _run_task_projection(
        self,
        manifest: dict[str, Any],
        *,
        run_dir: Path,
        runtime_root: Path,
        application_id: str,
        task_id: str,
        checkpoint_cache: dict[tuple[str, str], dict[str, Any] | None],
        max_bytes: int = RUNTIME_TASK_PROJECTION_MAX_BYTES,
    ) -> dict[str, Any] | None:
        """Load the Run-owned archive, or the live/legacy checkpoint fallback."""

        run_id = str(manifest["run_id"])
        if "task_tree_artifact" in manifest:
            artifact = manifest.get("task_tree_artifact")
            if not isinstance(artifact, str) or not artifact or artifact != artifact.strip():
                return self._incomplete_task_projection(
                    run_id=run_id,
                    owned_by_run=True,
                )
            return self._task_projection_from_source(
                run_dir,
                Path(artifact),
                missing_is_incomplete=True,
                expected_run_id=run_id,
                expected_task_id=task_id,
                owned_by_run=True,
                max_bytes=max_bytes,
            )

        observation = manifest.get("task_tree_observation")
        workers_require_evidence = isinstance(observation, dict) and observation.get("worker_agents_configured") is True
        if workers_require_evidence and observation.get("enabled") is False:
            return self._incomplete_task_projection(
                run_id=run_id,
                owned_by_run=True,
            )

        task_key = (application_id, task_id)
        if task_key not in checkpoint_cache:
            checkpoint_cache[task_key] = self._task_projection(
                runtime_root,
                application_id=application_id,
                task_id=task_id,
                max_bytes=max_bytes,
            )
        task = checkpoint_cache[task_key]
        if task is None:
            if workers_require_evidence:
                return self._incomplete_task_projection(
                    run_id=run_id,
                    owned_by_run=True,
                )
            return None
        if task.get("_task_source_incomplete"):
            # An unreadable task-scoped checkpoint cannot prove which attempt
            # it belongs to. Preserve that uncertainty for every candidate Run
            # instead of turning the source failure into "Never run".
            return task
        checkpoint_run_id = self._optional_string(task.get("run_id"))
        manifest_status = str(manifest.get("status") or "").strip().lower()
        if checkpoint_run_id not in {None, run_id}:
            return None
        # A terminal legacy Run may use a surviving checkpoint only when that
        # tree explicitly identifies the same Run. Active Runs can also use an
        # unscoped recovery checkpoint while it is still being written.
        if manifest_status != "running" and checkpoint_run_id != run_id:
            return None
        return task

    def _task_projection(
        self,
        runtime_root: Path,
        *,
        application_id: str,
        task_id: str,
        max_bytes: int = RUNTIME_TASK_PROJECTION_MAX_BYTES,
    ) -> dict[str, Any] | None:
        checkpoints_root = runtime_root / "checkpoints"
        return self._task_projection_from_source(
            checkpoints_root,
            Path(*application_id.split("/")) / task_id / "task_tree.json",
            max_bytes=max_bytes,
        )

    def _task_projection_from_source(
        self,
        root: Path,
        relative: Path,
        *,
        missing_is_incomplete: bool = False,
        expected_run_id: str | None = None,
        expected_task_id: str | None = None,
        owned_by_run: bool = False,
        max_bytes: int = RUNTIME_TASK_PROJECTION_MAX_BYTES,
    ) -> dict[str, Any] | None:
        tree, source_incomplete = self._read_json_object_bounded_secure_with_status(
            root,
            relative,
            max_bytes=max_bytes,
        )
        if tree is None:
            if not source_incomplete and not missing_is_incomplete:
                return None
            # Preserve the difference between "there is no checkpoint" and
            # "there is a checkpoint we could not safely/readably project".
            # Both contain no calls, but only the latter may be presented as an
            # incomplete status instead of "Never run".
            return self._incomplete_task_projection(
                run_id=expected_run_id,
                owned_by_run=owned_by_run,
            )
        tree_run_id = self._optional_string(tree.get("run_id"))
        tree_task_id = self._optional_string(tree.get("task_id"))
        if (
            expected_run_id is not None
            and tree_run_id != expected_run_id
            or expected_task_id is not None
            and tree_task_id not in {None, expected_task_id}
        ):
            return self._incomplete_task_projection(
                run_id=expected_run_id,
                owned_by_run=owned_by_run,
            )

        latest_workers: dict[
            tuple[str, tuple[str, ...]],
            tuple[
                tuple[int, str, int, str, str],
                int,
                dict[str, Any],
            ],
        ] = {}
        all_workers: list[
            tuple[
                tuple[int, str, int, str, str],
                int,
                dict[str, Any],
            ]
        ] = []
        tree_workers = tree.get("workers")
        if isinstance(tree_workers, dict):
            for agent_name, raw_calls in tree_workers.items():
                calls = raw_calls if isinstance(raw_calls, list) else [raw_calls]
                for raw_call in calls:
                    if not isinstance(raw_call, dict):
                        continue
                    candidate = {**raw_call, "agent_name": str(agent_name)}
                    run_scope = tuple(
                        sorted(
                            {
                                run_id
                                for run_id in (
                                    self._optional_string(candidate.get("attempt_run_id")),
                                    self._optional_string(candidate.get("cached_claim_run_id")),
                                )
                                if run_id is not None
                            }
                        )
                    )
                    key = (str(agent_name), run_scope)
                    rank = self._worker_projection_rank(candidate)
                    indexed_candidate = (rank, len(all_workers), candidate)
                    all_workers.append(indexed_candidate)
                    current = latest_workers.get(key)
                    if current is None or (rank, indexed_candidate[1]) > (
                        current[0],
                        current[1],
                    ):
                        latest_workers[key] = indexed_candidate
        ordered_representatives = sorted(
            latest_workers.values(),
            key=lambda selected_worker: (
                selected_worker[0],
                str(selected_worker[2].get("agent_name") or ""),
                selected_worker[1],
            ),
            reverse=True,
        )
        selected_representatives = ordered_representatives[:RUN_DETAIL_WORKER_MAX_COUNT]
        selected_indexes = {indexed_worker[1] for indexed_worker in selected_representatives}
        remaining_workers = sorted(
            (indexed_worker for indexed_worker in all_workers if indexed_worker[1] not in selected_indexes),
            key=lambda indexed_worker: (
                indexed_worker[0],
                str(indexed_worker[2].get("agent_name") or ""),
                indexed_worker[1],
            ),
            reverse=True,
        )
        detail_workers = [*selected_representatives]
        detail_workers.extend(remaining_workers[: max(0, RUN_DETAIL_WORKER_MAX_COUNT - len(detail_workers))])
        return {
            **tree,
            # ``run.detail`` gets one call per Worker entity first, then the
            # newest remaining calls until its response budget is full.
            "workers": [worker for _, _, worker in detail_workers],
            # Runtime summaries need only one representative per Worker/run
            # scope; detail-call truncation must not make their status unknown.
            "_worker_representatives": [worker for _, _, worker in selected_representatives],
            "_workers_truncated": len(all_workers) > len(detail_workers),
            "_worker_entities_truncated": len(ordered_representatives) > RUN_DETAIL_WORKER_MAX_COUNT,
            "_task_source_incomplete": False,
            "_task_owned_by_run": owned_by_run,
        }

    @staticmethod
    def _incomplete_task_projection(
        *,
        run_id: str | None,
        owned_by_run: bool,
    ) -> dict[str, Any]:
        projection: dict[str, Any] = {
            "workers": [],
            "_worker_representatives": [],
            "_workers_truncated": True,
            "_worker_entities_truncated": True,
            "_task_source_incomplete": True,
            "_task_owned_by_run": owned_by_run,
        }
        if run_id is not None:
            projection["run_id"] = run_id
        return projection

    def _runtime_root(self) -> Path:
        config = self._read_yaml(self.project_root / "config" / "system.yaml")
        runtime = config.get("runtime", {}) if isinstance(config, dict) else {}
        if not isinstance(runtime, dict):
            runtime = {}
        configured = Path(
            os.environ.get("AGENTLOOM_RUNTIME_ROOT", "").strip() or str(runtime.get("root_dir") or ".agentloom")
        ).expanduser()
        if not configured.is_absolute():
            configured = self.project_root / configured
        return configured.absolute()

    def _canonical_run_dir(
        self,
        runs_root: Path,
        manifest_path: Path,
        manifest: dict[str, Any],
    ) -> Path | None:
        application_id = manifest.get("application_id")
        task_id = manifest.get("task_id")
        run_id = manifest.get("run_id")
        if not all(isinstance(item, str) for item in (application_id, task_id, run_id)):
            return None
        try:
            canonical_application = safe_application_id(application_id)
            canonical_task = validate_runtime_id(task_id, field="task_id")
            canonical_run = validate_runtime_id(run_id, field="run_id")
        except ValueError:
            return None
        if (canonical_application, canonical_task, canonical_run) != (
            application_id,
            task_id,
            run_id,
        ):
            return None
        expected = runs_root / Path(*application_id.split("/")) / run_id
        if manifest_path != expected / "manifest.json" or expected.is_symlink():
            return None
        return expected

    def _match_system(
        self,
        manifest: dict[str, Any],
        *,
        paths_by_id: dict[str, Path],
        systems_by_application: dict[str, list[str]],
    ) -> str | None:
        yaml_path = manifest.get("yaml_path")
        if isinstance(yaml_path, str) and yaml_path:
            candidate = Path(yaml_path).expanduser()
            if not candidate.is_absolute():
                candidate = self.project_root / candidate
            # Run manifests are data. Match their recorded absolute identity
            # lexically; live refresh must never follow or inspect the current
            # Agent YAML after bootstrap.
            recorded_path = Path(os.path.abspath(candidate))
            for system_id, system_path in paths_by_id.items():
                if recorded_path == system_path:
                    return system_id
        application_id = str(manifest.get("application_id") or "")
        candidates = systems_by_application.get(application_id, [])
        return candidates[0] if len(candidates) == 1 else None

    def _system_identity_paths(
        self,
        systems: list[dict[str, Any]],
    ) -> dict[str, Path]:
        """Build stable lexical identities without touching Agent files."""

        return {
            str(system["id"]): Path(
                os.path.abspath(self.project_root / str(system["path"])),
            )
            for system in systems
        }

    @staticmethod
    def _run_status(
        manifest: dict[str, Any],
        *,
        task: dict[str, Any] | None,
        run_dir: Path,
    ) -> str:
        status = str(manifest.get("status") or "").strip().lower()
        terminal_status = _TERMINAL_RUN_STATUS_ALIASES.get(status)
        if terminal_status is not None:
            return terminal_status
        if status == "running":
            lease = RuntimeRunLease(run_dir)
            try:
                if lease.is_held():
                    return "running"
            except (OSError, RuntimeError):
                pass

        if status == "running" and task is not None:
            task_status = str(task.get("status") or "").strip().lower()
            terminal_status = _TERMINAL_RUN_STATUS_ALIASES.get(task_status)
            if terminal_status is not None:
                return terminal_status
        if status == "running":
            return "crashed"
        return "unknown"

    def _task_events(
        self,
        record: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        summary = record["summary"]
        run_dir = record["run_dir"]
        sources: list[tuple[Path, Path, bool, bool]] = []
        artifact = record["manifest"].get("task_events_artifact")
        if isinstance(artifact, str) and artifact:
            sources.append(
                (
                    run_dir,
                    Path(artifact),
                    True,
                    record["manifest"].get("task_events_complete") is False,
                )
            )
        default_artifact = Path("audit/task_events.jsonl")
        if all(relative != default_artifact for _, relative, _, _ in sources):
            sources.append((run_dir, default_artifact, True, False))
        sources.append(
            (
                record["runtime_root"],
                Path("checkpoints")
                / Path(*summary["application_id"].split("/"))
                / summary["task_id"]
                / "task_events.jsonl",
                False,
                False,
            )
        )

        selected: tuple[bytes, int, bool, bool] | None = None
        for root, relative, owned_by_run, declared_incomplete in sources:
            window = self._read_event_window(root, relative)
            if window is not None:
                raw, start = window
                selected = raw, start, owned_by_run, declared_incomplete
                break
        if selected is None:
            return [], [], self._event_limits()
        raw, start, owned_by_run, declared_incomplete = selected

        source_truncated = start > 0
        if start > 0:
            boundary = raw.find(b"\n")
            if boundary < 0:
                return (
                    [],
                    [],
                    self._event_limits(
                        truncated=True,
                        source_incomplete=True,
                    ),
                )
            raw = raw[boundary + 1 :]

        parsed: list[dict[str, Any]] = []
        malformed = False
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (UnicodeError, json.JSONDecodeError):
                malformed = True
                continue
            if isinstance(event, dict):
                parsed.append(event)
            else:
                malformed = True

        semantic_events = self._project_event_window(
            record,
            parsed,
            prefix_truncated=source_truncated,
            owned_by_run=owned_by_run,
        )
        preview, preview_truncated, returned_bytes = self._limit_event_preview(semantic_events)
        limits = self._event_limits(
            truncated=source_truncated or malformed or declared_incomplete or preview_truncated,
            source_incomplete=source_truncated or malformed or declared_incomplete,
            returned_count=len(preview),
            returned_bytes=returned_bytes,
        )
        return preview, semantic_events, limits

    @staticmethod
    def _read_event_window(root: Path, relative: Path) -> tuple[bytes, int] | None:
        try:
            with SecureDirectory(root, create=False) as storage:
                with storage.open_binary_reader(relative) as stream:
                    stream.seek(0, os.SEEK_END)
                    size = stream.tell()
                    start = max(0, size - RUN_DETAIL_EVENT_SCAN_MAX_BYTES)
                    stream.seek(start)
                    return stream.read(RUN_DETAIL_EVENT_SCAN_MAX_BYTES), start
        except (OSError, RuntimeError, ValueError):
            return None

    def _project_event_window(
        self,
        record: dict[str, Any],
        events: list[dict[str, Any]],
        *,
        prefix_truncated: bool,
        owned_by_run: bool,
    ) -> list[dict[str, Any]]:
        target_run_id = record["summary"]["run_id"]
        marker_run_ids = [
            self._optional_string(event.get("run_id"))
            for event in events
            if event.get("type") in {"run_started", "run_resumed"}
        ]
        if target_run_id in marker_run_ids or not prefix_truncated:
            return self._events_for_run(events, target_run_id)

        # A marker for another attempt proves that unscoped tail events do not
        # belong to the requested historical Run.
        if marker_run_ids:
            return []

        current_task = record.get("task")
        task_is_current_run = isinstance(current_task, dict) and current_task.get("run_id") == target_run_id
        if owned_by_run or task_is_current_run:
            return [event for event in events if self._event_run_id(event) in {None, target_run_id}]

        # For an old cumulative checkpoint with no target marker in the bounded
        # window, only explicitly scoped events are safe to show.
        return [event for event in events if self._event_run_id(event) == target_run_id]

    @staticmethod
    def _event_run_id(event: dict[str, Any]) -> str | None:
        explicit = TuiBridge._optional_string(event.get("run_id"))
        tree = event.get("tree")
        if explicit is None and isinstance(tree, dict):
            explicit = TuiBridge._optional_string(tree.get("run_id"))
        return explicit

    @staticmethod
    def _limit_event_preview(
        events: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool, int]:
        selected: list[dict[str, Any]] = []
        returned_bytes = 0
        truncated = False
        for event in reversed(events):
            encoded = json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8", errors="replace")
            if len(encoded) > RUN_DETAIL_EVENT_MAX_BYTES:
                truncated = True
                continue
            if len(selected) >= RUN_DETAIL_EVENT_MAX_COUNT:
                truncated = True
                break
            if returned_bytes + len(encoded) > RUN_DETAIL_EVENT_MAX_BYTES:
                truncated = True
                break
            selected.append(event)
            returned_bytes += len(encoded)
        selected.reverse()
        return selected, truncated or len(selected) != len(events), returned_bytes

    @staticmethod
    def _event_limits(
        *,
        truncated: bool = False,
        source_incomplete: bool = False,
        returned_count: int = 0,
        returned_bytes: int = 0,
    ) -> dict[str, Any]:
        return {
            "truncated": truncated,
            "source_incomplete": source_incomplete,
            "returned_count": returned_count,
            "returned_bytes": returned_bytes,
            "max_count": RUN_DETAIL_EVENT_MAX_COUNT,
            "max_bytes": RUN_DETAIL_EVENT_MAX_BYTES,
            "max_scan_bytes": RUN_DETAIL_EVENT_SCAN_MAX_BYTES,
        }

    @staticmethod
    def _events_for_run(events: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        preamble: list[dict[str, Any]] = []
        current_run_id: str | None = None
        saw_run_marker = False
        selected_started = False
        for event in events:
            event_type = event.get("type")
            if event_type in {"run_started", "run_resumed"}:
                marker_run_id = TuiBridge._optional_string(event.get("run_id"))
                if selected_started and marker_run_id != run_id:
                    break
                if marker_run_id == run_id:
                    if not saw_run_marker and event_type == "run_started":
                        projected.extend(preamble)
                    projected.append(event)
                    selected_started = True
                current_run_id = marker_run_id
                saw_run_marker = True
                continue

            explicit_run_id = TuiBridge._optional_string(event.get("run_id"))
            tree = event.get("tree")
            if explicit_run_id is None and isinstance(tree, dict):
                explicit_run_id = TuiBridge._optional_string(tree.get("run_id"))
            if selected_started:
                if current_run_id == run_id and explicit_run_id in {None, run_id}:
                    projected.append(event)
                continue
            if not saw_run_marker:
                if explicit_run_id == run_id:
                    projected.append(event)
                elif explicit_run_id is None:
                    preamble.append(event)
        return projected

    def _workers(
        self,
        record: dict[str, Any],
        *,
        events: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        task = record.get("task") or {}
        workers_by_key: dict[tuple[str, int], dict[str, Any]] = {}
        truncated = bool(task.get("_workers_truncated"))
        target_run_id = record["summary"]["run_id"]
        raw_workers = task.get("workers", [])
        if not isinstance(raw_workers, list):
            raw_workers = []
        for position, worker in enumerate(raw_workers):
            if position >= RUN_DETAIL_WORKER_MAX_COUNT:
                truncated = True
                break
            if not isinstance(worker, dict):
                continue
            worker_run_ids = {
                value
                for value in (
                    self._optional_string(worker.get("attempt_run_id")),
                    self._optional_string(worker.get("cached_claim_run_id")),
                )
                if value is not None
            }
            if worker_run_ids and target_run_id not in worker_run_ids:
                continue
            if events and not worker_run_ids and not task.get("_task_owned_by_run"):
                continue
            projected = self._checkpoint_worker_summary(worker)
            key = (str(projected["agent_name"]), int(projected["call_index"]))
            workers_by_key[key] = projected
        for event in events:
            event_type = event.get("type")
            if event_type in {"run_started", "run_resumed"}:
                continue
            if self._event_run_id(event) not in {None, target_run_id} or event_type not in {
                "worker_call_started",
                "worker_call_resume_claimed",
                "worker_call_cached_result_claimed",
                "worker_call_finished",
            }:
                continue
            agent_name = str(event.get("agent_name") or event.get("worker_name") or "")
            try:
                call_index = int(event.get("call_index") or 0)
            except (TypeError, ValueError):
                call_index = 0
            key = (agent_name, call_index)
            worker = workers_by_key.get(key)
            if worker is None:
                if len(workers_by_key) >= RUN_DETAIL_WORKER_MAX_COUNT:
                    truncated = True
                    continue
                worker = {
                    "agent_name": agent_name,
                    "call_index": call_index,
                    "status": "unknown",
                    "step": None,
                    "started_at": None,
                    "ended_at": None,
                    "error": None,
                }
                workers_by_key[key] = worker
            if event_type in {"worker_call_started", "worker_call_resume_claimed"}:
                worker["status"] = "running"
                worker["started_at"] = self._optional_string(
                    event.get("started_at") or event.get("claimed_at") or event.get("timestamp")
                )
            elif event_type == "worker_call_cached_result_claimed":
                worker["status"] = "cached"
                worker["started_at"] = None
                worker["ended_at"] = self._optional_string(event.get("claimed_at") or event.get("timestamp"))
                worker["error"] = None
            else:
                worker["status"] = str(event.get("status") or worker["status"])
                worker["ended_at"] = self._optional_string(event.get("finished_at") or event.get("timestamp"))
                worker["error"] = self._optional_string(event.get("error"))
        return [workers_by_key[key] for key in sorted(workers_by_key)], truncated

    def _run_result(
        self,
        record: dict[str, Any],
        *,
        events: list[dict[str, Any]] | None = None,
        events_incomplete: bool = False,
    ) -> tuple[bool, str | None, dict[str, Any]]:
        manifest = record["manifest"]
        artifact = manifest.get("result_artifact")
        if isinstance(artifact, str) and artifact:
            try:
                with SecureDirectory(record["run_dir"], create=False) as storage:
                    payload, truncated = storage.read_bytes_up_to(
                        Path(artifact),
                        RUN_DETAIL_RESULT_MAX_BYTES,
                    )
            except (OSError, RuntimeError, ValueError):
                pass
            else:
                text = payload.decode("utf-8", errors="ignore")
                return (
                    True,
                    text,
                    self._result_limits(
                        truncated=truncated,
                        returned_bytes=len(text.encode("utf-8")),
                    ),
                )
        available = False
        result: Any = None
        target_run_id = record["summary"]["run_id"]
        if events is None:
            _, events, event_limits = self._task_events(record)
            events_incomplete = event_limits["source_incomplete"]
        for event in events:
            if event.get("type") in {"run_started", "run_resumed"}:
                if self._optional_string(event.get("run_id")) == target_run_id:
                    available, result = False, None
                continue
            if event.get("type") == "task_tree_replaced":
                tree = event.get("tree")
                if isinstance(tree, dict) and tree.get("run_id") == target_run_id:
                    tree_status = str(tree.get("status") or "").strip().lower()
                    available = tree_status in {"completed", "success", "succeeded"} and "result" in tree
                    result = tree.get("result") if available else None
            elif self._event_run_id(event) in {None, target_run_id} and event.get("type") == "task_status_changed":
                task_status = str(event.get("status") or "").strip().lower()
                available = task_status in {"completed", "success", "succeeded"} and "result" in event
                result = event.get("result") if available else None
        if not available:
            return (
                False,
                None,
                self._result_limits(
                    truncated=events_incomplete,
                    source_incomplete=events_incomplete,
                ),
            )
        if result is None or isinstance(result, str):
            text = result
        else:
            text = json.dumps(result, ensure_ascii=False, default=str)
        if text is None:
            return True, None, self._result_limits(source_incomplete=events_incomplete)
        preview, truncated = self._truncate_text(text, RUN_DETAIL_RESULT_MAX_BYTES)
        return (
            True,
            preview,
            self._result_limits(
                truncated=truncated,
                source_incomplete=events_incomplete,
                returned_bytes=len(preview.encode("utf-8")),
            ),
        )

    @staticmethod
    def _result_limits(
        *,
        truncated: bool = False,
        source_incomplete: bool = False,
        returned_bytes: int = 0,
    ) -> dict[str, Any]:
        return {
            "truncated": truncated,
            "source_incomplete": source_incomplete,
            "returned_bytes": returned_bytes,
            "max_bytes": RUN_DETAIL_RESULT_MAX_BYTES,
        }

    def _log_files(self, root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        files: list[dict[str, Any]] = []
        returned_bytes = 0
        truncated = False
        try:
            with SecureDirectory(root, create=False) as storage:
                paths, truncated = storage.bounded_regular_files(
                    max_files=RUN_DETAIL_LOG_MAX_FILES,
                    max_entries=RUN_DETAIL_FILE_SCAN_MAX_ENTRIES,
                )
                for relative in paths:
                    remaining = RUN_DETAIL_LOG_MAX_TOTAL_BYTES - returned_bytes
                    if remaining <= 0:
                        truncated = True
                        break
                    try:
                        with storage.open_binary_reader(relative) as stream:
                            size = os.fstat(stream.fileno()).st_size
                            read_size = min(
                                size,
                                remaining,
                                RUN_DETAIL_LOG_MAX_BYTES_PER_FILE,
                            )
                            stream.seek(max(0, size - read_size))
                            tail = stream.read(read_size).decode("utf-8", errors="ignore")
                    except (OSError, RuntimeError, ValueError):
                        continue
                    tail_bytes = len(tail.encode("utf-8"))
                    returned_bytes += tail_bytes
                    tail_truncated = size > read_size
                    truncated = truncated or tail_truncated
                    files.append(
                        {
                            "path": self._runtime_file_display_path(root / relative, root=root),
                            "size": size,
                            "tail": tail,
                            "tail_truncated": tail_truncated,
                        }
                    )
        except (OSError, RuntimeError, ValueError):
            return [], self._log_limits()
        return files, self._log_limits(
            truncated=truncated,
            returned_count=len(files),
            returned_bytes=returned_bytes,
        )

    @staticmethod
    def _log_limits(
        *,
        truncated: bool = False,
        returned_count: int = 0,
        returned_bytes: int = 0,
    ) -> dict[str, Any]:
        return {
            "truncated": truncated,
            "returned_count": returned_count,
            "returned_bytes": returned_bytes,
            "max_count": RUN_DETAIL_LOG_MAX_FILES,
            "max_bytes": RUN_DETAIL_LOG_MAX_TOTAL_BYTES,
            "max_bytes_per_file": RUN_DETAIL_LOG_MAX_BYTES_PER_FILE,
            "max_scanned_entries": RUN_DETAIL_FILE_SCAN_MAX_ENTRIES,
        }

    def _artifact_files(self, root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        files: list[dict[str, Any]] = []
        truncated = False
        try:
            with SecureDirectory(root, create=False) as storage:
                paths, truncated = storage.bounded_regular_files(
                    max_files=RUN_DETAIL_ARTIFACT_MAX_FILES,
                    max_entries=RUN_DETAIL_FILE_SCAN_MAX_ENTRIES,
                )
                for relative in paths:
                    try:
                        with storage.open_binary_reader(relative) as stream:
                            size = os.fstat(stream.fileno()).st_size
                    except (OSError, RuntimeError, ValueError):
                        continue
                    files.append(
                        {
                            "path": self._runtime_file_display_path(root / relative, root=root),
                            "size": size,
                        }
                    )
        except (OSError, RuntimeError, ValueError):
            return [], self._artifact_limits()
        return files, self._artifact_limits(
            truncated=truncated,
            returned_count=len(files),
        )

    @staticmethod
    def _artifact_limits(
        *,
        truncated: bool = False,
        returned_count: int = 0,
    ) -> dict[str, Any]:
        return {
            "truncated": truncated,
            "returned_count": returned_count,
            "max_count": RUN_DETAIL_ARTIFACT_MAX_FILES,
            "max_scanned_entries": RUN_DETAIL_FILE_SCAN_MAX_ENTRIES,
        }

    def _runtime_file_display_path(self, path: Path, *, root: Path) -> str:
        try:
            return path.relative_to(self.project_root).as_posix()
        except ValueError:
            return path.relative_to(root.parent).as_posix()

    @staticmethod
    def _truncate_text(value: str, max_bytes: int) -> tuple[str, bool]:
        encoded = value.encode("utf-8", errors="replace")
        if len(encoded) <= max_bytes:
            return value, False
        return encoded[:max_bytes].decode("utf-8", errors="ignore"), True

    @staticmethod
    def _has_symlink_component(path: Path, root: Path) -> bool:
        current = path
        while current != root:
            if current.is_symlink() or current.parent == current:
                return True
            current = current.parent
        return root.is_symlink()

    @staticmethod
    def _read_json_object_bounded_secure(
        root: Path,
        relative: Path,
        *,
        max_bytes: int,
    ) -> dict[str, Any] | None:
        raw, _ = TuiBridge._read_json_object_bounded_secure_with_status(
            root,
            relative,
            max_bytes=max_bytes,
        )
        return raw

    @staticmethod
    def _read_json_object_bounded_secure_with_status(
        root: Path,
        relative: Path,
        *,
        max_bytes: int,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Read one bounded object and report an existing unreadable source.

        The boolean is false only for a successful object read or a genuinely
        missing path. Oversized, malformed, unsafe, and otherwise unreadable
        existing sources are incomplete observations rather than absence.
        """

        try:
            with SecureDirectory(root, create=False) as storage:
                payload, truncated = storage.read_bytes_up_to(relative, max_bytes)
        except FileNotFoundError:
            return None, False
        except (OSError, RuntimeError, ValueError):
            return None, True
        if truncated:
            return None, True
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return None, True
        if not isinstance(raw, dict):
            return None, True
        return raw, False

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    def _model_catalog(self) -> dict[str, Any]:
        config = self._read_yaml(self.project_root / "config" / "llm.yaml")
        model_config = config.get("model", {}) if isinstance(config, dict) else {}
        if not isinstance(model_config, dict):
            model_config = {}
        default = str(model_config.get("default_model_type") or "")
        items: list[dict[str, Any]] = []
        for model_type, raw_settings in model_config.items():
            if model_type in {"default_model_type", "common"}:
                continue
            if not isinstance(raw_settings, dict):
                continue
            items.append(
                {
                    "type": str(model_type),
                    "description": str(raw_settings.get("description") or ""),
                    "default": model_type == default,
                    "configured": bool(raw_settings.get("model")),
                }
            )
        return {
            "default": default,
            "configured": bool(items and any(item["default"] and item["configured"] for item in items)),
            "items": items,
        }

    def _scan_systems(
        self,
        *,
        definition_cache: AgentDefinitionCache | None = None,
    ) -> list[dict[str, Any]]:
        from src.tui_bridge.definition import model_types, validate_agent_definition

        applications_root = self.project_root / "applications"
        if applications_root.is_symlink() or not applications_root.is_dir():
            return []
        paths = sorted(
            path
            for pattern in ("*.yaml", "*.yml")
            for path in applications_root.rglob(pattern)
            if "workflows" in path.parts
            and "worker_agents" not in path.parts
            and not path.is_symlink()
            and not self._has_symlink_component(path, applications_root)
        )
        systems: list[dict[str, Any]] = []
        catalog = model_types(self.project_root)
        for path in paths:
            relative = path.relative_to(self.project_root).as_posix()
            definition, read_errors = self._read_definition(
                path,
                definition_cache=definition_cache,
            )
            errors = read_errors + validate_agent_definition(
                self.project_root,
                relative,
                definition,
                catalog=catalog,
                definition_cache=definition_cache,
            )
            systems.append(
                {
                    "id": relative,
                    "path": relative,
                    "application_id": self._application_id(path),
                    "name": str(definition.get("name") or path.stem),
                    "description": str(definition.get("description") or ""),
                    "state": "never_run",
                    "validation": {"valid": not errors, "errors": errors},
                    "latest_run": None,
                }
            )
        return systems

    def _application_id(self, workflow_path: Path) -> str:
        relative = workflow_path.relative_to(self.project_root / "applications")
        try:
            workflow_index = relative.parts.index("workflows")
        except ValueError:
            return relative.parent.as_posix()
        return "/".join(relative.parts[:workflow_index])

    def _worker_files(
        self,
        supervisor_path: Path,
        definition: dict[str, Any],
    ) -> list[Path]:
        raw_workers = definition.get("worker_agents", [])
        if not isinstance(raw_workers, list):
            return []
        worker_folder = supervisor_path.parent / "worker_agents"
        workers: list[Path] = []
        for item in raw_workers:
            raw_path = item.get("path") if isinstance(item, dict) else None
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            configured = Path(raw_path.strip())
            if configured.is_absolute():
                candidate = configured.resolve()
            elif "/" in raw_path or "\\" in raw_path:
                candidate = (self.project_root / configured).resolve()
            else:
                candidate = (worker_folder / configured).resolve()
            try:
                candidate.relative_to(self.project_root)
            except ValueError:
                continue
            if candidate.is_file() and candidate.suffix.lower() in {".yaml", ".yml", ".md"}:
                workers.append(candidate)
        return workers

    def _file_summary(self, path: Path, kind: str) -> dict[str, Any]:
        return {
            "path": path.relative_to(self.project_root).as_posix(),
            "kind": kind,
            "size": path.stat().st_size,
        }

    @staticmethod
    def _workflow_text(raw_workflow: Any) -> str:
        if isinstance(raw_workflow, str):
            return raw_workflow
        if isinstance(raw_workflow, list):
            return "\n\n".join(str(item) for item in raw_workflow)
        return ""

    @staticmethod
    def _read_definition(
        path: Path,
        *,
        definition_cache: AgentDefinitionCache | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        from src.tui_bridge.definition import read_agent_definition

        result = read_agent_definition(path, cache=definition_cache)
        if result.error is not None or result.definition is None:
            return {}, [f"invalid YAML: {result.error or 'agent definition must be a YAML object'}"]
        return result.definition, []

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError):
            return {}
        return raw if isinstance(raw, dict) else {}
