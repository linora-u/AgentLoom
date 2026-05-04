#!/usr/bin/env python3
"""
Unit Test Studio demo entrypoint.

Runs the supervisor workflow and returns an English report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import fire

# Ensure project root is importable.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.trace import generate_id
from src.workflows.workflow_manager import get_supervisor_agent_yaml_path
from src.lib.logging import initialize_global_logger_once, resolve_logger
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory, YamlConfiguredSupervisorAgent


def run_unit_test_studio(
    target_path: str,
    targets: str,
    output_dir: str = "test/generated",
) -> str:
    """
    Run the Unit Test Studio supervisor workflow.

    Args:
        target_path: Root path used to resolve relative module paths in targets.
        targets: Comma-separated function targets, e.g. `src/a.py:foo,src/a.py:bar`.
        output_dir: Output directory under target_path where generated tests are written.
    """

    target_root = Path(target_path).resolve()
    if not target_root.exists() or not target_root.is_dir():
        raise ValueError(f"target_path must be an existing directory: {target_root}")

    yaml_path = get_supervisor_agent_yaml_path("unit_test_studio") / "unit_test_studio_agent.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(f"Supervisor YAML not found: {yaml_path}")

    config = YamlAgentFactory._load_config_from_file(yaml_path)
    logger = initialize_global_logger_once(config["name"])
    log = resolve_logger(logger, __name__)

    supervisor = YamlConfiguredSupervisorAgent(config=config, logger=logger)

    payload = {
        "target_root": str(target_root),
        "targets": targets,
        "output_dir": output_dir,
    }

    task_content = (
        "Generate Python pytest tests using Unit Test Studio.\n"
        "Use this JSON payload exactly:\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )
    task_id = generate_id(task_content, prefix="task")
    log.info("Running Unit Test Studio task_id=%s payload=%s", task_id, payload)

    result = supervisor.run(task_content, task_id=task_id)
    report = "" if result is None else str(result)
    log.info("Unit Test Studio completed. report_length=%d", len(report))
    return report


def cli_run(
    target_path: str,
    targets: str,
    output_dir: str = "test/generated",
):
    """
    CLI wrapper for Unit Test Studio demo.
    """
    report = run_unit_test_studio(
        target_path=target_path,
        targets=targets,
        output_dir=output_dir,
    )
    print(report)


if __name__ == "__main__":
    fire.Fire(cli_run)
