#!/usr/bin/env python3
"""Hook: TaskCompleted — request a final completion record."""

from common import (
    CONTEXT_FILE,
    HOOK_TAG,
    TRACE_FILE,
    get_task_id,
    output,
    task_workspace_dir,
)


def main() -> None:
    workspace = task_workspace_dir()
    task_id = get_task_id() or "current-task"

    output({
        "decision": "allow",
        "user_message": (
            f"{HOOK_TAG} Task '{task_id}' completed. "
            f"Finalize {workspace / TRACE_FILE} and {workspace / CONTEXT_FILE}."
        ),
        "telemetry": {"task_workspace": str(workspace)},
    })


if __name__ == "__main__":
    main()
