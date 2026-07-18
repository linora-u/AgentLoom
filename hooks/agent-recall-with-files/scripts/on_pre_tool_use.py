#!/usr/bin/env python3
"""Hook: PreToolUse — inject runtime context and normalize paths.

Injects runtime file contents into the agent context so the LLM can
see its own notes.  Empty-template files get a brief "(empty)" marker
instead of the full template text to reduce noise.
"""

from common import (
    CONTEXT_FILE,
    HOOK_TAG,
    INSIGHTS_FILE,
    PRE_TOOL_INSIGHTS_LINES,
    PRE_TOOL_TRACE_LINES,
    TRACE_FILE,
    get_runtime_agent_path,
    get_tool_input,
    is_template_only,
    normalize_tool_input,
    output,
    read_full,
    runtime_dir,
    tail,
)


def main() -> None:
    agent = get_runtime_agent_path()
    rd = runtime_dir(agent)
    ti = get_tool_input()

    # Normalize runtime path aliases in tool_input.
    modified_input = normalize_tool_input(agent, ti)

    # Read recent context from runtime files, with template-awareness.
    context_full = "" if is_template_only(rd / CONTEXT_FILE, CONTEXT_FILE) else read_full(rd / CONTEXT_FILE)
    trace_tail = "" if is_template_only(rd / TRACE_FILE, TRACE_FILE) else tail(rd / TRACE_FILE, PRE_TOOL_TRACE_LINES)
    insights_tail = "" if is_template_only(rd / INSIGHTS_FILE, INSIGHTS_FILE) else tail(rd / INSIGHTS_FILE, PRE_TOOL_INSIGHTS_LINES)

    prefix = ""
    if modified_input is not None:
        prefix = (
            f"{HOOK_TAG} Normalized runtime paths to {rd}. "
            f"Use the exact current agent directory and do not invent aliases.\n\n"
        )

    if not context_full and not trace_tail and not insights_tail:
        context_text = (
            f"{HOOK_TAG} No runtime notes yet under {rd}. "
            f"Keep {CONTEXT_FILE}, {TRACE_FILE} and {INSIGHTS_FILE} current as you work."
        )
    else:
        parts = [f"{HOOK_TAG} Runtime context from {rd}:"]
        if context_full:
            parts.append(f"[{CONTEXT_FILE}]\n{context_full}")
        if trace_tail:
            parts.append(f"[{TRACE_FILE}]\n{trace_tail}")
        if insights_tail:
            parts.append(f"[{INSIGHTS_FILE}]\n{insights_tail}")
        context_text = "\n\n".join(parts)

    result: dict = {
        "decision": "modify" if modified_input is not None else "allow",
        "agent_context": prefix + context_text,
        "telemetry": {"runtime_dir": str(rd)},
    }
    if modified_input is not None:
        result["modified_input"] = modified_input

    output(result)


if __name__ == "__main__":
    main()
