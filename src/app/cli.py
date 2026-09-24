"""Application-owned CLI commands."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext, redirect_stdout
from datetime import UTC, datetime
from typing import Any, TextIO

import click

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
    for field in ("output", "error", "phase", "goal"):
        value = getattr(event, field, None)
        if field == "output" and event.event == "run.completed":
            payload[field] = value
        elif value is not None:
            payload[field] = dict(value) if field == "goal" else value
    return payload


def _goal_text(goal: Mapping[str, object]) -> str:
    return f"Goal: {goal.get('status')}"


def _emit_jsonl_record(payload: dict[str, object], stream: TextIO) -> None:
    click.echo(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        file=stream,
    )
    stream.flush()


@contextmanager
def _isolated_machine_stdout() -> Iterator[TextIO]:
    """Reserve fd 1 for JSONL and route incidental output to stderr."""

    protocol_stream = click.get_text_stream("stdout")
    try:
        protocol_fd = protocol_stream.fileno()
    except (AttributeError, OSError, ValueError):
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


def _has_transient_provider_error(error: BaseException) -> bool:
    """Classify trusted provider failures without loading a provider SDK."""

    from agentloom.execution.agent_runtime import AgentRuntimeError
    from agentloom.execution.model_protocol import ModelProtocolError

    litellm_errors = sys.modules.get("litellm.exceptions")
    transient_types = (
        ()
        if litellm_errors is None
        else (
            litellm_errors.Timeout,
            litellm_errors.APIConnectionError,
            litellm_errors.InternalServerError,
            litellm_errors.ServiceUnavailableError,
            litellm_errors.RateLimitError,
        )
    )
    denied_types: tuple[type[BaseException], ...] = (
        SystemExit,
        KeyboardInterrupt,
        click.ClickException,
        click.exceptions.Exit,
        ModelProtocolError,
    )
    if litellm_errors is not None:
        denied_types += (
            litellm_errors.AuthenticationError,
            litellm_errors.PermissionDeniedError,
            litellm_errors.BadRequestError,
        )
    retry_module = sys.modules.get("agentloom.integrations.litellm.litellm_retry")
    if retry_module is not None:
        denied_types += (retry_module.ProviderCallBudgetExceeded,)
    smol = sys.modules.get("smolagents")
    if smol is not None:
        denied_types += (smol.AgentParsingError, smol.AgentMaxStepsError)

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
        if isinstance(current, AgentRuntimeError):
            if current.category != "provider" or not current.retryable:
                return False
            transient_seen = True
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
  loom run applications/test_demo/workflows/test_agent.yaml --output-format json
  loom run applications/test_demo/workflows/test_agent.yaml --output-format jsonl
"""


@click.command("run", epilog=_RUN_EPILOG)
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
    type=click.Choice(("text", "json", "jsonl"), case_sensitive=False),
    default="text",
    show_default=True,
    help="Choose human-readable output, one JSON event, or lifecycle JSON Lines.",
)
@click.option(
    "--require-valid-supervisor-target",
    is_flag=True,
    hidden=True,
)
def run(
    yaml_path: str,
    no_file_log: bool,
    resume_task_id: str | None,
    task_override: str | None,
    output_format: str,
    require_valid_supervisor_target: bool,
) -> None:
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
            _run_rejected_payload(error, message=message, retryable=retryable),
            event_stream,
        )

    machine_output = output_format in {"json", "jsonl"}
    output_context = _isolated_machine_stdout() if machine_output else nullcontext(None)
    with output_context as active_event_stream:
        event_stream = active_event_stream
        try:
            from agentloom.app.runner import execute_app

            if machine_output:

                def emit_event(event: Any) -> None:
                    assert event_stream is not None
                    if output_format == "jsonl" or event.event != "run.started":
                        _emit_jsonl_record(_run_event_payload(event), event_stream)
                    emitted_events.add(event.event)

                execute_app(
                    yaml_path,
                    file_logging=False if no_file_log else None,
                    resume_task_id=resume_task_id,
                    task_override=task_override,
                    event_sink=emit_event,
                    require_valid_supervisor_target=require_valid_supervisor_target,
                )
            else:
                completed = execute_app(
                    yaml_path,
                    file_logging=False if no_file_log else None,
                    resume_task_id=resume_task_id,
                    task_override=task_override,
                    require_valid_supervisor_target=require_valid_supervisor_target,
                )
                rendered_output = (
                    completed.output
                    if isinstance(completed.output, str)
                    else json.dumps(completed.output, ensure_ascii=False, indent=2)
                )
                if not getattr(completed, "final_answer_presented", False):
                    click.echo(rendered_output)
                completed_goal = getattr(completed, "goal", None)
                if isinstance(completed_goal, Mapping):
                    click.echo(_goal_text(completed_goal))
        except KeyboardInterrupt as exc:
            if not emitted_events:
                emit_rejected(exc, message="interrupted before run started")
            from agentloom.app.run import ApplicationRunInterrupted

            if isinstance(exc, ApplicationRunInterrupted) and exc.resumable:
                click.echo("\nInterrupted. Use --resume to continue.", err=True)
            else:
                click.echo(
                    "\nInterrupted; no resumable checkpoint is available.",
                    err=True,
                )
            raise click.exceptions.Exit(130) from exc
        except Exception as exc:
            retryable = _has_transient_provider_error(exc)
            if not emitted_events:
                emit_rejected(exc, retryable=retryable)
            click.echo(f"\n Execution failed: {exc}", err=True)
            raise click.exceptions.Exit(_EX_TEMPFAIL if retryable else 1) from exc
        except SystemExit as exc:
            emit_rejected(exc, message="nested process exit")
            click.echo("\n Execution failed: nested process exit", err=True)
            raise click.exceptions.Exit(1) from exc


_CREATE_EPILOG = """\
\b
Examples:
  loom create applications/test_demo/workflows/test_agent.yaml
  loom create applications/test_demo/workflows/test_agent.yaml -o my_app.py
"""


@click.command("create", epilog=_CREATE_EPILOG)
@click.argument("yaml_path")
@click.option(
    "-o",
    "--output",
    default=None,
    help="Output file path. Defaults to applications/{category}/{name}_app.py.",
)
def create(yaml_path: str, output: str | None) -> None:
    """Generate a minimal demo script for a supervisor YAML config."""

    from agentloom.app.scaffold import create_demo_script

    try:
        generated = create_demo_script(
            yaml_path,
            output_path=output,
            interactive=True,
        )
        click.echo(f" Demo script generated: {generated}")
    except FileExistsError as exc:
        click.echo(f"\n  {exc}", err=True)
        raise click.exceptions.Exit(1) from exc
    except Exception as exc:
        click.echo(f"\n Failed: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc
