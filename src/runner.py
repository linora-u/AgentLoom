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

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from src.lib.smolagents.agent.yaml_agent_factory import (
    YamlAgentFactory,
    YamlConfiguredSupervisorAgent,
)

#: 启动 agent 时 YAML 中必须提供的字段。
_REQUIRED_YAML_FIELDS = ("name", "workflow", "description")


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


def validate_required_yaml_fields(
    config: dict,
    yaml_path: Path | str,
) -> None:
    """校验 YAML 配置中必须包含的关键字段。

    检查 ``name``、``workflow``、``description`` 三个字段是否存在且非空。

    Args:
        config: 已解析的 YAML 配置字典。
        yaml_path: YAML 文件路径，用于错误提示。

    Raises:
        ValueError: 当存在缺失或为空的必填字段时。
    """
    missing: list[str] = []
    for field in ("name", "description"):
        value = config.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field)

    workflow = config.get("workflow")
    workflow_valid = False
    if isinstance(workflow, str):
        workflow_valid = bool(workflow.strip())
    elif isinstance(workflow, list):
        workflow_valid = bool(workflow) and all(
            isinstance(item, str) and item.strip()
            for item in workflow
        )
    if not workflow_valid:
        missing.append("workflow")

    if missing:
        missing_str = ", ".join(missing)
        raise ValueError(
            f"YAML 配置文件 {yaml_path} 缺少必填字段: {missing_str}\n"
            f"请确保 YAML 中包含以下字段: {', '.join(_REQUIRED_YAML_FIELDS)}"
        )


def run_app(
    yaml_path: str | Path,
    resume_task_id: str | None = None,
    task_override: str | None = None,
    file_logging: bool | None = None,
) -> str:
    """Run a supervisor agent from a YAML configuration file.

    This is the **one-liner** entry point for launching any application.
    It handles logger initialisation, config loading, agent creation,
    and execution automatically.

    The task to execute is always taken from the YAML ``description``
    field.  The YAML must also contain ``name`` and ``workflow``.

    Args:
        yaml_path: Path to the supervisor YAML config.  Can be relative to
            the project root, e.g.
            ``"applications/ai_quality_analysis/workflows/code_review_agent.yaml``.
        resume_task_id: If provided, resume from this checkpoint instead of
            starting fresh.
        task_override: If provided, use this string as the task instead
            of the YAML ``description`` field.
        file_logging: Optional per-invocation override for
            ``logging.file_enabled``. ``None`` uses configuration.

    Returns:
        The string result produced by the supervisor agent.

    Raises:
        FileNotFoundError: If *yaml_path* cannot be resolved.
        ValueError: If required YAML fields are missing.
        RuntimeError: If the agent execution fails.
    """
    # Resolve configuration before any runtime component is constructed.
    resolved_path = _resolve_yaml_path(yaml_path)
    config = YamlAgentFactory._load_config_from_file(resolved_path)
    validate_required_yaml_fields(config, resolved_path)
    effective_config = build_effective_agent_config(
        config,
        source_name=str(config.get("_yaml_file_path") or resolved_path),
    )

    agent_name = config["name"]
    effective_task = task_override.strip() if task_override else config["description"].strip()
    application_id = resolve_application_id(
        config,
        resolved_path,
        agent_root=C.agent_root,
    )
    runtime_home = resolve_runtime_home(effective_config, agent_root=C.agent_root)
    is_resume = resume_task_id is not None
    task_id = resume_task_id or generate_runtime_id("task")
    run_id = generate_runtime_id("run")
    runtime_context = runtime_home.context(
        application_id=application_id,
        task_id=task_id,
        run_id=run_id,
    )
    runtime_context.prepare_run()
    run_attempt_lease = runtime_context.run_lease()
    run_attempt_lease.acquire()
    try:
        runtime_context.write_manifest(
            yaml_path=str(resolved_path),
            agent_name=agent_name,
            mode="resume" if is_resume else "new",
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
    except BaseException:
        run_attempt_lease.release()
        raise

    outcome = "failed"
    outcome_error: str | None = None
    checkpoint_mgr: CheckpointManager | None = None
    task_lease: Any | None = None
    heartbeat: SupervisorHeartbeat | None = None
    file_history: FileHistoryManager | None = None
    supervisor: YamlConfiguredSupervisorAgent | None = None

    with run_attempt_lease, bind_run_context(runtime_context):
        try:
            logger_backend = initialize_run_logger(
                runtime_context,
                logging_builder=logging_builder,
                file_logging=file_logging,
            )
        except Exception as exc:
            runtime_context.update_manifest(
                status="failed",
                ended_at=datetime.now().astimezone().isoformat(),
                error=str(exc),
            )
            raise
        with bind_logger_backend(logger_backend, context=runtime_context):
            log = get_logger(logger_backend, __name__)
            try:
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
                    raise RuntimeError("Cannot resume: checkpoint is disabled in system.yaml")
                if ckpt_enabled:
                    if is_resume:
                        try:
                            runtime_context.validate_checkpoint_path(
                                require_exists=True,
                            )
                        except FileNotFoundError as exc:
                            raise FileNotFoundError(
                                f"No checkpoint found for application={application_id}, task_id={task_id}"
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
                        # Validate again under the lease: the pre-lease check
                        # rejects symlinked ancestors, while require_exists
                        # prevents a cleanup race from recreating an empty task.
                        runtime_context.validate_checkpoint_path(
                            require_exists=True,
                        )

                if is_resume and checkpoint_mgr is not None:
                    tree = checkpoint_mgr.load_task_tree(task_id)
                    if tree is None:
                        raise FileNotFoundError(
                            f"No checkpoint found for application={application_id}, task_id={task_id}"
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
                                f"Checkpoint {task_id} expired ({age:.0f}s > {max_resume_age}s)"
                            )
                    tree_status = tree.get("status", "unknown")
                    if tree_status == "running":
                        from src.lib.heartbeat.status import detect_crashed_status

                        heartbeat_payload = checkpoint_mgr._read_json(runtime_context.heartbeat_path)
                        if detect_crashed_status(heartbeat_payload) == "crashed":
                            tree_status = "crashed"
                            log.info("Detected crashed task (process dead, heartbeat stale)")
                    resumable_statuses = {"interrupted", "failed", "crashed"}
                    if tree_status not in resumable_statuses:
                        raise ValueError(
                            f"Checkpoint {task_id} is not resumable "
                            f"(status={tree_status}); start a new task instead"
                        )
                    checkpoint_mgr.record_run_resumed(task_id)
                    log.info("Resuming task %s (status=%s)", task_id, tree_status)
                elif checkpoint_mgr is not None:
                    checkpoint_mgr.record_task_created(
                        task_id,
                        yaml_path=str(resolved_path),
                        agent_name=agent_name,
                        task_text=effective_task,
                        created_at=datetime.now().astimezone().isoformat(),
                    )
                    checkpoint_mgr.record_run_started(task_id)

                file_history = None
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
                                    "Restored file history: %d snapshots, %d tracked files",
                                    file_history.snapshot_count,
                                    file_history.tracked_file_count,
                                )
                        except Exception as exc:
                            log.warning("Failed to restore file history: %s", exc)

                    heartbeat = SupervisorHeartbeat(
                        path=runtime_context.heartbeat_path,
                        agent_name=agent_name,
                        run_id=run_id,
                        interval=heartbeat_interval,
                        storage=checkpoint_mgr.task_storage(task_id),
                    )
                    heartbeat.start()

                    from src.lib.checkpoint.coordinator import CheckpointCoordinator as _CC

                    _CC.set_pending_heartbeat(heartbeat)
                    _CC.set_pending_file_history(file_history)
                    log.info("File history manager prepared for pre-edit backups")

                # Pre-warm LSP servers after the run logger is bound.
                try:
                    from src.services.lsp import LSPServerManager
                    from src.services.lsp.config import LSPConfig

                    lsp_config = LSPConfig.from_yaml(C.get("lsp_servers", {}))
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
                outcome = "completed"
                log.info("=" * 70)
                log.info("Execution completed successfully.")
                log.info("=" * 70)
                return result_str
            except KeyboardInterrupt:
                outcome = "interrupted"
                log.warning("Interrupted by user. Checkpoint saved for task_id=%s", task_id)
                log.warning("Resume with: loom run %s --resume %s", yaml_path, task_id)
                raise
            except (FileNotFoundError, ValueError) as exc:
                outcome_error = str(exc)
                raise
            except Exception as exc:
                outcome_error = str(exc)
                log.error("Execution failed: %s", exc)
                raise RuntimeError(f"Agent execution failed: {exc}") from exc
            finally:
                try:
                    from src.tools.shell.background_task import BackgroundTaskRegistry

                    BackgroundTaskRegistry.get_instance().terminate_current_run()
                except Exception as exc:
                    log.debug("Background task teardown skipped: %s", exc)
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
                if outcome == "completed" and cleanup_on_success and checkpoint_mgr is not None:
                    try:
                        tree = checkpoint_mgr.load_task_tree(task_id)
                        if tree and tree.get("status") == "completed":
                            checkpoint_mgr.delete_task(task_id)
                            log.info("Cleaned up checkpoint for completed task %s", task_id)
                    except Exception:
                        pass
                manifest_updates = {
                    "status": outcome,
                    "ended_at": datetime.now().astimezone().isoformat(),
                }
                if outcome_error:
                    manifest_updates["error"] = outcome_error
                try:
                    runtime_context.update_manifest(**manifest_updates)
                finally:
                    if task_lease is not None:
                        task_lease.release()
                    if checkpoint_mgr is not None:
                        checkpoint_mgr.close()
