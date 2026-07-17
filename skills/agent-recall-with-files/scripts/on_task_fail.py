#!/usr/bin/env python3
"""Hook: StopFailure — remind agent to record the failure."""

from common import (
    SKILL_TAG,
    TRACE_FILE,
    get_tool_input,
    output,
    persistent_insights_path,
    task_workspace_dir,
)


def main() -> None:
    workspace = task_workspace_dir()
    ti = get_tool_input()
    task_id = (ti.get("task_id") or "") or "current-task"
    error = (ti.get("error") or "") or "task failed"

    output({
        "decision": "allow",
        "user_message": (
            f"{SKILL_TAG} Task '{task_id}' failed. "
            f"Record the error and next step in {workspace / TRACE_FILE}. "
            f"Log the root cause as a [pitfall] in {persistent_insights_path()} — "
            f"it persists across sessions so future runs avoid the same issue. "
            f"Error: {error}."
        ),
        "telemetry": {"task_workspace": str(workspace)},
    })


if __name__ == "__main__":
    main()
