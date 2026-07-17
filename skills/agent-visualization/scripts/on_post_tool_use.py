#!/usr/bin/env python3
"""Hook: PostToolUse — Update last event description with tool result summary."""

import sys

from common import (
    SKILL_TAG,
    find_supervisor_viz_path,
    get_agent_name,
    get_hook_context,
    get_tool_name,
    is_filtered_tool,
    output,
    update_latest_tool_event,
)


def main() -> None:
    tool_name = get_tool_name()

    # Filter out internal hooks
    if is_filtered_tool(tool_name):
        output({"decision": "allow"})
        return

    ctx = get_hook_context()
    tool_response = ctx.get("tool_response")
    if not tool_response:
        output({"decision": "allow"})
        return

    path = find_supervisor_viz_path()
    result_str = str(tool_response.get("result", ""))
    if result_str:
        summary = result_str[:200]
        update_latest_tool_event(
            path,
            agent_name=get_agent_name(),
            tool_name=tool_name,
            description=f"{tool_name}: {summary}",
            result=summary,
        )

    output({"decision": "allow"})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{SKILL_TAG} on_post_tool_use error: {exc}", file=sys.stderr)
        output({"decision": "allow"})
