#!/usr/bin/env python3
"""Hook: TaskCreated — bootstrap the runtime directory for the active agent.

Core behaviour:
- ``context.md`` and ``trace.md`` are isolated by task id and created when missing.
- ``insights.md`` is **preserved** if it already exists (cross-session experience).
  Only created from template when missing.
- Old insights are compacted via ``summarize_insights`` when the file is too long.
"""

from common import (
    CONTEXT_FILE,
    HOOK_TAG,
    INSIGHTS_FILE,
    TRACE_FILE,
    output,
    persistent_insights_path,
    read_template,
    summarize_insights,
    task_workspace_dir,
)


def main() -> None:
    workspace = task_workspace_dir()
    # Runtime owns only this canonical workspace. Repository-root files may have
    # the same names and must never be treated as disposable migration state.
    workspace.mkdir(parents=True, exist_ok=True)

    # Task files are fresh for a new task id and preserved on resume.
    for filename, title in ((CONTEXT_FILE, "Context"), (TRACE_FILE, "Trace")):
        path = workspace / filename
        if not path.exists():
            path.write_text(read_template(filename, title), encoding="utf-8")

    # Persistent file — preserve if it has real content.
    persistent_insights = persistent_insights_path()
    has_prior_insights = (
        persistent_insights.exists() and persistent_insights.stat().st_size > 100
    )

    if not has_prior_insights:
        persistent_insights.write_text(
            read_template(INSIGHTS_FILE, "Insights"), encoding="utf-8",
        )

    # Compact insights if they have grown too long.
    if has_prior_insights:
        summarize_insights(persistent_insights)

    # Build output message.
    ctx_parts = [
        f"{HOOK_TAG} Runtime ready at {workspace}.",
        f"Use {workspace / CONTEXT_FILE} for task state snapshot,",
        f"{workspace / TRACE_FILE} for execution trace,",
        f"{persistent_insights} for cross-task insights.",
    ]
    if has_prior_insights:
        ctx_parts.append(
            f"Found insights from previous tasks at {persistent_insights}. "
            "Review before starting."
        )

    output({
        "decision": "allow",
        "agent_context": " ".join(ctx_parts),
        "telemetry": {"task_workspace": str(workspace)},
    })


if __name__ == "__main__":
    main()
