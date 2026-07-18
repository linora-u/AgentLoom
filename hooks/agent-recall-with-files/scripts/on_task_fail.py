#!/usr/bin/env python3
"""Hook: StopFailure — request a durable failure record."""

from common import (
    HOOK_TAG,
    INSIGHTS_FILE,
    TRACE_FILE,
    get_runtime_agent_path,
    get_task_id,
    get_tool_input,
    output,
    runtime_dir,
)


def main() -> None:
    agent = get_runtime_agent_path()
    rd = runtime_dir(agent)
    ti = get_tool_input()
    task_id = get_task_id() or "current-task"
    error = (ti.get("error") or "") or "task failed"

    output({
        "decision": "allow",
        "user_message": (
            f"{HOOK_TAG} Task '{task_id}' failed. "
            f"Record the error and next step in {rd / TRACE_FILE}. "
            f"Log the root cause as a [pitfall] in {rd / INSIGHTS_FILE} — "
            f"it persists across sessions so future runs avoid the same issue. "
            f"Error: {error}."
        ),
        "telemetry": {"runtime_dir": str(rd)},
    })


if __name__ == "__main__":
    main()
