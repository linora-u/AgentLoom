#!/usr/bin/env python3
"""Hook: PreToolUse — emit a tool-call event for user-visible tools."""

import sys
from typing import Any

from common import (
    HOOK_TAG,
    append_event_to_state,
    find_supervisor_name,
    get_agent_name,
    get_tool_input,
    get_tool_name,
    is_filtered_tool,
    output,
    update_viz_state,
    viz_output_path,
)


def main() -> None:
    tool_name = get_tool_name()
    if is_filtered_tool(tool_name):
        output({"decision": "allow"})
        return

    active_agent_name = get_agent_name()
    tool_input = get_tool_input()
    tool_args = {
        key: value_text[:100]
        for key, value in tool_input.items()
        if key not in {"agent_name", "task_id", "cwd"}
        and (value_text := str(value))
    }

    def record(data: dict[str, Any]) -> None:
        agent_name = active_agent_name
        agent_type = "worker"
        agents = data.get("config", {}).get("agents", [])
        for agent in agents:
            if agent.get("name") == agent_name:
                agent_type = str(agent.get("type", "worker"))
                break

        if agent_type == "worker":
            worker_activated = any(
                event.get("event_type") == "activated"
                and event.get("agent_name") == agent_name
                for event in data.get("timeline", [])
            )
            if not worker_activated and (supervisor := find_supervisor_name(data)):
                agent_name = supervisor
                agent_type = "supervisor"

        append_event_to_state(
            data,
            agent_name=agent_name,
            agent_type=agent_type,
            event_type="tool_call",
            status="codeact",
            description=f"Calling tool: {tool_name}",
            tool_name=tool_name,
            tool_args=tool_args or None,
        )

    update_viz_state(viz_output_path(), record)
    output({"decision": "allow"})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{HOOK_TAG} on_pre_tool_use error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
