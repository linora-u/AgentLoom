#!/usr/bin/env python3
"""Hook: TaskCreated — bootstrap the runtime directory for the active agent.

Core behaviour:
- ``context.md`` and ``trace.md`` are isolated by task id and created when missing.
- ``insights.md`` is **preserved** if it already exists (cross-session experience).
  Only created from template when missing.
- Old insights are compressed via ``summarize_insights`` when the file is too long.
"""

from common import (
    CONTEXT_FILE,
    HOOK_TAG,
    INSIGHTS_FILE,
    LEGACY_ROOT_FILES,
    TRACE_FILE,
    output,
    persistent_insights_path,
    project_root_dir,
    read_template,
    remove_path,
    summarize_insights,
    task_workspace_dir,
)


def main() -> None:
    workspace = task_workspace_dir()
    project_root = project_root_dir()

    # Clean up legacy artifacts from workspace root.
    remove_path(project_root / ".planning")
    for name in LEGACY_ROOT_FILES:
        remove_path(project_root / name)

    # Clean up legacy files with old names inside the runtime dir.
    for old_name in ("progress.md", "findings.md"):
        old_path = workspace / old_name
        if old_path.exists():
            remove_path(old_path)

    # Ensure runtime directory exists (do NOT delete it — insights must survive).
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

    # Compress insights if they have grown too long.
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
