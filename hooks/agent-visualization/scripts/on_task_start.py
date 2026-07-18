#!/usr/bin/env python3
"""Hook: TaskCreated — initialize visualization state with the supervisor."""

import sys
from typing import Any

from common import (
    HOOK_TAG,
    append_event_to_state,
    ensure_agent_in_config,
    get_agent_name,
    get_root_run_id,
    get_runtime_agent_path,
    get_tool_input,
    output,
    update_viz_state,
    viz_output_path,
)


def main() -> None:
    tool_input = get_tool_input()
    agent_name = str(tool_input.get("agent_name") or get_agent_name())
    task_text = str(tool_input.get("task_text", ""))
    path = viz_output_path()

    def initialize(data: dict[str, Any]) -> None:
        data.clear()
        data.update(
            {
                "config": {
                    "title": f"Agent Execution: {agent_name}",
                    "agents": [],
                    "root_run_id": get_root_run_id(),
                    "runtime_agent_path": get_runtime_agent_path(),
                },
                "timeline": [],
            }
        )
        ensure_agent_in_config(data, agent_name, "supervisor")
        worker_agents = tool_input.get("worker_agents", [])
        if isinstance(worker_agents, list):
            for worker_name in worker_agents:
                if isinstance(worker_name, str) and worker_name:
                    ensure_agent_in_config(data, worker_name, "worker")
        append_event_to_state(
            data,
            agent_name=agent_name,
            agent_type="supervisor",
            event_type="start",
            status="thinking",
            description=task_text[:200] if task_text else "Task started",
        )

    update_viz_state(path, initialize)
    output(
        {
            "decision": "allow",
            "telemetry": {"viz_file": str(path), "supervisor": agent_name},
        }
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{HOOK_TAG} on_task_start error: {exc}", file=sys.stderr)
        output({"decision": "allow"})
