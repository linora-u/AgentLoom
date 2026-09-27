#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

import fire

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from agentloom.app.runner import run_app


def main(
    file_logging: bool | None = None,
    resume: str | None = None,
) -> str:
    """Run the browser-harness task configured in its Agent YAML."""
    result = run_app(
        "applications/browser_harness_probe/workflows/browser_harness_probe_agent.yaml",
        file_logging=file_logging,
        resume_task_id=resume,
    )
    print(result)
    return result


if __name__ == "__main__":
    fire.Fire(main)
