#!/usr/bin/env python3
"""Hook: SubagentStop — remind the agent to record the subtask result."""

from common import (
    HOOK_TAG,
    TRACE_FILE,
    get_tool_input,
    get_tool_name,
    output,
    persistent_insights_path,
    task_workspace_dir,
)


def main() -> None:
    workspace = task_workspace_dir()
    tool = get_tool_name()
    ti = get_tool_input()

    msg = (
        f"{HOOK_TAG} Subtask '{tool}' finished. "
        f"Record the result in {workspace / TRACE_FILE}."
    )

    success = ti.get("success", True) if ti else True
    if not success:
        error = (ti.get("error") or "") or "unknown error"
        msg += (
            f" Log this as a [pitfall] in {persistent_insights_path()} — "
            f"it persists across sessions. Error: {error}."
        )

    output({
        "decision": "allow",
        "agent_context": msg,
        "telemetry": {"task_workspace": str(workspace)},
    })


if __name__ == "__main__":
    main()
