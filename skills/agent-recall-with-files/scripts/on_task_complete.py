#!/usr/bin/env python3
"""Hook: TaskCompleted — remind agent to record a completion note."""

from common import (
    CONTEXT_FILE, TRACE_FILE, SKILL_TAG,
    runtime_dir, get_runtime_agent_path, get_tool_input, output,
)


def main() -> None:
    agent = get_runtime_agent_path()
    rd = runtime_dir(agent)
    ti = get_tool_input()
    task_id = (ti.get("task_id") or "") or "current-task"

    output({
        "decision": "allow",
        "user_message": (
            f"{SKILL_TAG} Task '{task_id}' completed. "
            f"Finalize {rd / TRACE_FILE} and {rd / CONTEXT_FILE}."
        ),
        "telemetry": {"runtime_dir": str(rd)},
    })


if __name__ == "__main__":
    main()
