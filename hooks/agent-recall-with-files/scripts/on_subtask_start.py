#!/usr/bin/env python3
"""Hook: SubagentStart — bootstrap runtime files and request progress records.

When a sub-agent starts, its runtime directory may not yet exist because
``TaskCreated`` only fires for the top-level agent.  This hook ensures that
the runtime directory and template files (context.md, trace.md, insights.md)
are created for every sub-agent, so the LLM only needs to *update* them
rather than *create* them from scratch.

Existing files are never overwritten — insights.md is preserved across
sessions, and if context.md / trace.md already contain real content they
are left untouched.
"""

from common import (
    CONTEXT_FILE,
    HOOK_TAG,
    INSIGHTS_FILE,
    TRACE_FILE,
    get_runtime_agent_path,
    get_tool_name,
    output,
    read_template,
    runtime_dir,
)


def _bootstrap_runtime(rd):
    """Create the runtime directory and seed template files if missing."""
    rd.mkdir(parents=True, exist_ok=True)

    # Ephemeral files — only create if absent (do not overwrite).
    for fname, title in ((CONTEXT_FILE, "Context"), (TRACE_FILE, "Trace")):
        path = rd / fname
        if not path.exists():
            path.write_text(read_template(fname, title), encoding="utf-8")

    # Persistent file — only create if absent or trivially small.
    insights_path = rd / INSIGHTS_FILE
    if not insights_path.exists() or insights_path.stat().st_size <= 100:
        if not insights_path.exists():
            insights_path.write_text(
                read_template(INSIGHTS_FILE, "Insights"), encoding="utf-8",
            )


def main() -> None:
    agent = get_runtime_agent_path()
    rd = runtime_dir(agent)
    tool = get_tool_name()

    _bootstrap_runtime(rd)

    output({
        "decision": "allow",
        "user_message": (
            f"{HOOK_TAG} Subtask '{tool}' started. "
            f"Record meaningful progress in {rd / TRACE_FILE}."
        ),
        "telemetry": {"runtime_dir": str(rd)},
    })


if __name__ == "__main__":
    main()
