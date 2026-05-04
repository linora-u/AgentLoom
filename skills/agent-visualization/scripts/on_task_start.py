#!/usr/bin/env python3
"""Hook: TaskCreated — Initialize visualization JSON with supervisor agent."""

from common import (
    SKILL_TAG,
    viz_output_path, read_viz_state, write_viz_state,
    ensure_agent_in_config, append_event,
    get_agent_name, get_runtime_agent_path, get_tool_input, output,
)


def main() -> None:
    tool_input = get_tool_input()
    agent_name = tool_input.get("agent_name") or get_agent_name()
    task_text = tool_input.get("task_text", "")

    path = viz_output_path(get_runtime_agent_path())

    # Initialize fresh state
    data = {
        "config": {
            "title": f"Agent Execution: {agent_name}",
            "agents": [],
        },
        "timeline": [],
    }

    # Add supervisor
    ensure_agent_in_config(data, agent_name, "supervisor")

    # Pre-register all worker agents from supervisor config
    worker_agents = tool_input.get("worker_agents", [])
    for w_name in worker_agents:
        if w_name:
            ensure_agent_in_config(data, w_name, "worker")

    write_viz_state(path, data)

    # Emit start event
    desc = task_text[:200] if task_text else "Task started"
    append_event(
        path,
        agent_name=agent_name,
        agent_type="supervisor",
        event_type="start",
        status="thinking",
        description=desc,
    )

    output({
        "decision": "allow",
        "telemetry": {"viz_file": str(path), "supervisor": agent_name},
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import sys
        print(f"{SKILL_TAG} on_task_start error: {exc}", file=sys.stderr)
        output({"decision": "allow"})
