#!/usr/bin/env python3
"""Hook: SubagentStop — Worker completed, supervisor resumes reviewing."""

import sys

from common import (
    SKILL_TAG,
    read_viz_state, append_event, find_supervisor_name,
    find_supervisor_viz_path,
    get_tool_input, output,
)


def main() -> None:
    tool_input = get_tool_input()
    worker_name = tool_input.get("agent_name", "unknown_worker")
    success = tool_input.get("success", True)
    error_msg = tool_input.get("error", "")

    path = find_supervisor_viz_path()
    data = read_viz_state(path)

    # Emit worker → completed or error
    if success:
        append_event(
            path,
            agent_name=worker_name,
            agent_type="worker",
            event_type="completed",
            status="completed",
            description=f"Worker {worker_name} completed",
        )
    else:
        append_event(
            path,
            agent_name=worker_name,
            agent_type="worker",
            event_type="error",
            status="error",
            description=f"Worker {worker_name} failed: {str(error_msg)[:150]}",
        )

    # Emit supervisor → agent_return + reviewing
    sup_name = find_supervisor_name(read_viz_state(path))
    if sup_name:
        append_event(
            path,
            agent_name=sup_name,
            agent_type="supervisor",
            event_type="agent_return",
            status="reviewing",
            description=f"Reviewing result from {worker_name}",
        )

    output({
        "decision": "allow",
        "telemetry": {"worker": worker_name, "success": success},
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{SKILL_TAG} on_subtask_finish error: {exc}", file=sys.stderr)
        output({"decision": "allow"})
