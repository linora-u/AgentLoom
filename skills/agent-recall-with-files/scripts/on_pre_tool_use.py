#!/usr/bin/env python3
"""Hook: PreToolUse — inject recent canonical workspace context.

Injects runtime file contents into the agent context so the LLM can
see its own notes.  Empty-template files get a brief "(empty)" marker
instead of the full template text to reduce noise.
"""

from common import (
    CONTEXT_FILE,
    INSIGHTS_FILE,
    PRE_TOOL_INSIGHTS_LINES,
    PRE_TOOL_TRACE_LINES,
    SKILL_TAG,
    TRACE_FILE,
    is_template_only,
    output,
    persistent_insights_path,
    read_full,
    tail,
    task_workspace_dir,
)


def main() -> None:
    workspace = task_workspace_dir()
    persistent_insights = persistent_insights_path()

    # Read recent context from runtime files, with template-awareness.
    context_full = "" if is_template_only(workspace / CONTEXT_FILE, CONTEXT_FILE) else read_full(workspace / CONTEXT_FILE)
    trace_tail = "" if is_template_only(workspace / TRACE_FILE, TRACE_FILE) else tail(workspace / TRACE_FILE, PRE_TOOL_TRACE_LINES)
    insights_tail = "" if is_template_only(persistent_insights, INSIGHTS_FILE) else tail(persistent_insights, PRE_TOOL_INSIGHTS_LINES)

    if not context_full and not trace_tail and not insights_tail:
        context_text = (
            f"{SKILL_TAG} No runtime notes yet under {workspace}. "
            f"Keep {workspace / CONTEXT_FILE}, {workspace / TRACE_FILE}, and "
            f"{persistent_insights} current as you work."
        )
    else:
        parts = [f"{SKILL_TAG} Runtime context from {workspace}:"]
        if context_full:
            parts.append(f"[{CONTEXT_FILE}]\n{context_full}")
        if trace_tail:
            parts.append(f"[{TRACE_FILE}]\n{trace_tail}")
        if insights_tail:
            parts.append(f"[{INSIGHTS_FILE}]\n{insights_tail}")
        context_text = "\n\n".join(parts)

    result: dict = {
        "decision": "allow",
        "agent_context": context_text,
        "telemetry": {"task_workspace": str(workspace)},
    }

    output(result)


if __name__ == "__main__":
    main()
