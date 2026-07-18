#!/usr/bin/env python3
"""Hook: SubagentStop — record completion and supervisor review."""

import sys
from typing import Any

from common import (
    HOOK_TAG,
    append_event_to_state,
    find_supervisor_name,
    get_tool_input,
    output,
    update_viz_state,
    viz_output_path,
)


def main() -> None:
    tool_input = get_tool_input()
    worker_name = str(tool_input.get("agent_name", "unknown_worker"))
    success = bool(tool_input.get("success", True))
    error_msg = str(tool_input.get("error", ""))

    def record(data: dict[str, Any]) -> None:
        append_event_to_state(
            data,
            agent_name=worker_name,
            agent_type="worker",
            event_type="completed" if success else "error",
            status="completed" if success else "error",
            description=(
                f"Worker {worker_name} completed"
                if success
                else f"Worker {worker_name} failed: {error_msg[:150]}"
            ),
        )
        if supervisor := find_supervisor_name(data):
            append_event_to_state(
                data,
                agent_name=supervisor,
                agent_type="supervisor",
                event_type="agent_return",
                status="reviewing",
                description=f"Reviewing result from {worker_name}",
            )

    update_viz_state(viz_output_path(), record)
    output(
        {
            "decision": "allow",
            "telemetry": {"worker": worker_name, "success": success},
        }
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{HOOK_TAG} on_subtask_finish error: {exc}", file=sys.stderr)
        output({"decision": "allow"})
