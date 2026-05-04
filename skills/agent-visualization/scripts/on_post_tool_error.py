#!/usr/bin/env python3
"""Hook: PostToolUseFailure — Update last event description with tool error."""

import sys

from common import (
    SKILL_TAG,
    find_supervisor_viz_path, read_viz_state, write_viz_state,
    is_filtered_tool, get_tool_name, get_hook_context, output,
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
    data = read_viz_state(path)
    timeline = data.get("timeline", [])

    if not timeline:
        output({"decision": "allow"})
        return

    # Update the last event's description with error
    last_event = timeline[-1]
    error_str = str(tool_response.get("error", "Unknown error"))
    if error_str:
        summary = error_str[:200]
        last_event["description"] = f"{tool_name} error: {summary}"
        last_event["status"] = "error"
        if "error" not in last_event:
            last_event["error"] = summary
        write_viz_state(path, data)

    output({"decision": "allow"})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{SKILL_TAG} on_post_tool_error error: {exc}", file=sys.stderr)
        output({"decision": "allow"})
