#!/usr/bin/env python3
"""Hook: PostToolUseFailure — update the matching tool event with its error."""

import sys
from typing import Any

from common import (
    HOOK_TAG,
    get_hook_context,
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

    tool_response = get_hook_context().get("tool_response")
    if not tool_response:
        output({"decision": "allow"})
        return
    error = (
        tool_response.get("error", "Unknown error")
        if isinstance(tool_response, dict)
        else tool_response
    )
    summary = str(error)[:200]
    if summary:

        def record(data: dict[str, Any]) -> None:
            for event in reversed(data.get("timeline", [])):
                if (
                    event.get("event_type") == "tool_call"
                    and event.get("tool_name") == tool_name
                ):
                    event["description"] = f"{tool_name} error: {summary}"
                    event["status"] = "error"
                    event.setdefault("error", summary)
                    return

        update_viz_state(viz_output_path(), record)
    output({"decision": "allow"})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{HOOK_TAG} on_post_tool_error error: {exc}", file=sys.stderr)
        output({"decision": "allow"})
