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
    get_tool_name,
    output,
    persistent_insights_path,
    read_template,
    task_workspace_dir,
)


def _bootstrap_workspace(task_workspace, persistent_insights):
    """Create the task workspace and seed template files if missing."""
    task_workspace.mkdir(parents=True, exist_ok=True)

    # Ephemeral files — only create if absent (do not overwrite).
    for fname, title in ((CONTEXT_FILE, "Context"), (TRACE_FILE, "Trace")):
        path = task_workspace / fname
        if not path.exists():
            path.write_text(read_template(fname, title), encoding="utf-8")

    # Persistent file — only create if absent or trivially small.
    if not persistent_insights.exists() or persistent_insights.stat().st_size <= 100:
        if not persistent_insights.exists():
            persistent_insights.write_text(
                read_template(INSIGHTS_FILE, "Insights"), encoding="utf-8",
            )


def main() -> None:
    workspace = task_workspace_dir()
    tool = get_tool_name()

    _bootstrap_workspace(workspace, persistent_insights_path())

    output({
        "decision": "allow",
        "user_message": (
            f"{HOOK_TAG} Subtask '{tool}' started. "
            f"Record meaningful progress in {workspace / TRACE_FILE}."
        ),
        "telemetry": {"task_workspace": str(workspace)},
    })


if __name__ == "__main__":
    main()
