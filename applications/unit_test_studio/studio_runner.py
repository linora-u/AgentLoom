#!/usr/bin/env python3
"""Run the Unit Test Studio task declared in its Agent YAML."""

from __future__ import annotations

import sys
from pathlib import Path

import fire

# Ensure project root is importable.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agentloom.app.runner import run_app  # noqa: E402 - project root is added above
from agentloom.app.workflows import get_supervisor_agent_yaml_path  # noqa: E402
from agentloom.execution.agent_runtime import JSONValue  # noqa: E402


def run_unit_test_studio(
    file_logging: bool | None = None,
    resume: str | None = None,
) -> JSONValue:
    """Use the YAML task and return the Agent's actual final reply."""
    yaml_path = get_supervisor_agent_yaml_path("unit_test_studio") / "unit_test_studio_agent.yaml"
    return run_app(
        str(yaml_path),
        file_logging=file_logging,
        resume_task_id=resume,
    )


def cli_run(
    file_logging: bool | None = None,
    resume: str | None = None,
):
    """CLI wrapper for the configured Unit Test Studio task."""
    report = run_unit_test_studio(file_logging=file_logging, resume=resume)
    if report is not None:
        print(report)


if __name__ == "__main__":
    fire.Fire(cli_run)
