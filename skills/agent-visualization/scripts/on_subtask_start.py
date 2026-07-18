#!/usr/bin/env python3
"""Hook: SubagentStart — Worker agent activated, supervisor enters waiting."""

import sys

from common import (
    SKILL_TAG,
    append_event,
    find_supervisor_name,
    find_supervisor_viz_path,
    get_tool_input,
    output,
    read_viz_state,
    register_agent_in_config,
)


def main() -> None:
    tool_input = get_tool_input()
    worker_name = tool_input.get("agent_name", "unknown_worker")

    # RuntimeContext injects the root supervisor timeline for every agent.
    path = find_supervisor_viz_path()

    # Dynamically add worker to config without losing concurrent workers.
    register_agent_in_config(path, worker_name, "worker")
    data = read_viz_state(path)

    # Emit supervisor → waiting (agent_call)
    sup_name = find_supervisor_name(data)
    if sup_name:
        append_event(
            path,
            agent_name=sup_name,
            agent_type="supervisor",
            event_type="agent_call",
            status="waiting",
            description=f"Calling worker: {worker_name}",
            target_agent=worker_name,
        )

    # Emit worker → activated (thinking — just started, figuring out what to do)
    append_event(
        path,
        agent_name=worker_name,
        agent_type="worker",
        event_type="activated",
        status="thinking",
        description=f"Worker {worker_name} started",
    )

    output({
        "decision": "allow",
        "telemetry": {"worker": worker_name, "viz_file": str(path)},
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{SKILL_TAG} on_subtask_start error: {exc}", file=sys.stderr)
        output({"decision": "allow"})
