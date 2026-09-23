"""Runtime-owned CLI commands and maintenance operations."""

from __future__ import annotations

from pathlib import Path

import click


def _configured_runtime_home():
    from agentloom.config import C
    from agentloom.execution import resolve_runtime_home

    return resolve_runtime_home(C.raw, agent_root=C.agent_root)


def _configured_checkpoints_root():
    try:
        return _configured_runtime_home().checkpoints_root
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@click.command("list-tasks")
@click.option("--detail", is_flag=True, default=False, help="Show worker-level details.")
def list_tasks(detail: bool) -> None:
    """List all retained checkpoint tasks."""

    from agentloom.execution.checkpoint.checkpoint_manager import list_all_tasks

    tasks = list_all_tasks(checkpoints_root=_configured_checkpoints_root())
    if not tasks:
        click.echo("No checkpoint tasks found.")
        return

    icons = {
        "interrupted": "⏸",
        "failed": "❌",
        "running": "🔄",
        "completed": "✅",
        "crashed": "💥",
    }
    for task in tasks:
        status = task.get("status", "")
        line = (
            f"  {icons.get(status, '?')}  {task['task_id']}  "
            f"[{task['agent_name']}]  {status}  "
            f"{task.get('interrupted_at') or task.get('created_at', '')}"
        )
        if status == "crashed":
            line += "  (process died — resumable)"
        click.echo(line)
        if detail:
            workers = task.get("workers", [])
            for index, worker in enumerate(workers):
                prefix = "  └─" if index == len(workers) - 1 else "  ├─"
                worker_status = worker.get("status", "")
                step = worker.get("step")
                step_info = f" ({step} steps)" if step else ""
                error = worker.get("error")
                error_info = f"  err: {error[:60]}" if error else ""
                click.echo(
                    f"       {prefix} {icons.get(worker_status, '?')} "
                    f"{worker.get('agent_name', '?')} "
                    f"#{worker.get('call_index', 0)}  "
                    f"{worker_status or '?'}{step_info}{error_info}"
                )


@click.command("clean-tasks")
@click.option("--all", "clean_all", is_flag=True, default=False, help="Remove ALL checkpoints.")
@click.option("--before", "before_days", type=int, default=None, help="Remove checkpoints older than N days.")
def clean_tasks(clean_all: bool, before_days: int | None) -> None:
    """Clean old checkpoint data."""

    from agentloom.execution.checkpoint import (
        cleanup_expired_tasks,
        delete_checkpoint_task_if_inactive,
    )
    from agentloom.execution.checkpoint.checkpoint_manager import list_all_tasks

    checkpoints_root = _configured_checkpoints_root()
    tasks = list_all_tasks(checkpoints_root=checkpoints_root)
    if not tasks:
        click.echo("No checkpoints to clean.")
        return

    max_age_seconds = (before_days if before_days is not None else 7) * 86400
    if clean_all:
        total_removed = sum(
            int(
                delete_checkpoint_task_if_inactive(
                    Path(str(task["checkpoint_dir"]))
                )
            )
            for task in tasks
        )
    else:
        total_removed = cleanup_expired_tasks(
            checkpoints_root=checkpoints_root,
            max_age_seconds=max_age_seconds,
        )
    click.echo(f"Cleaned {total_removed} checkpoint(s).")


@click.command("clean-runtime")
def clean_runtime_command() -> None:
    """Apply bounded retention to run directories and raw artifacts."""

    from agentloom.config import C
    from agentloom.execution.retention import clean_runtime

    runtime_config = C.get("runtime", {})
    if not isinstance(runtime_config, dict):
        runtime_config = {}
    home = _configured_runtime_home()
    try:
        home.validate_root()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    result = clean_runtime(home.root_dir, config=runtime_config)
    if result.skipped:
        reason = result.skip_reason or "runtime cleanup did not run"
        raise click.ClickException(f"runtime cleanup skipped: {reason}")
    click.echo(
        "Cleaned runtime: "
        f"runs={result.removed_run_count}, "
        f"artifacts={result.removed_artifact_count}, "
        f"reclaimed_bytes={result.reclaimed_bytes}."
    )
    for error in result.errors:
        click.echo(f"warning: {error}", err=True)
