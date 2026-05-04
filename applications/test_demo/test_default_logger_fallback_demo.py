#!/usr/bin/env python3
"""Minimal demo: only task_content + yaml config, then run."""

from __future__ import annotations

import os
import sys
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.lib.logging import get_global_logger, initialize_global_logger_once, resolve_logger
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory, YamlConfiguredSupervisorAgent


DEFAULT_YAML_PATH = Path(__file__).parent / "workflows" / "test_agent.yaml"
DEFAULT_TASK_CONTENT = "请用两句话介绍你自己，并说明当前是否使用了默认 logger。"


def _extract_log_file_path() -> Path | None:
    backend = get_global_logger(create_if_missing=False)
    console = getattr(backend, "console", None)
    log_file_path = getattr(console, "log_file_path", None)
    return Path(log_file_path) if log_file_path else None


def run_demo(task_content: str, yaml_path: Path) -> None:

    if not yaml_path.exists():
        raise FileNotFoundError(f"YAML not found: {yaml_path}")

    config = YamlAgentFactory._load_config_from_file(yaml_path)
    initialize_global_logger_once(config["name"])
    resolve_logger(None, __name__).info("default logger fallback demo start")

    supervisor = YamlConfiguredSupervisorAgent(config=config, logger=None)

    log_path = _extract_log_file_path()
    print(f"default logger fallback active: {get_global_logger(create_if_missing=False) is not None}")
    print(f"log file path: {log_path}")

    if os.getenv("AGENT_LOOM_DEMO_NO_RUN") == "1":
        print("skip run: true")
        return

    print("skip run: false")
    result = supervisor.run(task_content)
    print("run result:")
    print(result)


if __name__ == "__main__":
    run_demo(task_content=DEFAULT_TASK_CONTENT, yaml_path=DEFAULT_YAML_PATH.resolve())
