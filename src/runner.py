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

from datetime import datetime
from pathlib import Path

from src.lib.checkpoint import CheckpointManager
from src.lib.checkpoint.file_history import FileHistoryManager
from src.lib.config import C, build_effective_agent_config
from src.lib.heartbeat import SupervisorHeartbeat
from src.lib.logging import get_logger, initialize_global_logger_once
from src.lib.smolagents.agent.yaml_agent_factory import (
    YamlAgentFactory,
    YamlConfiguredSupervisorAgent,
)
from src.trace import generate_id

#: 启动 agent 时 YAML 中必须提供的字段。
_REQUIRED_YAML_FIELDS = ("name", "workflow", "description")


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
    log_to_file: bool = False,
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

    Returns:
        The string result produced by the supervisor agent.

    Raises:
        FileNotFoundError: If *yaml_path* cannot be resolved.
        ValueError: If required YAML fields are missing.
        RuntimeError: If the agent execution fails.
    """
    # 1. Resolve config path and load config first (needed for logger init).
    resolved_path = _resolve_yaml_path(yaml_path)
    config = YamlAgentFactory._load_config_from_file(resolved_path)
    validate_required_yaml_fields(config, resolved_path)
    effective_config = build_effective_agent_config(
        config,
        source_name=str(config.get("_yaml_file_path") or resolved_path),
    )

    # 2. Initialise logging with the YAML app name.
    agent_name = config["name"]
    global_logger = initialize_global_logger_once(agent_name)
    log = get_logger(global_logger, __name__)
    log.info("Loading supervisor config: %s", resolved_path)

    # 3. Extract task from description.
    effective_task = task_override.strip() if task_override else config["description"].strip()

    # 4. Pre-warm LSP servers (long-lived, aligned with Claude Code).
    try:
        from src.services.lsp import LSPServerManager
        from src.services.lsp.config import LSPConfig

        lsp_yaml = C.get("lsp_servers", {})
        lsp_config = LSPConfig.from_yaml(lsp_yaml)
        lsp_manager = LSPServerManager.get_instance()
        lsp_manager.initialize(lsp_config, project_root=str(C.agent_root))
    except Exception as exc:
        log.debug("LSP pre-warm skipped: %s", exc)

    # 5. Create supervisor agent.
    supervisor = YamlConfiguredSupervisorAgent(
        config=config,
        logger=global_logger,
    )

    # 5. Checkpoint & heartbeat setup. Application-level system.yaml is part of
    # the effective configuration, so isolated validation apps can disable raw
    # checkpoint artifacts without changing the global project setting.
    checkpoint_config = effective_config.get("checkpoint", {})
    if not isinstance(checkpoint_config, dict):
        checkpoint_config = {}
    ckpt_enabled = checkpoint_config.get("enabled", True)
    heartbeat_interval = checkpoint_config.get("heartbeat_interval", 5.0)
    cleanup_on_success = checkpoint_config.get("cleanup_on_success", True)
    max_resume_age = checkpoint_config.get("max_resume_age", 604800)

    # Obtain the current per-run log directory (already created by logger init).
    from src.lib.logging.logger_manager import get_current_run_log_dir as _get_run_dir
    _current_run_log_dir = _get_run_dir()

    is_resume = resume_task_id is not None

    if ckpt_enabled:
        if is_resume:
            # Resume: create a scan-only manager first to locate the task,
            # then re-create with the resolved run_log_dir.
            _scan_mgr = CheckpointManager(agent_name)
            _resolved_dir = _scan_mgr._resolve_task_run_dir(resume_task_id)
            if _resolved_dir is None:
                raise FileNotFoundError(
                    f"No checkpoint found for task_id={resume_task_id} "
                    f"under .logs/{agent_name}/*/checkpoints/"
                )
            checkpoint_mgr: CheckpointManager | None = CheckpointManager(
                agent_name, run_log_dir=_resolved_dir,
            )
        else:
            checkpoint_mgr = CheckpointManager(
                agent_name, run_log_dir=_current_run_log_dir,
            )
    else:
        checkpoint_mgr = None

    if is_resume and checkpoint_mgr is not None:
        task_id = resume_task_id
        tree = checkpoint_mgr.load_task_tree(task_id)
        if tree is None:
            raise FileNotFoundError(
                f"No checkpoint found for task_id={task_id} "
                f"under .logs/{agent_name}/*/checkpoints/"
            )
        # Check expiry
        created_at = tree.get("created_at", "")
        if created_at and max_resume_age > 0:
            try:
                from datetime import datetime as _dt
                age = (datetime.now().astimezone() - _dt.fromisoformat(created_at)).total_seconds()
                if age > max_resume_age:
                    raise FileNotFoundError(
                        f"Checkpoint {task_id} expired ({age:.0f}s > {max_resume_age}s)"
                    )
            except (ValueError, TypeError):
                pass
        tree_status = tree.get("status", "unknown")
        # Detect crashed: if status is still "running" but process is dead
        if tree_status == "running":
            from src.lib.heartbeat.status import detect_crashed_status as _detect_crashed_status
            hb = checkpoint_mgr._read_json(checkpoint_mgr._heartbeat_path(task_id))
            detected = _detect_crashed_status(hb)
            if detected == "crashed":
                tree_status = "crashed"
                log.info("Detected crashed task (process dead, heartbeat stale)")
        log.info("Resuming task %s (status=%s)", task_id, tree_status)
    elif is_resume and checkpoint_mgr is None:
        raise RuntimeError("Cannot resume: checkpoint is disabled in system.yaml")
    else:
        task_id = generate_id(f"supervisor_{agent_name}", prefix="task")
        if checkpoint_mgr is not None:
            checkpoint_mgr.record_task_created(
                task_id,
                yaml_path=str(yaml_path),
                agent_name=agent_name,
                task_text=effective_task,
                created_at=datetime.now().astimezone().isoformat(),
            )
            # Record task_id → run_log_dir mapping in index for resume lookup.
            checkpoint_mgr.save_task_to_index(task_id)

    # 6. File history manager (pre-edit backups for rewind).
    file_history = None
    if ckpt_enabled and checkpoint_mgr is not None:
        fh_dir = checkpoint_mgr._task_dir(task_id) / "file-history"
        file_history = FileHistoryManager(fh_dir)
        if is_resume:
            # Restore file history state from persisted index.
            try:
                import json as _json
                idx_path = fh_dir / "snapshots.json"
                if idx_path.exists():
                    data = _json.loads(idx_path.read_text(encoding="utf-8"))
                    file_history.restore_from_index(data)
                    log.info("Restored file history: %d snapshots, %d tracked files",
                             file_history.snapshot_count, file_history.tracked_file_count)
            except Exception as exc:
                log.warning("Failed to restore file history: %s", exc)

    heartbeat = SupervisorHeartbeat(
        path=checkpoint_mgr._heartbeat_path(task_id) if checkpoint_mgr else Path(f"/tmp/heartbeat_{task_id}.json"),
        agent_name=agent_name,
        interval=heartbeat_interval,
    )
    heartbeat.start()

    # Register heartbeat + file history so coordinator.activate() can inject them.
    try:
        from src.lib.checkpoint.coordinator import CheckpointCoordinator as _CC
        _CC.set_pending_heartbeat(heartbeat)
        if file_history is not None:
            _CC.set_pending_file_history(file_history)
    except Exception:
        pass

    # File-history hooks are registered on each agent's own HookManager inside
    # the agent run lifecycle. Registering on the global singleton would miss
    # the per-agent tool shim path.
    if file_history is not None:
        log.info("File history manager prepared for pre-edit backups")

    log.info("=" * 70)
    log.info("Application: %s", agent_name)
    log.info("Task ID:     %s", task_id)
    log.info("Mode:        %s", "RESUME" if is_resume else "NEW")
    log.info("Task: %s", effective_task[:200])
    log.info("=" * 70)

    # 6. Execute.
    try:
        result = supervisor.run(
            effective_task,
            task_id=task_id,
            checkpoint_manager=checkpoint_mgr,
            resume=is_resume,
        )
        result_str = "" if result is None else str(result)
        log.info("=" * 70)
        log.info("Execution completed successfully.")
        log.info("=" * 70)
        return result_str
    except KeyboardInterrupt:
        # P4: checkpoint is already saved by base_agent — only log here.
        log.warning("Interrupted by user. Checkpoint saved for task_id=%s", task_id)
        log.warning("Resume with:  loom run %s --resume %s", yaml_path, task_id)
        raise
    except Exception as exc:
        log.error("Execution failed: %s", exc)
        raise RuntimeError(f"Agent execution failed: {exc}") from exc
    finally:
        heartbeat.stop()
        # Stop worker heartbeat writers (managed by coordinator).
        try:
            from src.lib.checkpoint.coordinator import CheckpointCoordinator
            coord = CheckpointCoordinator.current()
            if coord is not None:
                coord.stop_all_worker_heartbeats()
        except Exception:
            pass
        # Disconnect MCP clients (if any).
        try:
            mcp_mgr = getattr(supervisor, "_mcp_manager", None)
            if mcp_mgr is not None:
                mcp_mgr.disconnect_all()
        except Exception:
            pass
        # Self-learning history is recorded live by canonical lifecycle hooks
        # under .agentloom/sessions/events and indexed independently of
        # checkpoint retention.
        # M1: cleanup_on_success — remove checkpoint after successful completion.
        if cleanup_on_success and checkpoint_mgr is not None:
            try:
                tree = checkpoint_mgr.load_task_tree(task_id)
                if tree and tree.get("status") == "completed":
                    checkpoint_mgr.delete_task(task_id)
                    log.info("Cleaned up checkpoint for completed task %s", task_id)
            except Exception:
                pass
