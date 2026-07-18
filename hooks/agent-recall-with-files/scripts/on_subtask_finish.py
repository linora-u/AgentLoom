#!/usr/bin/env python3
"""Hook: SubagentStop — remind the agent to record the subtask result."""

from common import (
    HOOK_TAG,
    INSIGHTS_FILE,
    TRACE_FILE,
    get_runtime_agent_path,
    get_tool_input,
    get_tool_name,
    output,
    runtime_dir,
)


def main() -> None:
    agent = get_runtime_agent_path()
    rd = runtime_dir(agent)
    tool = get_tool_name()
    ti = get_tool_input()

    msg = (
        f"{HOOK_TAG} Subtask '{tool}' finished. "
        f"Record the result in {rd / TRACE_FILE}."
    )

    success = ti.get("success", True) if ti else True
    if not success:
        error = (ti.get("error") or "") or "unknown error"
        msg += (
            f" Log this as a [pitfall] in {rd / INSIGHTS_FILE} — "
            f"it persists across sessions. Error: {error}."
        )

    output({
        "decision": "allow",
        "agent_context": msg,
        "telemetry": {"runtime_dir": str(rd)},
    })


if __name__ == "__main__":
    main()
