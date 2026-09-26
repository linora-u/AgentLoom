#!/usr/bin/env python3
"""Entry script for the zsxq owner-post scraper Agent.

Drives the user's already-open Chrome through browser-harness, scrolls the
target zsxq group from newest to `since_date`, and writes the result to CSV.

Usage:
    .venv/bin/python applications/browser_harness_probe/zsxq_scraper_app.py

The Agent reads `applications/browser_harness_probe/workflows/zsxq_scraper_agent.yaml`,
which pins the group URL, since_date 2024-01-01, and the CSV output path.
"""
from __future__ import annotations

import os
import sys

import fire

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from agentloom.app.runner import run_app  # noqa: E402 - project root is added above


def main(
    file_logging: bool | None = None,
    resume: str | None = None,
) -> str:
    """Run the zsxq owner-post scraper Agent."""

    result = run_app(
        "applications/browser_harness_probe/workflows/zsxq_scraper_agent.yaml",
        file_logging=file_logging,
        resume_task_id=resume,
    )
    print(result)
    return result


if __name__ == "__main__":
    fire.Fire(main)
