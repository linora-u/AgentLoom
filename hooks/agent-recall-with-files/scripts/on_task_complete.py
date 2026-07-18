#!/usr/bin/env python3
"""Hook: TaskCompleted — request a final completion record."""

from common import (
    CONTEXT_FILE,
    HOOK_TAG,
    TRACE_FILE,
    get_runtime_agent_path,
    get_task_id,
    output,
    runtime_dir,
)


def main() -> None:
    agent = get_runtime_agent_path()
    rd = runtime_dir(agent)
    task_id = get_task_id() or "current-task"

    output({
        "decision": "allow",
        "user_message": (
            f"{HOOK_TAG} Task '{task_id}' completed. "
            f"Finalize {rd / TRACE_FILE} and {rd / CONTEXT_FILE}."
        ),
        "telemetry": {"runtime_dir": str(rd)},
    })


if __name__ == "__main__":
    main()
