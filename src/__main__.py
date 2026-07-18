"""
CLI entry point for the src package.

After ``pip install -e .``, the ``loom`` command is available::

    # Run a supervisor agent
    loom run applications/<app>/workflows/<agent>.yaml

    # Generate a demo script
    loom create applications/<app>/workflows/<agent>.yaml
    loom create applications/<app>/workflows/<agent>.yaml -o my_app.py
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext, redirect_stdout
from datetime import UTC, datetime
from typing import Any, TextIO

import click

from src.schedules.cli import schedules as _schedules_command

_MAIN_EPILOG = """\
\b
Examples:
  loom run applications/<app>/workflows/<agent>.yaml
  loom create applications/<app>/workflows/<agent>.yaml
  loom create applications/<app>/workflows/<agent>.yaml -o my_app.py
  loom schedules add applications/<app>/workflows/<agent>.yaml --every 1h
  loom schedules serve
  loom ui

Use 'loom <command> -h' for more details on each command.
"""
_EX_TEMPFAIL = 75


def _run_info_payload(run_info: Any) -> dict[str, object]:
    return {
        "application_id": run_info.application_id,
        "task_id": run_info.task_id,
        "run_id": run_info.run_id,
        "run_dir": str(run_info.run_dir),
        "manifest_path": str(run_info.manifest_path),
        "log_path": str(run_info.log_path) if run_info.log_path is not None else None,
    }


def _run_event_payload(event: Any) -> dict[str, object]:
    occurred_at = event.occurred_at
    if event.event == "run.rejected":
        return {
            "schema_version": event.schema_version,
            "event": event.event,
            "occurred_at": occurred_at.isoformat(),
            "phase": event.phase,
            "error": {
                "kind": event.error.kind,
                "message": event.error.message,
                "retryable": event.error.retryable,
            },
        }
    payload: dict[str, object] = {
        "schema_version": event.schema_version,
        "event": event.event,
        "occurred_at": occurred_at.isoformat(),
        "run": _run_info_payload(event.run),
    }
    for field in ("output", "error", "phase"):
        value = getattr(event, field, None)
        if value is not None:
            payload[field] = value
    return payload


def _emit_jsonl_record(payload: dict[str, object], stream: TextIO) -> None:
    click.echo(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        file=stream,
    )
    stream.flush()


@contextmanager
def _isolated_jsonl_stdout() -> Iterator[TextIO]:
    """Reserve the original fd 1 for JSONL and route all other fd 1 writes to stderr."""

    protocol_stream = click.get_text_stream("stdout")
    try:
        protocol_fd = protocol_stream.fileno()
    except (AttributeError, OSError, ValueError):
        # Click's in-process test runner exposes an in-memory stream without a
        # file descriptor. Python-level redirection is the strongest isolation
        # available there; real CLI processes always take the fd-level branch.
        with redirect_stdout(sys.stderr):
            yield protocol_stream
        return

    if protocol_fd != 1:
        with redirect_stdout(sys.stderr):
            yield protocol_stream
        return

    protocol_stream.flush()
    sys.stderr.flush()
    saved_stdout_fd = os.dup(protocol_fd)
    lifecycle_stream: TextIO | None = None
    try:
        lifecycle_fd = os.dup(saved_stdout_fd)
        lifecycle_stream = os.fdopen(
            lifecycle_fd,
            "w",
            encoding=protocol_stream.encoding or "utf-8",
            errors=protocol_stream.errors or "replace",
            buffering=1,
        )
        os.dup2(2, protocol_fd)
        with redirect_stdout(sys.stderr):
            yield lifecycle_stream
    finally:
        if lifecycle_stream is not None:
            lifecycle_stream.flush()
        os.dup2(saved_stdout_fd, protocol_fd)
        os.close(saved_stdout_fd)
        if lifecycle_stream is not None:
            lifecycle_stream.close()


def _run_rejected_payload(
    error: BaseException,
    *,
    message: str | None = None,
    retryable: bool = False,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event": "run.rejected",
        "occurred_at": datetime.now(UTC).isoformat(),
        "phase": "preflight",
        "error": {
            "kind": type(error).__name__,
            "message": str(error) if message is None else message,
            "retryable": retryable,
        },
    }


@click.group(context_settings={"help_option_names": ["-h", "--help"]}, epilog=_MAIN_EPILOG)
def main():
    """AgentLoom – AI agent framework CLI."""
    import os
    from pathlib import Path as _Path

    try:
        from src.lib.config import C

        agent_root = _Path(C.agent_root).resolve()
        if _Path.cwd().resolve() != agent_root:
            os.chdir(agent_root)
    except Exception:
        pass  # discovery may fail here; let sub-commands report the real error


def _has_transient_provider_error(error: BaseException) -> bool:
    """Return true only for a trusted transient LiteLLM exception chain."""
    from litellm.exceptions import (
        APIConnectionError,
        AuthenticationError,
        BadRequestError,
        InternalServerError,
        PermissionDeniedError,
        RateLimitError,
        ServiceUnavailableError,
        Timeout,
    )
    from smolagents import AgentMaxStepsError, AgentParsingError

    from src.lib.smolagents.models.litellm_retry import (
        ProviderCallBudgetExceeded,
    )
    from src.lib.smolagents.models.tool_call_parser import ToolCallParseError

    transient_types = (
        Timeout,
        APIConnectionError,
        InternalServerError,
        ServiceUnavailableError,
        RateLimitError,
    )
    denied_types = (
        SystemExit,
        KeyboardInterrupt,
        click.ClickException,
        click.exceptions.Exit,
        AuthenticationError,
        PermissionDeniedError,
        BadRequestError,
        ProviderCallBudgetExceeded,
        AgentParsingError,
        AgentMaxStepsError,
        ToolCallParseError,
    )
    current: BaseException | None = error
    visited: set[int] = set()
    transient_seen = False
    while current is not None:
        identity = id(current)
        if identity in visited:
            return False
        visited.add(identity)
        if isinstance(current, denied_types):
            return False
        if isinstance(current, transient_types):
            transient_seen = True
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return transient_seen


_RUN_EPILOG = """\
\b
Examples:
  loom run applications/test_demo/workflows/test_agent.yaml
  loom run applications/test_demo/workflows/test_agent.yaml --no-file-log
  loom run applications/test_demo/workflows/test_agent.yaml --resume task_xxx
  loom run applications/test_demo/workflows/test_agent.yaml --task "Inspect this repository"
  loom run applications/test_demo/workflows/test_agent.yaml --output-format jsonl
"""


@main.command(epilog=_RUN_EPILOG)
@click.argument("yaml_path")
@click.option(
    "--no-file-log",
    is_flag=True,
    default=False,
    help="Disable this run's file log (configuration is used by default).",
)
@click.option("--resume", "resume_task_id", default=None, help="Resume from a checkpoint task ID.")
@click.option("--task", "task_override", default=None, help="Override the task from the application YAML.")
@click.option(
    "--output-format",
    type=click.Choice(("text", "jsonl"), case_sensitive=False),
    default="text",
    show_default=True,
    help="Choose human-readable output or lifecycle JSON Lines.",
)
def run(
    yaml_path: str,
    no_file_log: bool,
    resume_task_id: str | None,
    task_override: str | None,
    output_format: str,
):
    """Run a supervisor agent from a YAML configuration."""
    emitted_events: set[str] = set()
    event_stream: TextIO | None = None

    def emit_rejected(
        error: BaseException,
        *,
        message: str | None = None,
        retryable: bool = False,
    ) -> None:
        if event_stream is None or emitted_events:
            return
        _emit_jsonl_record(
            _run_rejected_payload(
                error,
                message=message,
                retryable=retryable,
            ),
            event_stream,
        )

    output_context = _isolated_jsonl_stdout() if output_format == "jsonl" else nullcontext(None)
    with output_context as active_event_stream:
        event_stream = active_event_stream
        try:
            if output_format == "jsonl":
                from src.runner import execute_app

                def emit_event(event: Any) -> None:
                    assert event_stream is not None
                    _emit_jsonl_record(_run_event_payload(event), event_stream)
                    emitted_events.add(event.event)

                execute_app(
                    yaml_path,
                    file_logging=False if no_file_log else None,
                    resume_task_id=resume_task_id,
                    task_override=task_override,
                    event_sink=emit_event,
                )
            else:
                from src.runner import execute_app

                result = execute_app(
                    yaml_path,
                    file_logging=False if no_file_log else None,
                    resume_task_id=resume_task_id,
                    task_override=task_override,
                ).output
                click.echo(result)
        except KeyboardInterrupt as exc:
            if not emitted_events:
                emit_rejected(exc, message="interrupted before run started")
            from src.application_run import ApplicationRunInterrupted

            if isinstance(exc, ApplicationRunInterrupted) and exc.resumable:
                click.echo("\nInterrupted. Use --resume to continue.", err=True)
            else:
                click.echo(
                    "\nInterrupted; no resumable checkpoint is available.",
                    err=True,
                )
            raise click.exceptions.Exit(130) from exc
        except SystemExit as exc:
            emit_rejected(exc, message="nested process exit")
            click.echo("\n Execution failed: nested process exit", err=True)
            raise click.exceptions.Exit(1) from exc
        except Exception as exc:
            retryable = _has_transient_provider_error(exc)
            if not emitted_events:
                emit_rejected(exc, retryable=retryable)
            click.echo(f"\n Execution failed: {exc}", err=True)
            raise click.exceptions.Exit(_EX_TEMPFAIL if retryable else 1) from exc


# ─────────────────────────────────────────────
# loom list-tasks
# ─────────────────────────────────────────────

def _configured_runtime_home():
    from src.lib.config import C
    from src.lib.runtime import resolve_runtime_home

    return resolve_runtime_home(C.raw, agent_root=C.agent_root)


def _configured_checkpoints_root():
    try:
        return _configured_runtime_home().checkpoints_root
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("list-tasks")
@click.option("--detail", is_flag=True, default=False, help="Show worker-level details.")
def list_tasks(detail: bool):
    """List all retained checkpoint tasks."""
    from src.lib.checkpoint.checkpoint_manager import list_all_tasks

    tasks = list_all_tasks(
        checkpoints_root=_configured_checkpoints_root()
    )
    if not tasks:
        click.echo("No checkpoint tasks found.")
        return

    _ICONS = {"interrupted": "⏸", "failed": "❌", "running": "🔄", "completed": "✅", "crashed": "💥"}
    for t in tasks:
        status = t.get("status", "")
        icon = _ICONS.get(status, "?")
        line = (
            f"  {icon}  {t['task_id']}  [{t['agent_name']}]  "
            f"{status}  {t.get('interrupted_at') or t.get('created_at', '')}"
        )
        if status == "crashed":
            line += "  (process died — resumable)"
        click.echo(line)

        # Show worker details if --detail flag is set.
        if detail:
            workers = t.get("workers", [])
            for i, w in enumerate(workers):
                is_last = (i == len(workers) - 1)
                prefix = "  └─" if is_last else "  ├─"
                w_icon = _ICONS.get(w.get("status", ""), "?")
                ci = w.get("call_index", 0)
                w_step = w.get("step")
                step_info = f" ({w_step} steps)" if w_step else ""
                w_err = w.get("error")
                err_info = f"  err: {w_err[:60]}" if w_err else ""
                click.echo(
                    f"       {prefix} {w_icon} {w.get('agent_name', '?')} #{ci}"
                    f"  {w.get('status', '?')}{step_info}{err_info}"
                )


# ─────────────────────────────────────────────
# loom clean-tasks
# ─────────────────────────────────────────────

@main.command("clean-tasks")
@click.option("--all", "clean_all", is_flag=True, default=False, help="Remove ALL checkpoints.")
@click.option("--before", "before_days", type=int, default=None, help="Remove checkpoints older than N days.")
def clean_tasks(clean_all: bool, before_days: int | None):
    """Clean old checkpoint data."""
    from pathlib import Path as _Path

    from src.lib.checkpoint import (
        cleanup_expired_tasks,
        delete_checkpoint_task_if_inactive,
    )
    from src.lib.checkpoint.checkpoint_manager import list_all_tasks

    checkpoints_root = _configured_checkpoints_root()
    tasks = list_all_tasks(checkpoints_root=checkpoints_root)
    if not tasks:
        click.echo("No checkpoints to clean.")
        return

    max_age_seconds = (before_days if before_days is not None else 7) * 86400
    if clean_all:
        total_removed = sum(
            int(delete_checkpoint_task_if_inactive(_Path(str(task["checkpoint_dir"]))))
            for task in tasks
        )
    else:
        total_removed = cleanup_expired_tasks(
            checkpoints_root=checkpoints_root,
            max_age_seconds=max_age_seconds,
        )
    click.echo(f"Cleaned {total_removed} checkpoint(s).")


# ─────────────────────────────────────────────
# loom clean-runtime / migrate-runtime
# ─────────────────────────────────────────────

@main.command("clean-runtime")
def clean_runtime_command() -> None:
    """Apply bounded retention to run directories and raw artifacts."""
    from src.lib.config import C
    from src.lib.runtime.retention import clean_runtime

    runtime_config = C.get("runtime", {})
    if not isinstance(runtime_config, dict):
        runtime_config = {}
    home = _configured_runtime_home()
    try:
        home.validate_root()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    result = clean_runtime(
        home.root_dir,
        config=runtime_config,
    )
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


@main.command("migrate-runtime")
@click.option(
    "--dry-run/--apply",
    default=True,
    show_default=True,
    help="Preview or migrate legacy checkpoints, .logs, and .runtime workspaces.",
)
def migrate_runtime_command(dry_run: bool) -> None:
    """Migrate legacy checkpoints and archive the unscoped agent workspace."""
    from datetime import timedelta as _timedelta
    from pathlib import Path as _Path

    from src.lib.config import C
    from src.lib.runtime.migration import migrate_runtime
    from src.lib.runtime.workspace_migration import (
        archive_legacy_agent_workspaces,
        preview_legacy_agent_workspaces,
    )

    max_age = _timedelta(days=7)

    home = _configured_runtime_home()
    try:
        home.validate_root()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    legacy_logs = _Path(C.agent_root) / ".logs"
    legacy_workspace = _Path(C.agent_root) / ".runtime"
    try:
        result = migrate_runtime(
            legacy_logs,
            home.root_dir,
            dry_run=dry_run,
            archive_legacy=not dry_run,
            agent_root=C.agent_root,
            max_age=max_age,
        )
        workspace_result = (
            preview_legacy_agent_workspaces(legacy_workspace)
            if dry_run
            else archive_legacy_agent_workspaces(legacy_workspace, home.root_dir)
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"Runtime migration ({'dry-run' if dry_run else 'apply'}): "
        f"candidates={result.plan.candidate_count}, "
        f"skipped={result.plan.skipped_count}, "
        f"migrated={result.migrated_count}, "
        f"already_migrated={result.already_migrated_count}."
    )
    for candidate in result.plan.candidates:
        progress = ",".join(candidate.progress_kinds)
        click.echo(
            f"  migrate {candidate.task_id} -> {candidate.application_id} "
            f"[{progress}]"
        )
    for skipped in result.plan.skipped:
        click.echo(f"  skip {skipped.task_id}: {skipped.reason}")
    if result.archive_dir is not None:
        click.echo(f"Archived legacy logs: {result.archive_dir}")
    click.echo(
        "Legacy agent workspace: "
        f"files={workspace_result.file_count}, "
        f"bytes={workspace_result.total_bytes}, "
        f"archived={workspace_result.archive_dir or 'no'}."
    )


# ─────────────────────────────────────────────
# loom sessions
# ─────────────────────────────────────────────

@main.group()
def sessions():
    """Search and manage indexed AgentLoom run history."""


@sessions.command("index")
@click.argument("path", required=False, default=None)
def sessions_index(path: str):
    """Report ledger counts or import canonical self-learning event exports."""
    import json as _json
    from src.extensions.self_learning.session_index import SessionIndex

    index = SessionIndex()
    result = index.index_all(path)
    click.echo(_json.dumps(result, ensure_ascii=False, indent=2, default=str))


@sessions.command("search")
@click.argument("query")
@click.option("--limit", default=10, show_default=True, type=int)
@click.option("--agent", default=None)
@click.option("--app", default=None)
@click.option("--since", default=None)
@click.option("--scope", default="all", type=click.Choice(["current_app", "project", "all"]), show_default=True)
def sessions_search(query: str, limit: int, agent: str | None, app: str | None, since: str | None, scope: str):
    """Search indexed session events."""
    import json as _json
    from src.extensions.self_learning.session_index import SessionIndex

    result = SessionIndex().search(query, limit=limit, agent=agent, app=app, since=since, scope=scope)
    click.echo(_json.dumps(result, ensure_ascii=False, indent=2, default=str))


@sessions.command("scroll")
@click.argument("run_id")
@click.argument("event_id", type=int)
@click.option("--direction", default="after", type=click.Choice(["before", "after"]), show_default=True)
@click.option("--window", default=5, show_default=True, type=int)
def sessions_scroll(run_id: str, event_id: int, direction: str, window: int):
    """Scroll before or after a session event."""
    import json as _json
    from src.extensions.self_learning.session_index import SessionIndex

    result = SessionIndex().scroll(run_id, event_id, direction=direction, window=window)
    click.echo(_json.dumps(result, ensure_ascii=False, indent=2, default=str))


@sessions.command("prune")
@click.option(
    "--retention-days",
    required=True,
    type=click.IntRange(min=0),
    help="Delete history older than this many days; 0 includes all prior history.",
)
def sessions_prune(retention_days: int):
    """Prune old run/event history; curated memory is unaffected."""
    import json as _json

    from src.extensions.self_learning.ledger import SelfLearningLedger

    result = SelfLearningLedger().prune_events(retention_days=retention_days)
    click.echo(_json.dumps(result, ensure_ascii=False, indent=2, default=str))


# ─────────────────────────────────────────────
# loom learn / reviews / feedback
# ─────────────────────────────────────────────

def _review_scope_selection(
    *,
    application_id: str | None,
    project_scope: bool,
    all_scopes: bool,
) -> tuple[str, str]:
    selections = int(bool(application_id)) + int(project_scope) + int(all_scopes)
    if selections != 1:
        raise click.UsageError(
            "Choose exactly one scope: --application, --project, or the command's --all option."
        )
    if application_id:
        return "application", application_id
    if project_scope:
        return "project", "project"
    return "all", ""


def _echo_json(value) -> None:
    import json as _json

    if hasattr(value, "to_dict") and callable(value.to_dict):
        value = value.to_dict()
    click.echo(_json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _review_cli_service():
    root_context = click.get_current_context().find_root()
    if isinstance(root_context.obj, dict) and "review_service" in root_context.obj:
        return root_context.obj["review_service"]
    from src.extensions.self_learning.review_artifacts import ReviewCLIService

    return ReviewCLIService()


def _review_cli_call(operation):
    try:
        return operation()
    except click.ClickException:
        raise
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

@main.group()
def learn():
    """Extract and review self-learning candidates."""


@learn.command("review")
@click.option("--application", "application_id", default=None, help="Review one Application.")
@click.option("--project", "project_scope", is_flag=True, help="Review Project candidates.")
@click.option("--all-unreviewed", is_flag=True, help="Review each Application, then Project.")
@click.option("--dry-run", is_flag=True, help="Render decisions without activating candidates.")
def learn_review_command(
    application_id: str | None,
    project_scope: bool,
    all_unreviewed: bool,
    dry_run: bool,
) -> None:
    """Review exactly one scope, or every unreviewed scope in isolation."""

    selection = _review_scope_selection(
        application_id=application_id,
        project_scope=project_scope,
        all_scopes=all_unreviewed,
    )
    service = _review_cli_call(_review_cli_service)
    if selection[0] == "all":
        result = _review_cli_call(lambda: service.review_all(dry_run=dry_run))
    else:
        result = _review_cli_call(
            lambda: service.review_one(selection[0], selection[1], dry_run=dry_run)
        )
    _echo_json(result)


@main.group()
def reviews():
    """Inspect, apply, or roll back scoped review decisions."""


@reviews.command("status")
@click.option("--application", "application_id", default=None, help="Show one Application.")
@click.option("--project", "project_scope", is_flag=True, help="Show Project status.")
@click.option("--all", "all_scopes", is_flag=True, help="Show all review scopes.")
def reviews_status_command(
    application_id: str | None,
    project_scope: bool,
    all_scopes: bool,
) -> None:
    """Show review state for exactly one scope or for all scopes."""

    scope_type, scope_id = _review_scope_selection(
        application_id=application_id,
        project_scope=project_scope,
        all_scopes=all_scopes,
    )
    service = _review_cli_call(_review_cli_service)
    _echo_json(_review_cli_call(lambda: service.status(scope_type, scope_id)))


@reviews.command("apply")
@click.option("--application", "application_id", default=None, help="Apply one Application INBOX.")
@click.option("--project", "project_scope", is_flag=True, help="Apply the Project INBOX.")
def reviews_apply_command(application_id: str | None, project_scope: bool) -> None:
    """Apply decisions from exactly one scoped INBOX."""

    scope_type, scope_id = _review_scope_selection(
        application_id=application_id,
        project_scope=project_scope,
        all_scopes=False,
    )
    service = _review_cli_call(_review_cli_service)
    _echo_json(_review_cli_call(lambda: service.apply(scope_type, scope_id)))


@reviews.command("rollback")
@click.argument("review_id")
def reviews_rollback_command(review_id: str) -> None:
    """Roll back mutations created by one immutable review batch."""

    service = _review_cli_call(_review_cli_service)
    _echo_json(_review_cli_call(lambda: service.rollback(review_id)))


@main.group()
def feedback():
    """Submit outcome feedback for a completed run."""


@feedback.command("submit")
@click.argument("run_id")
@click.option(
    "--verdict",
    required=True,
    type=click.Choice(["accepted", "rejected", "corrected"]),
)
@click.option(
    "--item",
    "item_id",
    default=None,
    type=click.IntRange(min=1),
    help="Optional affected memory item id.",
)
def feedback_submit_command(run_id: str, verdict: str, item_id: int | None) -> None:
    """Record accepted, rejected, or corrected run feedback."""

    service = _review_cli_call(_review_cli_service)
    _echo_json(
        _review_cli_call(
            lambda: service.submit_feedback(
                run_id=run_id,
                verdict=verdict,
                item_id=item_id,
            )
        )
    )


# ─────────────────────────────────────────────
# loom memory
# ─────────────────────────────────────────────

@main.group()
def memory():
    """Manage durable AgentLoom memory."""


_MEMORY_SCOPES = ["project", "app", "application"]


@memory.command("list")
@click.option("--scope", default=None, type=click.Choice(_MEMORY_SCOPES))
@click.option("--scope-id", default="", help="Application id when scope is app.")
def memory_list(scope: str | None, scope_id: str):
    """List active curated memory."""
    import json as _json
    from src.extensions.self_learning.memory_store import MemoryStore

    result = MemoryStore().list(scope=scope, scope_id=scope_id)
    click.echo(_json.dumps(result, ensure_ascii=False, indent=2, default=str))


@memory.command("add")
@click.option("--scope", default="project", type=click.Choice(_MEMORY_SCOPES), show_default=True)
@click.option("--scope-id", default="", help="Application id when scope is app.")
@click.argument("content")
def memory_add(scope: str, scope_id: str, content: str):
    """Add active memory directly from CLI."""
    import json as _json
    from src.extensions.self_learning.memory_store import MemoryStore

    result = MemoryStore().add(scope, content, scope_id=scope_id)
    click.echo(_json.dumps(result, ensure_ascii=False, indent=2, default=str))


@memory.command("replace")
@click.option("--scope", default="project", type=click.Choice(_MEMORY_SCOPES), show_default=True)
@click.option("--scope-id", default="", help="Application id when scope is app.")
@click.argument("target")
@click.argument("content")
def memory_replace(scope: str, scope_id: str, target: str, content: str):
    """Replace active memory directly from CLI."""
    import json as _json
    from src.extensions.self_learning.memory_store import MemoryStore

    result = MemoryStore().replace(scope, target, content, scope_id=scope_id)
    click.echo(_json.dumps(result, ensure_ascii=False, indent=2, default=str))


@memory.command("remove")
@click.option("--scope", default="project", type=click.Choice(_MEMORY_SCOPES), show_default=True)
@click.option("--scope-id", default="", help="Application id when scope is app.")
@click.argument("target")
def memory_remove(scope: str, scope_id: str, target: str):
    """Remove active memory directly from CLI."""
    import json as _json
    from src.extensions.self_learning.memory_store import MemoryStore

    result = MemoryStore().remove(scope, target, scope_id=scope_id)
    click.echo(_json.dumps(result, ensure_ascii=False, indent=2, default=str))


@memory.command("pending")
@click.option(
    "--status",
    default="pending",
    type=click.Choice(["pending", "approved", "rejected", "stale", "all"]),
    show_default=True,
)
def memory_pending(status: str):
    """List exact writes waiting for approval (or their audit status)."""
    import json as _json
    from src.extensions.self_learning.memory_store import MemoryStore

    result = MemoryStore().list_pending(status=None if status == "all" else status)
    click.echo(_json.dumps(result, ensure_ascii=False, indent=2, default=str))


@memory.command("stats")
def memory_stats():
    """Show active memory capacity and pending-write status."""
    import json as _json
    from src.extensions.self_learning.memory_store import MemoryStore

    result = MemoryStore().stats()
    click.echo(_json.dumps(result, ensure_ascii=False, indent=2, default=str))


@memory.command("export")
@click.option("--out", "out_path", default="", help="Write to this file instead of stdout.")
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "markdown"]), show_default=True)
def memory_export(out_path: str, fmt: str):
    """Export active memory and exact pending-write audit rows."""
    import json as _json
    from datetime import datetime as _datetime

    from src.extensions.self_learning.memory_store import MemoryStore

    store = MemoryStore()
    items = store.export_items()
    stats = store.stats()
    if fmt == "json":
        payload = {
            "exported_at": _datetime.now().astimezone().isoformat(),
            "db_path": str(store.db_path),
            "stats": stats,
            "items": items,
            "pending_writes": store.list_pending(status=None),
        }
        rendered = _json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    else:
        by_bucket: dict = {}
        for item in items:
            by_bucket.setdefault(f"{item['scope_type']}:{item['scope_id']}", []).append(item)
        lines = ["# AgentLoom Memory Export", ""]
        for bucket_key, bucket_items in by_bucket.items():
            lines.append(f"## {bucket_key}")
            lines.append("")
            for item in bucket_items:
                lines.append(f"- [{item['id']}] {item['content']}")
            lines.append("")
        rendered = "\n".join(lines).rstrip() + "\n"
    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        click.echo(f"Exported {len(items)} items to {out_path}")
    else:
        click.echo(rendered)


# ─────────────────────────────────────────────
# loom skills proposals
# ─────────────────────────────────────────────

@main.group()
def skills():
    """Manage skills and skill proposals."""


@skills.group("proposals")
def skill_proposals():
    """Review and promote generated skill proposals."""


@skill_proposals.command("list")
def skill_proposals_list():
    """List generated skill proposals."""
    import json as _json
    from src.extensions.self_learning.proposal_writer import ProposalWriter

    click.echo(_json.dumps(ProposalWriter().list(), ensure_ascii=False, indent=2, default=str))


@skill_proposals.command("show")
@click.argument("proposal_id")
def skill_proposals_show(proposal_id: str):
    """Show a generated skill proposal."""
    import json as _json
    from src.extensions.self_learning.proposal_writer import ProposalWriter

    click.echo(_json.dumps(ProposalWriter().show(proposal_id), ensure_ascii=False, indent=2, default=str))


@skill_proposals.command("promote")
@click.argument("proposal_id")
@click.option("--name", "destination", default="", help="Destination active skill name.")
def skill_proposals_promote(proposal_id: str, destination: str):
    """Promote a proposal with SKILL.md into active skills."""
    import json as _json
    from src.extensions.self_learning.proposal_writer import ProposalWriter

    result = ProposalWriter().promote(proposal_id, destination=destination)
    click.echo(_json.dumps(result, ensure_ascii=False, indent=2, default=str))


@skill_proposals.command("archive")
@click.argument("proposal_id")
def skill_proposals_archive(proposal_id: str):
    """Archive a generated skill proposal."""
    import json as _json
    from src.extensions.self_learning.proposal_writer import ProposalWriter

    result = ProposalWriter().archive(proposal_id)
    click.echo(_json.dumps(result, ensure_ascii=False, indent=2, default=str))


# ─────────────────────────────────────────────
# loom dashboard
# ─────────────────────────────────────────────

@main.command("dashboard")
def dashboard():
    """Launch the terminal TUI task monitoring dashboard (Textual)."""
    from src.ui.dashboard import run_dashboard

    run_dashboard()


_CREATE_EPILOG = """\
\b
Examples:
  loom create applications/test_demo/workflows/test_agent.yaml
  loom create applications/test_demo/workflows/test_agent.yaml -o my_app.py
"""


@main.command(epilog=_CREATE_EPILOG)
@click.argument("yaml_path")
@click.option("-o", "--output", default=None, help="Output file path. Defaults to applications/{category}/{name}_app.py.")
def create(yaml_path: str, output: str | None):
    """Generate a minimal demo script for a supervisor YAML config."""
    from src.scaffold import create_demo_script

    try:
        generated = create_demo_script(yaml_path, output_path=output, interactive=True)
        click.echo(f" Demo script generated: {generated}")
    except FileExistsError as exc:
        click.echo(f"\n  {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"\n Failed: {exc}", err=True)
        sys.exit(1)


# ─────────────────────────────────────────────
# loom ui
# ─────────────────────────────────────────────

_VALID_LOG_EXT = {".json", ".log"}

_UI_EPILOG = """\
\b
Examples:
  loom ui                          # interactive wizard, all defaults
  loom ui --port 9090              # skip port prompt, use 9090

The wizard will guide you through:
  [1/3] Server port
  [2/3] Auto-open browser
  [3/3] Runtime JSON log file (real-time monitoring)

Agent topology (supervisor + workers) is auto-discovered at runtime.
You can also load JSON files from the web UI at any time.
"""


def _resolve_path(raw: str, agent_root):
    """Resolve a user-supplied path (relative to *agent_root* or absolute)."""
    from pathlib import Path as _P
    p = _P(raw)
    if not p.is_absolute():
        p = _P(agent_root) / p
    return p.resolve()


def _prompt_port(default: int = 8080) -> int:
    """Interactive port prompt with retry loop."""
    while True:
        raw = click.prompt(
            click.style("  [1/3] ", fg="cyan") + "Server port",
            default=str(default),
            show_default=False,
            prompt_suffix=f" (default {default}, press Enter to use default): ",
        )
        raw = raw.strip()
        if not raw:
            click.echo(click.style(f"  ✔ Port: {default}", fg="green"))
            return default
        try:
            port = int(raw)
        except ValueError:
            click.echo(click.style("  ⚠ Port must be an integer between 1024~65535, please retry.", fg="yellow"))
            continue
        if not (1024 <= port <= 65535):
            click.echo(click.style("  ⚠ Port must be an integer between 1024~65535, please retry.", fg="yellow"))
            continue
        click.echo(click.style(f"  ✔ Port: {port}", fg="green"))
        return port


def _prompt_browser(default: bool = True) -> bool:
    """Interactive browser prompt with retry loop."""
    while True:
        raw = click.prompt(
            click.style("  [2/3] ", fg="cyan") + "Auto-open browser?",
            default="Y" if default else "N",
            show_default=False,
            prompt_suffix=" (Y/n, press Enter for Y): ",
        )
        raw = raw.strip().lower()
        if raw in ("", "y", "yes"):
            click.echo(click.style("  ✔ Auto-open browser: Yes", fg="green"))
            return True
        if raw in ("n", "no"):
            click.echo(click.style("  ✔ Auto-open browser: No", fg="green"))
            return False
        click.echo(click.style("  ⚠ Please enter Y or N, please retry.", fg="yellow"))


def _prompt_log(agent_root) -> str | None:
    """Interactive JSON log prompt with retry loop."""
    click.echo(click.style("  [3/3] ", fg="cyan") + "Runtime JSON log file " + click.style("(press Enter to skip)", dim=True) + ":")
    click.echo("         Monitor a JSON log file for real-time agent execution visualization.")
    click.echo("         Supports relative path (from AgentLoom root) or absolute path.")
    while True:
        raw = click.prompt("       ", default="", show_default=False, prompt_suffix="> ").strip()
        if not raw:
            click.echo(click.style("  ✔ Log: (skipped)", fg="green"))
            return None
        from pathlib import Path as _P
        if _P(raw).suffix.lower() not in _VALID_LOG_EXT:
            click.echo(click.style(f"  ⚠ Only .json/.log files are supported, please retry (Enter to skip).", fg="yellow"))
            continue
        resolved = _resolve_path(raw, agent_root)
        if not resolved.exists():
            # JSON log may not exist yet — just warn, allow it
            click.echo(click.style(f"  ⚠ File does not exist yet (will wait for it): {resolved}", fg="yellow"))
        display = raw if len(raw) < 80 else str(resolved)
        click.echo(click.style(f"  ✔ Log: {display}", fg="green"))
        return str(resolved)


@main.command(epilog=_UI_EPILOG)
@click.option("-p", "--port", type=int, default=None, help="Server port (skip interactive prompt).")
@click.option("--no-browser", is_flag=True, default=False, help="Don't auto-open browser (skip interactive prompt).")
def ui(port: int | None, no_browser: bool):
    """Launch the agent visualisation web UI (interactive setup wizard)."""
    from pathlib import Path as _P

    # Determine agent_root
    try:
        from src.lib.config import C
        agent_root = str(C.agent_root)
    except Exception:
        agent_root = str(_P.cwd())

    # Banner
    click.echo()
    click.echo("  ╔══════════════════════════════════════════════╗")
    click.echo("  ║       AgentLoom Visualization Setup Wizard       ║")
    click.echo("  ╚══════════════════════════════════════════════╝")
    click.echo()

    # [1/3] Port
    if port is not None:
        if not (1024 <= port <= 65535):
            click.echo(click.style(f"  ⚠ Invalid --port {port}. Must be 1024~65535.", fg="red"), err=True)
            sys.exit(1)
        click.echo(click.style(f"  [1/3] ", fg="cyan") + f"Server port: {port} (from --port)")
        chosen_port = port
    else:
        chosen_port = _prompt_port()

    # [2/3] Browser
    if no_browser:
        click.echo(click.style(f"  [2/3] ", fg="cyan") + "Auto-open browser: No (from --no-browser)")
        auto_browser = False
    else:
        auto_browser = _prompt_browser()

    # [3/3] Log
    log_file = _prompt_log(agent_root)

    # Summary
    click.echo()
    click.echo("  ──────────────────────────────────────")
    click.echo("    Configuration:")
    click.echo(f"      Port:     {chosen_port}")
    click.echo(f"      Browser:  {'Auto-open' if auto_browser else 'Disabled'}")
    click.echo(f"      Log:      {log_file or '(none)'}")
    click.echo("  ──────────────────────────────────────")
    click.echo()
    click.echo("  Starting server...")
    click.echo()

    # Start server
    from src.ui.server import start_server

    try:
        start_server(
            port=chosen_port,
            auto_browser=auto_browser,
            log_file=log_file,
            agent_root=agent_root,
        )
    except Exception as exc:
        click.echo(f"\n  Server failed: {exc}", err=True)
        sys.exit(1)


# Keep the durable scheduler in its own lightweight package so TUI and CLI
# share one backend without importing the Agent/model runtime for list/status.
main.add_command(_schedules_command)


if __name__ == "__main__":
    main()
