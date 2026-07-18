#!/usr/bin/env python3
"""Hook: TaskCompleted — remind agent to record a completion note."""

from common import (
    CONTEXT_FILE,
    SKILL_TAG,
    TRACE_FILE,
    get_tool_input,
    output,
    task_workspace_dir,
)


def main() -> None:
    workspace = task_workspace_dir()
    ti = get_tool_input()
    task_id = (ti.get("task_id") or "") or "current-task"

    output({
        "decision": "allow",
        "user_message": (
            f"{SKILL_TAG} Task '{task_id}' completed. "
            f"Finalize {workspace / TRACE_FILE} and {workspace / CONTEXT_FILE}."
        ),
        "telemetry": {"task_workspace": str(workspace)},
    })


if __name__ == "__main__":
    main()
