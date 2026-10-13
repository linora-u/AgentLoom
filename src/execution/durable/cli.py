"""Dedicated public durable consumer, separate from producer Application Runs."""
import json
from pathlib import Path
import signal
from threading import Event

import click


@click.group()
def durable():
    """Serve persistent Worker queues through the upstream execution engine."""


@durable.command()
@click.argument("application", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--worker", multiple=True, required=True, help="Trusted Application-relative Worker YAML.")
@click.option("--project-root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--root", type=click.Path(file_okay=False, path_type=Path), help="Stable durable namespace root.")
@click.option("--database-url", help="Explicit DBOS storage URL; defaults to a local SQLite file.")
@click.option("--host", type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Trusted HostSpec JSON file.")
@click.option("--idle-timeout", type=click.FloatRange(min=0, min_open=True))
def serve(application, worker, project_root, root, database_url, host, idle_timeout):
    """Run one local consumer until signalled; other CLIs can submit tasks."""
    from agentloom.app import Application
    from .host import HostSpec

    stop = Event()
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, lambda *_: stop.set())
    try:
        spec = HostSpec(**json.loads(host.read_text())) if host else None
        with Application.open(application, project_root=project_root) as app, app.durable(root=root, database_url=database_url) as execution:
            execution.executor(worker, host=spec).run(stop_token=stop, idle_timeout=idle_timeout)
    except Exception as error:
        raise click.ClickException(str(error)) from error
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
