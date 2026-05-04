#!/usr/bin/env python3
"""Hook: TaskCompleted / StopFailure — Emit final completed or error event."""

import os
import sys
from pathlib import Path

from common import (
    viz_output_path, read_viz_state, append_event, find_supervisor_name,
    get_agent_name, get_runtime_agent_path, get_tool_input, output,
)


def main() -> None:
    tool_input = get_tool_input()
    agent_name = tool_input.get("agent_name") or get_agent_name()

    # Determine if this is a success or failure from the script name
    script_name = Path(sys.argv[0]).stem if sys.argv else ""
    is_fail = "fail" in script_name

    path = viz_output_path(get_runtime_agent_path())
    data = read_viz_state(path)

    # Find supervisor from config, or use agent_name
    sup_name = find_supervisor_name(data) or agent_name

    if is_fail:
        error_msg = str(tool_input.get("error", "Task failed"))[:200]
        append_event(
            path,
            agent_name=sup_name,
            agent_type="supervisor",
            event_type="error",
            status="error",
            description=error_msg,
        )
    else:
        result_str = str(tool_input.get("result", "Task completed"))[:200]
        append_event(
            path,
            agent_name=sup_name,
            agent_type="supervisor",
            event_type="completed",
            status="completed",
            description=result_str,
        )

    output({
        "decision": "allow",
        "telemetry": {"final_status": "error" if is_fail else "completed"},
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[agent-visualization] on_task_complete error: {exc}", file=sys.stderr)
        output({"decision": "allow"})
