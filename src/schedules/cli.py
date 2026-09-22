"""Click commands for the shared AgentLoom schedule backend."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from .mutations import ScheduleMutationService
from .service import ScheduleServerAlreadyRunning, ScheduleService
from .store import ScheduleStore, ScheduleStoreError

SCHEDULE_CLI_DEPENDENCIES_KEY = "agentloom.schedule_cli_dependencies"


@dataclass(frozen=True)
class ScheduleCliDependencies:
    """Application-owned functions injected by the root CLI."""

    build_mutations: Callable[[Path], ScheduleMutationService]


@dataclass
class _ScheduleCommandContext:
    project_root: Path
    dependencies: ScheduleCliDependencies
    service: ScheduleService | None = None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _schedule_context(context: click.Context) -> _ScheduleCommandContext:
    command_context = context.find_object(_ScheduleCommandContext)
    if command_context is None:
        raise click.ClickException("Schedule command context is unavailable")
    return command_context


def _context_service(context: click.Context) -> ScheduleService:
    command_context = _schedule_context(context)
    if command_context.service is None:
        store = ScheduleStore(command_context.project_root)
        context.call_on_close(store.close)
        command_context.service = ScheduleService(store)
    return command_context.service


def _context_mutations(context: click.Context) -> ScheduleMutationService:
    command_context = _schedule_context(context)
    return command_context.dependencies.build_mutations(command_context.project_root)


def _emit_job(job: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        click.echo(_json(job))
    else:
        click.echo(f"{job['id']}  {job['state']}  {job.get('next_run_at') or '-'}  {job['name']}  {job['yaml_path']}")


@click.group("schedules")
@click.option(
    "--project",
    "project_root",
    type=click.Path(path_type=Path, file_okay=False, resolve_path=True),
    default=None,
    help="AgentLoom project root (defaults to the current project).",
)
@click.pass_context
def schedules(context: click.Context, project_root: Path | None) -> None:
    """Manage durable project-level Agent schedules."""
    dependencies = context.find_root().meta.get(SCHEDULE_CLI_DEPENDENCIES_KEY)
    if not isinstance(dependencies, ScheduleCliDependencies):
        raise click.ClickException("Schedule CLI dependencies are unavailable")
    context.obj = _ScheduleCommandContext(
        project_root=(project_root or Path.cwd()).expanduser().resolve(),
        dependencies=dependencies,
    )


@schedules.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
@click.option("--history", is_flag=True, help="List execution-ledger rows instead of jobs.")
@click.option("--job", "job_id", default=None, help="Filter --history to one job ID.")
@click.pass_context
def schedules_list(
    context: click.Context,
    as_json: bool,
    history: bool,
    job_id: str | None,
) -> None:
    """List schedule jobs or their durable execution history."""
    store = _context_service(context).store
    if history:
        executions = store.list_executions(job_id=job_id)
        if as_json:
            click.echo(_json(executions))
            return
        if not executions:
            click.echo("No schedule executions found.")
            return
        for execution in executions:
            click.echo(
                f"{execution['id']}  {execution['status']}  exit={execution.get('exit_code')}  "
                f"{execution['job_name']}  {execution.get('finished_at') or execution['claimed_at']}  "
                f"error={execution.get('error') or '-'}"
            )
        return
    if job_id is not None:
        raise click.UsageError("--job requires --history")
    jobs = store.list_jobs()
    if as_json:
        click.echo(_json(jobs))
        return
    if not jobs:
        click.echo("No schedule jobs found.")
        return
    for job in jobs:
        _emit_job(job, as_json=False)


@schedules.command("add")
@click.argument("yaml_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--name", default="", help="Display name (defaults to YAML filename).")
@click.option("--at", "run_at", default=None, help="Run once at an ISO timestamp.")
@click.option("--every", default=None, help="Repeat at a duration such as 15m or 2h.")
@click.option("--cron", "cron_expression", default=None, help="Repeat with a five-field cron expression.")
@click.option("--timezone", default="UTC", show_default=True, help="IANA timezone for wall-clock input.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
@click.pass_context
def schedules_add(
    context: click.Context,
    yaml_path: Path,
    name: str,
    run_at: str | None,
    every: str | None,
    cron_expression: str | None,
    timezone: str,
    as_json: bool,
) -> None:
    """Add a once, interval, or cron Agent execution."""
    choices = [run_at is not None, every is not None, cron_expression is not None]
    if sum(choices) != 1:
        raise click.UsageError("Choose exactly one of --at, --every, or --cron")
    schedule: dict[str, str]
    if run_at is not None:
        schedule = {"kind": "once", "at": run_at, "timezone": timezone}
    elif every is not None:
        schedule = {"kind": "interval", "every": every, "timezone": timezone}
    else:
        schedule = {
            "kind": "cron",
            "expression": str(cron_expression),
            "timezone": timezone,
        }
    try:
        job = _context_mutations(context).add(
            name=name or yaml_path.stem,
            yaml_path=yaml_path,
            schedule=schedule,
        )
    except (ValueError, ScheduleStoreError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_job(job, as_json=as_json)


def _job_mutation(command_name: str):
    def decorator(function):
        function = click.argument("job_id")(function)
        function = click.option("--json", "as_json", is_flag=True)(function)
        function = click.pass_context(function)
        return schedules.command(command_name)(function)

    return decorator


@_job_mutation("remove")
def schedules_remove(context: click.Context, job_id: str, as_json: bool) -> None:
    """Remove a job while retaining its execution ledger."""
    try:
        job = _context_mutations(context).mutate(
            "remove",
            job_id=job_id,
        )
    except (ValueError, ScheduleStoreError) as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(_json(job))
    else:
        click.echo(f"Removed {job['id']} ({job['name']}).")


@_job_mutation("pause")
def schedules_pause(context: click.Context, job_id: str, as_json: bool) -> None:
    """Pause scheduled firing; manual run remains available."""
    try:
        job = _context_mutations(context).mutate(
            "pause",
            job_id=job_id,
        )
    except (ValueError, ScheduleStoreError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_job(job, as_json=as_json)


@_job_mutation("resume")
def schedules_resume(context: click.Context, job_id: str, as_json: bool) -> None:
    """Resume a paused job and compute its next future fire."""
    try:
        job = _context_mutations(context).mutate(
            "resume",
            job_id=job_id,
        )
    except (ValueError, ScheduleStoreError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_job(job, as_json=as_json)


@schedules.command("run")
@click.argument("job_id")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
@click.pass_context
def schedules_run(context: click.Context, job_id: str, as_json: bool) -> None:
    """Run a job now without consuming its next scheduled fire."""
    try:
        execution = _context_service(context).run_now(job_id)
    except ScheduleStoreError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(_json(execution))
    else:
        click.echo(
            f"{execution['id']}  {execution['status']}  exit={execution.get('exit_code')}  "
            f"stdout={execution.get('stdout_path')}  stderr={execution.get('stderr_path')}"
        )
    if execution["status"] != "succeeded":
        raise click.exceptions.Exit(1)


@schedules.command("serve")
@click.option("--tick-seconds", default=1.0, show_default=True, type=click.FloatRange(min=0.1))
@click.option("--once", "run_once", is_flag=True, help="Run one tick and exit (diagnostics/system timers).")
@click.pass_context
def schedules_serve(context: click.Context, tick_seconds: float, run_once: bool) -> None:
    """Serve a persistent foreground ticker until SIGINT or SIGTERM."""
    service = _context_service(context)
    click.echo(f"Serving schedules from {service.store.jobs_path} (tick={tick_seconds}s).", err=True)
    try:
        service.serve(tick_seconds=tick_seconds, max_ticks=1 if run_once else None)
    except ScheduleServerAlreadyRunning as exc:
        raise click.ClickException(str(exc)) from exc


@schedules.command("status")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
@click.pass_context
def schedules_status(context: click.Context, as_json: bool) -> None:
    """Show ticker health, due jobs, claims, and execution count."""
    status = _context_service(context).status()
    if as_json:
        click.echo(_json(status))
    else:
        click.echo(
            f"{status['state']}  jobs={status['job_count']}  due={status['due_count']}  "
            f"running={status['claimed_count']}  executions={status['execution_count']}"
        )
