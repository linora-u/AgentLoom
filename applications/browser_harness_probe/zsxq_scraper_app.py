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

from src.runner import run_app


DEFAULT_TASK = (
    "Scrape the group-owner posts of the zsxq group "
    "https://wx.zsxq.com/group/51111541884844 from the newest post back to "
    "2024-01-01. Reuse the Chrome tab the user already has open. Drop comments. "
    "Save CSV with columns 时间/内容/超链接 to the project root."
)


def main(
    user_request: str = DEFAULT_TASK,
    file_logging: bool | None = None,
    resume: str | None = None,
) -> str:
    """Run the zsxq owner-post scraper Agent."""

    request = (user_request or "").strip() or DEFAULT_TASK
    task = (
        "User request:\n"
        f"{request}\n\n"
        "Follow the workflow exactly. Call browser_harness_doctor once, then "
        "scrape_zsxq_owner_posts once with no overrides."
    )
    result = run_app(
        "applications/browser_harness_probe/workflows/zsxq_scraper_agent.yaml",
        task_override=task,
        file_logging=file_logging,
        resume_task_id=resume,
    )
    print(result)
    return result


if __name__ == "__main__":
    fire.Fire(main)
