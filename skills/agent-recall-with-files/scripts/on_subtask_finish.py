#!/usr/bin/env python3
"""Hook: SubagentStop — remind agent to record subtask result."""

from common import (
    TRACE_FILE, INSIGHTS_FILE, SKILL_TAG,
    runtime_dir, get_runtime_agent_path, get_tool_name, get_tool_input, output,
)


def main() -> None:
    agent = get_runtime_agent_path()
    rd = runtime_dir(agent)
    tool = get_tool_name()
    ti = get_tool_input()

    msg = (
        f"{SKILL_TAG} Subtask '{tool}' finished. "
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
