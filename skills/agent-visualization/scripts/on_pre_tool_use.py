#!/usr/bin/env python3
"""Hook: PreToolUse — Emit tool_call event for user-visible tools."""

import sys

from common import (
    SKILL_TAG,
    find_supervisor_viz_path, append_event,
    is_filtered_tool, read_viz_state,
    get_agent_name, get_tool_name, get_tool_input, output,
)


def main() -> None:
    tool_name = get_tool_name()

    # Filter out internal hooks
    if is_filtered_tool(tool_name):
        output({"decision": "allow"})
        return

    agent_name = get_agent_name()
    tool_input = get_tool_input()

    path = find_supervisor_viz_path()
    data = read_viz_state(path)

    # Determine agent type from config
    agents = data.get("config", {}).get("agents", [])
    agent_type = "worker"
    for a in agents:
        if a.get("name") == agent_name:
            agent_type = a.get("type", "worker")
            break

    # If this agent is a worker but has not yet been "activated" in the timeline,
    # these tool calls are from the agent initialization phase (e.g. skill hooks
    # scanning files) — attribute them to the supervisor instead.
    if agent_type == "worker":
        timeline = data.get("timeline", [])
        worker_activated = any(
            ev.get("event_type") == "activated" and ev.get("agent_name") == agent_name
            for ev in timeline
        )
        if not worker_activated:
            from common import find_supervisor_name
            sup = find_supervisor_name(data)
            if sup:
                agent_name = sup
                agent_type = "supervisor"

    # All tool calls use a single unified status — simple and maintenance-free
    event_type = "tool_call"
    status = "codeact"

    # Build tool_args summary (truncated for display)
    tool_args = {}
    for k, v in tool_input.items():
        if k in ("agent_name", "task_id", "cwd"):
            continue
        sv = str(v)
        tool_args[k] = sv[:100] if len(sv) > 100 else sv

    desc = f"Calling tool: {tool_name}"

    append_event(
        path,
        agent_name=agent_name,
        agent_type=agent_type,
        event_type=event_type,
        status=status,
        description=desc,
        tool_name=tool_name,
        tool_args=tool_args if tool_args else None,
    )

    output({"decision": "allow"})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{SKILL_TAG} on_pre_tool_use error: {exc}", file=sys.stderr)
        output({"decision": "allow"})
