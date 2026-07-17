"""Project and runtime projections exposed through the TUI bridge."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from src.lib.runtime.context import (
    RuntimeRunLease,
    safe_application_id,
    validate_runtime_id,
)
from src.lib.runtime.storage import SecureDirectory
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory
from src.tui_bridge.builder import (
    BuilderService,
    DraftConflictError,
    validate_agent_definition,
)

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


class BridgeError(RuntimeError):
    """A stable, user-safe RPC error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TuiBridge:
    """Project projection and bounded Builder operations for one project."""

    def __init__(self, project_root: Path, *, builder_service: Any | None = None) -> None:
        self.project_root = project_root.expanduser().resolve()
        self._builder = builder_service or BuilderService(self.project_root)

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "bootstrap":
            return self.bootstrap()
        if method == "system.detail":
            system_id = params.get("system_id")
            if not isinstance(system_id, str) or not system_id:
                raise BridgeError("invalid_params", "system_id must be a non-empty string")
            return self.system_detail(system_id)
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
        if method == "builder.send":
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
                return self._builder.send(
                    session_id=session_id,
                    message=message,
                    model_type=model_type,
                )
            except ValueError as error:
                raise BridgeError("builder_failed", str(error)) from error
            except Exception as error:
                raise BridgeError(
                    "builder_failed",
                    "Builder model call failed; retry or select another configured model.",
                ) from error
        if method == "builder.draft":
            session_id = params.get("session_id")
            if not isinstance(session_id, str) or not session_id.strip():
                raise BridgeError("invalid_params", "session_id must be a non-empty string")
            return self._builder.get_draft(session_id)
        if method == "draft.apply":
            session_id = params.get("session_id")
            expected_revision = params.get("expected_revision")
            if not isinstance(session_id, str) or not session_id.strip():
                raise BridgeError("invalid_params", "session_id must be a non-empty string")
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise BridgeError("invalid_params", "expected_revision must be an integer")
            try:
                return self._builder.apply_draft(
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

    def bootstrap(self) -> dict[str, Any]:
        systems, runs, _ = self._snapshot()
        return {
            "project": {
                "root": str(self.project_root),
                "name": self.project_root.name,
            },
            "models": self._model_catalog(),
            "systems": systems,
            "runs": runs,
        }

    def system_detail(self, system_id: str) -> dict[str, Any]:
        systems, _, run_records = self._snapshot()
        summaries = {summary["id"]: summary for summary in systems}
        summary = summaries.get(system_id)
        if summary is None:
            raise BridgeError("not_found", f"system not found: {system_id}")

        supervisor_path = self.project_root / system_id
        definition, _ = self._read_definition(supervisor_path)
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
        task = self._task_projection(
            runtime_root,
            application_id=application_id,
            task_id=task_id,
        )
        if task is not None and str(task.get("run_id") or "") not in {"", run_id}:
            task = None

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
            return self._optional_string(task.get("error"))
        return None

    def _snapshot(
        self,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[tuple[str, str], dict[str, Any]],
    ]:
        systems = self._scan_systems()
        runs, records = self._scan_runs(systems)
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
        return systems, runs, records

    def _scan_runs(
        self,
        systems: list[dict[str, Any]],
    ) -> tuple[
        list[dict[str, Any]],
        dict[tuple[str, str], dict[str, Any]],
    ]:
        runtime_root = self._runtime_root()
        runs_root = runtime_root / "runs"
        if runtime_root.is_symlink() or not runs_root.is_dir():
            return [], {}
        task_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
        paths_by_id = {system["id"]: (self.project_root / system["path"]).resolve() for system in systems}
        systems_by_application: dict[str, list[str]] = {}
        for system in systems:
            systems_by_application.setdefault(system["application_id"], []).append(system["id"])

        records: dict[tuple[str, str], dict[str, Any]] = {}
        for manifest_path in sorted(runs_root.rglob("manifest.json")):
            if manifest_path.is_symlink() or self._has_symlink_component(manifest_path, runs_root):
                continue
            try:
                manifest_relative = manifest_path.relative_to(runs_root)
            except ValueError:
                continue
            manifest = self._read_json_object_bounded_secure(
                runs_root,
                manifest_relative,
                max_bytes=RUN_MANIFEST_MAX_BYTES,
            )
            if manifest is None:
                continue
            run_dir = self._canonical_run_dir(runs_root, manifest_path, manifest)
            if run_dir is None:
                continue
            application_id = str(manifest["application_id"])
            task_id = str(manifest["task_id"])
            run_id = str(manifest["run_id"])
            task_key = (application_id, task_id)
            if task_key not in task_cache:
                task_cache[task_key] = self._task_projection(
                    runtime_root,
                    application_id=application_id,
                    task_id=task_id,
                )
            task = task_cache[task_key]
            if task is not None and str(task.get("run_id") or "") not in {"", run_id}:
                task = None
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
            records[(application_id, run_id)] = {
                "summary": summary,
                "manifest": manifest,
                "run_dir": run_dir,
                "task": task,
                "runtime_root": runtime_root,
            }
        ordered = sorted(
            (record["summary"] for record in records.values()),
            key=lambda summary: (summary["started_at"] or "", summary["run_id"]),
            reverse=True,
        )
        return ordered, records

    def _task_projection(
        self,
        runtime_root: Path,
        *,
        application_id: str,
        task_id: str,
    ) -> dict[str, Any] | None:
        checkpoints_root = runtime_root / "checkpoints"
        tree = self._read_json_object_bounded_secure(
            checkpoints_root,
            Path(*application_id.split("/")) / task_id / "task_tree.json",
            max_bytes=RUNTIME_TASK_PROJECTION_MAX_BYTES,
        )
        if tree is None:
            return None

        flattened_workers: list[dict[str, Any]] = []
        workers_truncated = False
        tree_workers = tree.get("workers")
        if isinstance(tree_workers, dict):
            for agent_name, raw_calls in tree_workers.items():
                calls = raw_calls if isinstance(raw_calls, list) else [raw_calls]
                for raw_call in calls:
                    if len(flattened_workers) >= RUN_DETAIL_WORKER_MAX_COUNT:
                        workers_truncated = True
                        break
                    if not isinstance(raw_call, dict):
                        continue
                    flattened_workers.append(
                        {
                            **raw_call,
                            "agent_name": str(agent_name),
                        }
                    )
                if len(flattened_workers) >= RUN_DETAIL_WORKER_MAX_COUNT:
                    workers_truncated = True
                    break
        return {
            **tree,
            "workers": flattened_workers,
            "_workers_truncated": workers_truncated,
        }

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
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate.absolute()
            for system_id, system_path in paths_by_id.items():
                if resolved == system_path:
                    return system_id
        application_id = str(manifest.get("application_id") or "")
        candidates = systems_by_application.get(application_id, [])
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _run_status(
        manifest: dict[str, Any],
        *,
        task: dict[str, Any] | None,
        run_dir: Path,
    ) -> str:
        status = str(manifest.get("status") or "").strip().lower()
        if status in {"completed", "success", "succeeded"}:
            return "completed"
        if status in {"failed", "error", "interrupted", "cancelled"}:
            return "failed"
        if status == "crashed":
            return "crashed"
        if status == "running":
            lease = RuntimeRunLease(run_dir)
            try:
                if lease.is_held():
                    return "running"
            except (OSError, RuntimeError):
                pass

        if status == "running" and task is not None:
            task_status = str(task.get("status") or "").strip().lower()
            if task_status in {"completed", "success", "succeeded"}:
                return "completed"
            if task_status in {"failed", "error", "interrupted", "cancelled"}:
                return "failed"
            if task_status == "crashed":
                return "crashed"
        if status == "running":
            return "crashed"
        return "failed"

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
            if events and not worker_run_ids:
                continue
            agent_name = str(worker.get("agent_name") or "")
            call_index = int(worker.get("call_index") or 0)
            workers_by_key[(agent_name, call_index)] = {
                "agent_name": agent_name,
                "call_index": call_index,
                "status": str(worker.get("status") or "unknown"),
                "step": worker.get("step") if isinstance(worker.get("step"), int) else None,
                "started_at": self._optional_string(worker.get("started_at")),
                "ended_at": self._optional_string(worker.get("finished_at")),
                "error": self._optional_string(worker.get("error")),
            }
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
        try:
            with SecureDirectory(root, create=False) as storage:
                payload, truncated = storage.read_bytes_up_to(relative, max_bytes)
        except (OSError, RuntimeError, ValueError):
            return None
        if truncated:
            return None
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

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

    def _scan_systems(self) -> list[dict[str, Any]]:
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
        for path in paths:
            relative = path.relative_to(self.project_root).as_posix()
            definition, read_errors = self._read_definition(path)
            errors = read_errors + validate_agent_definition(
                self.project_root,
                relative,
                definition,
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
    def _read_definition(path: Path) -> tuple[dict[str, Any], list[str]]:
        try:
            raw = YamlAgentFactory._load_config_from_file(path)
        except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError) as error:
            return {}, [f"invalid YAML: {error}"]
        if not isinstance(raw, dict):
            return {}, ["agent definition must be a YAML object"]
        return raw, []

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError):
            return {}
        return raw if isinstance(raw, dict) else {}
