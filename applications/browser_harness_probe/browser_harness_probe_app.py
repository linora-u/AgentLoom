#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

import fire

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.runner import run_app


def main(
    user_request: str = "Run browser-harness doctor, then verify both isolated Chrome and real Chrome demo probes.",
    log_to_file: bool = False,
    resume: str | None = None,
) -> str:
    """Run the browser-harness AgentLoom probe application."""

    request = user_request.strip()
    if not request:
        raise ValueError("user_request must be non-empty")

    task = (
        "User request:\n"
        f"{request}\n\n"
        "Follow the workflow exactly. For the default demo, use the fixed demo probe tools."
    )
    result = run_app(
        "applications/browser_harness_probe/workflows/browser_harness_probe_agent.yaml",
        task_override=task,
        log_to_file=log_to_file,
        resume_task_id=resume,
    )
    print(result)
    return result


if __name__ == "__main__":
    fire.Fire(main)
