#!/usr/bin/env python3
"""Hook: TaskCompleted / StopFailure — emit the terminal event."""

import sys
from pathlib import Path
from typing import Any

from common import (
    HOOK_TAG,
    append_event_to_state,
    find_supervisor_name,
    get_agent_name,
    get_hook_context,
    get_tool_input,
    output,
    update_viz_state,
    viz_output_path,
)


def main() -> None:
    tool_input = get_tool_input()
    fallback_agent = str(tool_input.get("agent_name") or get_agent_name())
    is_failure = "fail" in Path(sys.argv[0]).stem

    def record(data: dict[str, Any]) -> None:
        supervisor = find_supervisor_name(data) or fallback_agent
        if is_failure:
            description = str(
                tool_input.get("error")
                or get_hook_context().get("tool_response")
                or "Task failed"
            )[:200]
        else:
            description = str(
                tool_input.get("result")
                or get_hook_context().get("tool_response")
                or "Task completed"
            )[:200]
        append_event_to_state(
            data,
            agent_name=supervisor,
            agent_type="supervisor",
            event_type="error" if is_failure else "completed",
            status="error" if is_failure else "completed",
            description=description,
        )

    update_viz_state(viz_output_path(), record)
    output(
        {
            "decision": "allow",
            "telemetry": {
                "final_status": "error" if is_failure else "completed"
            },
        }
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{HOOK_TAG} on_task_complete error: {exc}", file=sys.stderr)
        output({"decision": "allow"})
