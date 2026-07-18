#!/usr/bin/env python3
"""Hook: SubagentStart — record worker activation and supervisor wait."""

import sys
from typing import Any

from common import (
    HOOK_TAG,
    append_event_to_state,
    ensure_agent_in_config,
    find_supervisor_name,
    get_tool_input,
    output,
    update_viz_state,
    viz_output_path,
)


def main() -> None:
    tool_input = get_tool_input()
    worker_name = str(tool_input.get("agent_name", "unknown_worker"))
    path = viz_output_path()

    def record(data: dict[str, Any]) -> None:
        ensure_agent_in_config(data, worker_name, "worker")
        if supervisor := find_supervisor_name(data):
            append_event_to_state(
                data,
                agent_name=supervisor,
                agent_type="supervisor",
                event_type="agent_call",
                status="waiting",
                description=f"Calling worker: {worker_name}",
                target_agent=worker_name,
            )
        append_event_to_state(
            data,
            agent_name=worker_name,
            agent_type="worker",
            event_type="activated",
            status="thinking",
            description=f"Worker {worker_name} started",
        )

    update_viz_state(path, record)
    output(
        {
            "decision": "allow",
            "telemetry": {"worker": worker_name, "viz_file": str(path)},
        }
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{HOOK_TAG} on_subtask_start error: {exc}", file=sys.stderr)
        output({"decision": "allow"})
