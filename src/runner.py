"""
One-liner Application Runner.

Provides ``run_app`` – a single-call entry point that boots a
:class:`YamlConfiguredSupervisorAgent` from a YAML path and executes it.

Usage (Python API)::

    from src.runner import run_app

    result = run_app("applications/<app>/workflows/<agent>.yaml")

Usage (CLI)::

    loom run applications/<app>/workflows/<agent>.yaml
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.application_run import (
    ApplicationRunError,
    ApplicationRunInterrupted,
    ApplicationRunResult,
    RunEvent,
    RunEventSink,
    RunInfo,
    RunLifecycleEvent,
    RunPhase,
    RunRejectedEvent,
    RunRejection,
)
from src.lib.checkpoint import CheckpointManager
from src.lib.checkpoint.file_history import FileHistoryManager
from src.lib.config import C, build_effective_agent_config
from src.lib.heartbeat import SupervisorHeartbeat
from src.lib.logging import (
    LoggingConfigBuilder,
    bind_logger_backend,
    get_logger,
    initialize_run_logger,
)
from src.lib.runtime import (
    bind_run_context,
    generate_runtime_id,
    resolve_application_id,
    resolve_runtime_home,
)
from src.lib.smolagents.agent.runtime_validation import (
    validate_required_yaml_fields,
    validate_runtime_agent_config,
)
from src.lib.smolagents.agent.yaml_agent_factory import (
    YamlAgentFactory,
    YamlConfiguredSupervisorAgent,
)

_RUN_ARTIFACT_COPY_CHUNK_BYTES = 1024 * 1024
_TASK_TREE_CLEANUP_MAX_BYTES = 1024 * 1024


def _run_info(runtime_context: Any, log_path: Path | None) -> RunInfo:
    """Build the immutable public view of an internal RuntimeContext."""

    return RunInfo(
        application_id=runtime_context.application_id,
        task_id=runtime_context.task_id,
        run_id=runtime_context.run_id,
        run_dir=runtime_context.run_dir.absolute(),
        manifest_path=runtime_context.manifest_path.absolute(),
        log_path=log_path.absolute() if log_path is not None else None,
    )


def _configured_run_log_path(runtime_context: Any, logger_backend: Any) -> Path | None:
    """Return only the canonical file path configured on this run's backend."""

    console = getattr(logger_backend, "console", None)
    raw_path = getattr(console, "log_file_path", None)
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    configured_path = Path(raw_path).expanduser().absolute()
    canonical_path = runtime_context.log_path.absolute()
    return canonical_path if configured_path == canonical_path else None


def _emit_lifecycle_event(
    event_sink: RunEventSink | None,
    event: RunEvent,
) -> None:
    """Ignore ordinary observer failures while preserving process-control signals."""

    if event_sink is None:
        return
    try:
        event_sink(event)
    except Exception:
        return


def _emit_preflight_rejection(
    event_sink: RunEventSink | None,
    error: BaseException,
) -> None:
    _emit_lifecycle_event(
        event_sink,
        RunRejectedEvent(
            occurred_at=datetime.now().astimezone(),
            error=RunRejection(
                kind=type(error).__name__,
                message=str(error),
            ),
        ),
    )


def _events_for_run(events: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    """Project a cumulative checkpoint log onto one concrete Run attempt."""

    projected: list[dict[str, Any]] = []
    preamble: list[dict[str, Any]] = []
    current_run_id: str | None = None
    saw_run_marker = False
    selected_started = False
    for event in events:
        event_type = event.get("type")
        if event_type in {"run_started", "run_resumed"}:
            marker = event.get("run_id")
            marker_run_id = marker if isinstance(marker, str) else None
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

        explicit = event.get("run_id")
        explicit_run_id = explicit if isinstance(explicit, str) else None
        tree = event.get("tree")
        if (
            explicit_run_id is None
            and isinstance(tree, dict)
            and isinstance(tree.get("run_id"), str)
        ):
            explicit_run_id = tree["run_id"]
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


def _persist_run_observability(
    runtime_context: Any,
    checkpoint_mgr: CheckpointManager | None,
    task_id: str,
    *,
    result: str | None,
    event_start_offset: int | None,
) -> dict[str, Any]:
    """Copy durable result and task evidence into the canonical Run record."""

    manifest_updates: dict[str, Any] = {}
    if result is not None:
        result_artifact = runtime_context.artifacts_dir / "result.txt"
        runtime_context.atomic_write_run_file(result_artifact, result)
        manifest_updates.update(
            result_artifact="artifacts/result.txt",
            result_size=len(result.encode("utf-8")),
        )

    if checkpoint_mgr is None:
        return manifest_updates

    try:
        runtime_context.atomic_write_run_file_chunks(
            runtime_context.audit_dir / "task_tree.json",
            _checkpoint_file_chunks(
                checkpoint_mgr,
                task_id,
                relative_path="task_tree.json",
            ),
        )
    except FileNotFoundError:
        pass
    else:
        manifest_updates["task_tree_artifact"] = "audit/task_tree.json"

    if event_start_offset is not None:
        event_stats = {"count": 0, "complete": True}
        event_size = runtime_context.atomic_write_run_file_chunks(
            runtime_context.audit_dir / "task_events.jsonl",
            _run_event_chunks(
                checkpoint_mgr,
                task_id,
                start_offset=event_start_offset,
                stats=event_stats,
            ),
        )
        manifest_updates.update(
            task_events_artifact="audit/task_events.jsonl",
            task_events_run_id=runtime_context.run_id,
            task_events_count=event_stats["count"],
            task_events_size=event_size,
            task_events_complete=event_stats["complete"],
        )
    return manifest_updates


def _task_events_size(checkpoint_mgr: CheckpointManager, task_id: str) -> int:
    try:
        with checkpoint_mgr.task_storage(task_id) as storage:
            return storage.stat_file("task_events.jsonl").st_size
    except (FileNotFoundError, OSError, RuntimeError):
        return 0


def _run_event_chunks(
    checkpoint_mgr: CheckpointManager,
    task_id: str,
    *,
    start_offset: int,
    stats: dict[str, Any],
):
    with checkpoint_mgr.task_storage(task_id) as storage:
        try:
            with storage.open_binary_reader("task_events.jsonl") as stream:
                size = os.fstat(stream.fileno()).st_size
                if start_offset > size:
                    stats["complete"] = False
                    return
                stream.seek(start_offset)
                line_has_content = False
                while True:
                    chunk = stream.read(_RUN_ARTIFACT_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    segments = chunk.split(b"\n")
                    for segment in segments[:-1]:
                        if line_has_content or segment.strip():
                            stats["count"] += 1
                        line_has_content = False
                    if segments[-1].strip():
                        line_has_content = True
                    yield chunk
                if line_has_content:
                    stats["count"] += 1
        except FileNotFoundError:
            stats["complete"] = False


def _checkpoint_file_chunks(
    checkpoint_mgr: CheckpointManager,
    task_id: str,
    *,
    relative_path: str,
):
    """Stream one maintained checkpoint projection without replaying events."""

    with checkpoint_mgr.task_storage(task_id) as storage:
        with storage.open_binary_reader(relative_path) as stream:
            while True:
                chunk = stream.read(_RUN_ARTIFACT_COPY_CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk


def _checkpoint_age_seconds(created_at: Any) -> float:
    """Return checkpoint age in UTC, rejecting ambiguous lifecycle anchors."""

    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("checkpoint created_at is missing")
    try:
        parsed = datetime.fromisoformat(created_at.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("checkpoint created_at is invalid") from exc
    # Legacy migration interprets naive timestamps as UTC. Resume must use the
    # same rule so a migrated task is not selected and then rejected solely
    # because its original timestamp omitted an offset.
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds()


def _resolve_yaml_path(yaml_path: str | Path) -> Path:
    """Resolve a YAML path to an absolute path.

    Accepts either:
    - An absolute path (returned as-is after validation).
    - A relative path such as ``applications/ai_quality_analysis/workflows/code_review_agent.yaml``
      which is resolved against the project root (``C.agent_root``).

    Raises:
        FileNotFoundError: When the resolved path does not exist.
    """
    path = Path(yaml_path)
    if not path.is_absolute():
        path = Path(C.agent_root) / path

    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"YAML configuration file not found: {path}")
    return path


def execute_app(
    yaml_path: str | Path,
    resume_task_id: str | None = None,
    task_override: str | None = None,
    file_logging: bool | None = None,
    *,
    event_sink: RunEventSink | None = None,
) -> ApplicationRunResult:
    """Execute one Application and return its output plus canonical run receipt.

    Configuration errors are rejected before a run is allocated. Once a run
    exists, failures carry the same immutable RunInfo as lifecycle events.
    """

    try:
        resolved_path = _resolve_yaml_path(yaml_path)
        config = YamlAgentFactory._load_config_from_file(resolved_path)
        validate_runtime_agent_config(
            config,
            resolved_path,
            agent_root=C.agent_root,
        )
        effective_config = build_effective_agent_config(
            config,
            source_name=str(config.get("_yaml_file_path") or resolved_path),
        )

        checkpoint_config = effective_config.get("checkpoint", {})
        if not isinstance(checkpoint_config, dict):
            checkpoint_config = {}
        ckpt_enabled = checkpoint_config.get("enabled", True)
        heartbeat_interval = checkpoint_config.get("heartbeat_interval", 5.0)
        cleanup_on_success = checkpoint_config.get("cleanup_on_success", True)
        max_resume_age = checkpoint_config.get("max_resume_age", 604800)
        logging_builder = LoggingConfigBuilder().apply_mapping(
            effective_config.get("logging", {}),
            source="effective logging config",
        )

        agent_name = config["name"]
        effective_task = (
            task_override.strip() if task_override else config["description"].strip()
        )
        application_id = resolve_application_id(
            config,
            resolved_path,
            agent_root=C.agent_root,
        )
        runtime_home = resolve_runtime_home(effective_config, agent_root=C.agent_root)
        is_resume = resume_task_id is not None
        task_id = resume_task_id or generate_runtime_id("task")
        run_id = generate_runtime_id("run")
        started_at = datetime.now().astimezone()
        runtime_context = runtime_home.context(
            application_id=application_id,
            task_id=task_id,
            run_id=run_id,
        )
        public_run = _run_info(runtime_context, None)
    except BaseException as exc:
        _emit_preflight_rejection(event_sink, exc)
        raise
    started_emitted = False
    manifest_initialized = False
    phase: RunPhase = "initialization"
    log = None

    def emit_started() -> None:
        nonlocal started_emitted
        if started_emitted:
            return
        started_emitted = True
        _emit_lifecycle_event(
            event_sink,
            RunLifecycleEvent(
                event="run.started",
                run=public_run,
                occurred_at=datetime.now().astimezone(),
            ),
        )

    def update_failed_manifest(exc: BaseException) -> None:
        if not manifest_initialized:
            return
        try:
            runtime_context.update_manifest(
                status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                ended_at=datetime.now().astimezone().isoformat(),
                error=str(exc),
            )
        except Exception:
            return

    run_attempt_lease = None
    checkpoint_mgr: CheckpointManager | None = None
    task_lease: Any | None = None
    heartbeat: SupervisorHeartbeat | None = None
    file_history: FileHistoryManager | None = None
    supervisor: YamlConfiguredSupervisorAgent | None = None
    result_str = ""
    interrupt_resumable = False
    event_start_offset: int | None = None

    try:
        runtime_context.prepare_run()
        run_attempt_lease = runtime_context.run_lease()
        run_attempt_lease.acquire()
        try:
            runtime_context.write_manifest(
                yaml_path=str(resolved_path),
                agent_name=agent_name,
                mode="resume" if is_resume else "new",
                task_tree_observation={
                    "enabled": bool(ckpt_enabled),
                    "worker_agents_configured": bool(config.get("worker_agents")),
                },
            )
        except BaseException:
            # write_manifest persists the manifest before removing its starting
            # marker. Preserve terminal evidence if that post-commit cleanup fails.
            manifest_initialized = runtime_context.manifest_path.is_file()
            raise
        else:
            manifest_initialized = True

        with bind_run_context(runtime_context):
            logger_backend = initialize_run_logger(
                runtime_context,
                logging_builder=logging_builder,
                file_logging=file_logging,
            )
            public_run = _run_info(
                runtime_context,
                _configured_run_log_path(runtime_context, logger_backend),
            )
            emit_started()

            with bind_logger_backend(logger_backend, context=runtime_context):
                log = get_logger(logger_backend, __name__)
                outcome = "failed"
                outcome_error: str | None = None
                try:
                    phase = "execution"
                    log.info("Loading supervisor config: %s", resolved_path)

                    try:
                        from src.lib.runtime.retention import prune_runtime_if_due

                        prune_runtime_if_due(
                            runtime_home.root_dir,
                            effective_config.get("runtime", {}),
                        )
                    except Exception as exc:
                        log.debug("Runtime cleanup skipped: %s", exc)

                    if is_resume and not ckpt_enabled:
                        raise RuntimeError(
                            "Cannot resume: checkpoint is disabled in system.yaml"
                        )
                    if ckpt_enabled:
                        if is_resume:
                            try:
                                runtime_context.validate_checkpoint_path(
                                    require_exists=True,
                                )
                            except FileNotFoundError as exc:
                                raise FileNotFoundError(
                                    "No checkpoint found for "
                                    f"application={application_id}, task_id={task_id}"
                                ) from exc
                        else:
                            runtime_context.prepare_checkpoint()
                        checkpoint_mgr = CheckpointManager(
                            agent_name,
                            checkpoint_dir=runtime_context.checkpoint_dir,
                            run_id=run_id,
                        )
                        task_lease = checkpoint_mgr.task_lease(
                            require_exists=is_resume,
                        )
                        task_lease.acquire()
                        if is_resume:
                            runtime_context.validate_checkpoint_path(
                                require_exists=True,
                            )

                    if checkpoint_mgr is not None:
                        event_start_offset = _task_events_size(checkpoint_mgr, task_id)

                    if is_resume and checkpoint_mgr is not None:
                        tree = checkpoint_mgr.load_task_tree(task_id)
                        if tree is None:
                            raise FileNotFoundError(
                                "No checkpoint found for "
                                f"application={application_id}, task_id={task_id}"
                            )
                        if max_resume_age > 0:
                            try:
                                age = _checkpoint_age_seconds(tree.get("created_at"))
                            except (ValueError, TypeError) as exc:
                                raise FileNotFoundError(
                                    f"Checkpoint {task_id} has invalid created_at"
                                ) from exc
                            if age > max_resume_age:
                                raise FileNotFoundError(
                                    f"Checkpoint {task_id} expired "
                                    f"({age:.0f}s > {max_resume_age}s)"
                                )
                        tree_status = tree.get("status", "unknown")
                        if tree_status == "running":
                            from src.lib.heartbeat.status import detect_crashed_status

                            heartbeat_payload = checkpoint_mgr._read_json(
                                runtime_context.heartbeat_path
                            )
                            if detect_crashed_status(heartbeat_payload) == "crashed":
                                tree_status = "crashed"
                                log.info(
                                    "Detected crashed task "
                                    "(process dead, heartbeat stale)"
                                )
                        resumable_statuses = {"interrupted", "failed", "crashed"}
                        if tree_status not in resumable_statuses:
                            raise ValueError(
                                f"Checkpoint {task_id} is not resumable "
                                f"(status={tree_status}); start a new task instead"
                            )
                        checkpoint_mgr.record_run_resumed(task_id)
                        log.info(
                            "Resuming task %s (status=%s)",
                            task_id,
                            tree_status,
                        )
                    elif checkpoint_mgr is not None:
                        checkpoint_mgr.record_task_created(
                            task_id,
                            yaml_path=str(resolved_path),
                            agent_name=agent_name,
                            task_text=effective_task,
                            created_at=datetime.now().astimezone().isoformat(),
                        )
                        checkpoint_mgr.record_run_started(task_id)

                    if checkpoint_mgr is not None:
                        file_history = FileHistoryManager(
                            runtime_context.file_history_dir,
                            storage=checkpoint_mgr.directory_storage(
                                task_id,
                                runtime_context.file_history_dir,
                            ),
                        )
                        if is_resume:
                            try:
                                if file_history.restore_persisted_index():
                                    log.info(
                                        "Restored file history: %d snapshots, "
                                        "%d tracked files",
                                        file_history.snapshot_count,
                                        file_history.tracked_file_count,
                                    )
                            except Exception as exc:
                                log.warning(
                                    "Failed to restore file history: %s",
                                    exc,
                                )

                        heartbeat = SupervisorHeartbeat(
                            path=runtime_context.heartbeat_path,
                            agent_name=agent_name,
                            run_id=run_id,
                            interval=heartbeat_interval,
                            storage=checkpoint_mgr.task_storage(task_id),
                        )
                        heartbeat.start()

                        from src.lib.checkpoint.coordinator import (
                            CheckpointCoordinator as _CC,
                        )

                        _CC.set_pending_heartbeat(heartbeat)
                        _CC.set_pending_file_history(file_history)
                        log.info(
                            "File history manager prepared for pre-edit backups"
                        )

                    try:
                        from src.services.lsp import LSPServerManager
                        from src.services.lsp.config import LSPConfig

                        lsp_config = LSPConfig.from_yaml(
                            C.get("lsp_servers", {})
                        )
                        LSPServerManager.get_instance().initialize(
                            lsp_config,
                            project_root=str(C.agent_root),
                        )
                    except Exception as exc:
                        log.debug("LSP pre-warm skipped: %s", exc)

                    supervisor = YamlConfiguredSupervisorAgent(
                        config=config,
                        logger=logger_backend,
                    )

                    log.info("=" * 70)
                    log.info("Application: %s", application_id)
                    log.info("Task ID:     %s", task_id)
                    log.info("Run ID:      %s", run_id)
                    log.info("Mode:        %s", "RESUME" if is_resume else "NEW")
                    log.info("Task: %s", effective_task[:200])
                    log.info("=" * 70)

                    result = supervisor.run(
                        effective_task,
                        task_id=task_id,
                        run_id=run_id,
                        checkpoint_manager=checkpoint_mgr,
                        resume=is_resume,
                    )
                    result_str = "" if result is None else str(result)
                    phase = "finalization"
                    outcome = "completed"
                    log.info("=" * 70)
                    log.info("Execution completed successfully.")
                    log.info("=" * 70)
                except KeyboardInterrupt as exc:
                    outcome = "interrupted"
                    outcome_error = str(exc)
                    if checkpoint_mgr is not None:
                        try:
                            checkpoint_mgr.record_task_status_changed(
                                task_id,
                                "interrupted",
                                error=outcome_error or "run interrupted",
                            )
                            interrupt_resumable = True
                        except Exception as checkpoint_exc:
                            log.warning(
                                "Failed to persist resumable checkpoint: %s",
                                checkpoint_exc,
                            )
                    if interrupt_resumable:
                        log.warning(
                            "Interrupted by user. Checkpoint saved for task_id=%s",
                            task_id,
                        )
                        log.warning(
                            "Resume with: loom run %s --resume %s",
                            yaml_path,
                            task_id,
                        )
                    else:
                        log.warning(
                            "Interrupted by user; no resumable checkpoint is available"
                        )
                    raise
                except BaseException as exc:
                    outcome_error = str(exc)
                    if isinstance(exc, Exception):
                        log.error("Execution failed: %s", exc)
                    raise
                finally:
                    try:
                        from src.tools.shell.background_task import (
                            BackgroundTaskRegistry,
                        )

                        BackgroundTaskRegistry.get_instance().terminate_current_run()
                    except Exception as exc:
                        log.debug(
                            "Background task teardown skipped: %s",
                            exc,
                        )
                    try:
                        from src.tools.shell.process import ShellProcessRegistry

                        ShellProcessRegistry.get_instance().release_current_run()
                    except Exception as exc:
                        log.debug("Shell session teardown skipped: %s", exc)
                    if heartbeat is not None:
                        try:
                            heartbeat.stop()
                        except Exception:
                            pass
                        try:
                            heartbeat.close()
                        except Exception:
                            pass
                    try:
                        mcp_manager = getattr(supervisor, "_mcp_manager", None)
                        if mcp_manager is not None:
                            mcp_manager.disconnect_all()
                    except Exception:
                        pass
                    if file_history is not None:
                        try:
                            file_history.close()
                        except Exception:
                            pass
                    try:
                        durable_updates: dict[str, Any] = {}
                        if outcome == "completed":
                            durable_updates = _persist_run_observability(
                                runtime_context,
                                checkpoint_mgr,
                                task_id,
                                result=result_str,
                                event_start_offset=event_start_offset,
                            )
                        else:
                            try:
                                durable_updates = _persist_run_observability(
                                    runtime_context,
                                    checkpoint_mgr,
                                    task_id,
                                    result=None,
                                    event_start_offset=event_start_offset,
                                )
                            except Exception as exc:
                                log.warning(
                                    "Failed to persist Run observability: %s",
                                    exc,
                                )
                        manifest_updates = {
                            "status": outcome,
                            "ended_at": datetime.now().astimezone().isoformat(),
                            **durable_updates,
                        }
                        if outcome_error:
                            manifest_updates["error"] = outcome_error
                        try:
                            runtime_context.update_manifest(**manifest_updates)
                        except Exception as exc:
                            if outcome == "completed":
                                raise
                            log.warning(
                                "Failed to persist terminal manifest: %s",
                                exc,
                            )
                        if (
                            outcome == "completed"
                            and cleanup_on_success
                            and checkpoint_mgr is not None
                        ):
                            try:
                                tree = checkpoint_mgr.load_task_tree_projection(
                                    task_id,
                                    max_bytes=_TASK_TREE_CLEANUP_MAX_BYTES,
                                )
                                if (
                                    tree
                                    and tree.get("status") == "completed"
                                    and checkpoint_mgr.delete_task(task_id)
                                ):
                                    log.info(
                                        "Cleaned up checkpoint for completed task %s",
                                        task_id,
                                    )
                            except Exception:
                                pass
                    except KeyboardInterrupt as exc:
                        outcome = "interrupted"
                        outcome_error = str(exc)
                        if checkpoint_mgr is not None:
                            try:
                                checkpoint_mgr.record_task_status_changed(
                                    task_id,
                                    "interrupted",
                                    error=outcome_error or "run interrupted",
                                )
                                interrupt_resumable = True
                            except Exception as checkpoint_exc:
                                log.warning(
                                    "Failed to reopen checkpoint after finalization interruption: %s",
                                    checkpoint_exc,
                                )
                        raise
                    finally:
                        if task_lease is not None:
                            try:
                                task_lease.release()
                            except BaseException:
                                phase = "cleanup"
                                raise
                        if checkpoint_mgr is not None:
                            try:
                                checkpoint_mgr.close()
                            except BaseException:
                                phase = "cleanup"
                                raise
        if run_attempt_lease is not None:
            try:
                run_attempt_lease.release()
            except BaseException:
                phase = "cleanup"
                raise
            run_attempt_lease = None
    except BaseException as caught:
        terminal_error = caught
        if run_attempt_lease is not None:
            try:
                run_attempt_lease.release()
            except BaseException as cleanup_exc:
                terminal_error = cleanup_exc
                phase = "cleanup"
            run_attempt_lease = None
        emit_started()
        update_failed_manifest(terminal_error)
        ended_at = datetime.now().astimezone()
        if isinstance(terminal_error, KeyboardInterrupt):
            interrupted = ApplicationRunInterrupted(
                "Application run interrupted",
                run=public_run,
                phase=phase,
                original_error=terminal_error,
                resumable=interrupt_resumable,
            )
            _emit_lifecycle_event(
                event_sink,
                RunLifecycleEvent(
                    event="run.interrupted",
                    run=public_run,
                    occurred_at=ended_at,
                    error=str(interrupted),
                    phase=phase,
                ),
            )
            raise interrupted from terminal_error

        if not isinstance(terminal_error, Exception):
            detail = str(terminal_error)
            message = type(terminal_error).__name__
            if detail:
                message = f"{message}: {detail}"
        elif phase != "execution" or isinstance(
            terminal_error,
            (FileNotFoundError, ValueError),
        ):
            message = str(terminal_error)
        else:
            message = f"Agent execution failed: {terminal_error}"
        failure = ApplicationRunError(
            message,
            run=public_run,
            phase=phase,
            original_error=terminal_error,
        )
        _emit_lifecycle_event(
            event_sink,
            RunLifecycleEvent(
                event="run.failed",
                run=public_run,
                occurred_at=ended_at,
                error=message,
                phase=phase,
            ),
        )
        raise failure from terminal_error
    ended_at = datetime.now().astimezone()
    result = ApplicationRunResult(
        output=result_str,
        run=public_run,
        started_at=started_at,
        ended_at=ended_at,
    )
    _emit_lifecycle_event(
        event_sink,
        RunLifecycleEvent(
            event="run.completed",
            run=public_run,
            occurred_at=ended_at,
            output=result_str,
        ),
    )
    return result


def run_app(
    yaml_path: str | Path,
    resume_task_id: str | None = None,
    task_override: str | None = None,
    file_logging: bool | None = None,
) -> str:
    """Run one Application and return only its final string output.

    This compatibility entry point intentionally hides the structured receipt.
    Call :func:`execute_app` when the run identity or canonical paths are needed.
    """

    try:
        return execute_app(
            yaml_path,
            resume_task_id=resume_task_id,
            task_override=task_override,
            file_logging=file_logging,
        ).output
    except ApplicationRunInterrupted as exc:
        raise exc.original_error from exc
    except ApplicationRunError as exc:
        if exc.phase != "execution" or isinstance(
            exc.original_error,
            (FileNotFoundError, ValueError),
        ) or not isinstance(exc.original_error, Exception):
            raise exc.original_error from exc
        raise RuntimeError(str(exc)) from exc.original_error
