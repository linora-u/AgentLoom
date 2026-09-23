"""AgentLoom command-line composition root."""

from __future__ import annotations

import os
from pathlib import Path

import click
from agentloom.app.cli import create, run
from agentloom.app.composition import build_schedule_mutations
from agentloom.execution.cli import (
    clean_runtime_command,
    clean_tasks,
    list_tasks,
)
from agentloom.runtimes.pi.cli import runtime
from agentloom.schedules.cli import (
    SCHEDULE_CLI_DEPENDENCIES_KEY,
    ScheduleCliDependencies,
    schedules,
)
from agentloom.self_learning.cli import (
    feedback,
    learn,
    memory,
    reviews,
    sessions,
    skills,
)

_MAIN_EPILOG = """\
\b
Examples:
  loom run applications/<app>/workflows/<agent>.yaml
  loom create applications/<app>/workflows/<agent>.yaml
  loom create applications/<app>/workflows/<agent>.yaml -o my_app.py
  loom schedules add applications/<app>/workflows/<agent>.yaml --every 1h
  loom schedules serve
  loom runtime install pi

Use 'loom <command> -h' for more details on each command.
"""


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    epilog=_MAIN_EPILOG,
)
@click.pass_context
def main(context: click.Context) -> None:
    """AgentLoom – AI agent framework CLI."""

    context.meta.setdefault(
        SCHEDULE_CLI_DEPENDENCIES_KEY,
        ScheduleCliDependencies(
            build_mutations=build_schedule_mutations,
        ),
    )
    try:
        from agentloom.config import C

        agent_root = Path(C.agent_root).resolve()
        if Path.cwd().resolve() != agent_root:
            os.chdir(agent_root)
    except Exception:
        pass


for command in (
    run,
    create,
    schedules,
    runtime,
    list_tasks,
    clean_tasks,
    clean_runtime_command,
    sessions,
    learn,
    reviews,
    feedback,
    memory,
    skills,
):
    main.add_command(command)


if __name__ == "__main__":
    main()
