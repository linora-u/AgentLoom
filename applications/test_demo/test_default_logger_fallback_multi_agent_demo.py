#!/usr/bin/env python3
"""Default logger fallback demo with supervisor + worker agents."""

from __future__ import annotations

import os
import sys
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.lib.logging import get_global_logger, resolve_logger
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory, YamlConfiguredSupervisorAgent


DEFAULT_YAML_PATH = Path(__file__).parent / "workflows" / "test_default_logger_fallback_multi_supervisor.yaml"
DEFAULT_TASK_CONTENT = (
    "请按 workflow 执行：先由主 agent 执行一次 shell_tool，再调度子 agent 执行一次 shell_tool，"
    "最后返回两边的执行结果。"
)


def _extract_log_file_path() -> Path | None:
    backend = get_global_logger(create_if_missing=False)
    console = getattr(backend, "console", None)
    log_file_path = getattr(console, "log_file_path", None)
    return Path(log_file_path) if log_file_path else None


def run_demo(task_content: str, yaml_path: Path) -> None:
    if not yaml_path.exists():
        raise FileNotFoundError(f"YAML not found: {yaml_path}")

    resolve_logger(None, __name__).info("default logger fallback multi-agent demo start")

    config = YamlAgentFactory._load_config_from_file(yaml_path)
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
