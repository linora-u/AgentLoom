#!/usr/bin/env python3
"""Hook: TaskCreated — bootstrap the runtime directory for this agent.

Core behaviour:
- ``context.md`` and ``trace.md`` are always recreated from templates (ephemeral).
- ``insights.md`` is **preserved** if it already exists (cross-session experience).
  Only created from template when missing.
- Old insights are compressed via ``summarize_insights`` when the file is too long.
"""

from pathlib import Path
from common import (
    CONTEXT_FILE, TRACE_FILE, INSIGHTS_FILE, LEGACY_ROOT_FILES, SKILL_TAG,
    runtime_dir, read_template, remove_path, summarize_insights,
    get_runtime_agent_path, output,
)


def main() -> None:
    agent = get_runtime_agent_path()
    rd = runtime_dir(agent)

    # Clean up legacy artifacts from workspace root.
    remove_path(Path(".planning"))
    for name in LEGACY_ROOT_FILES:
        remove_path(Path(name))

    # Clean up legacy files with old names inside the runtime dir.
    for old_name in ("progress.md", "findings.md"):
        old_path = rd / old_name
        if old_path.exists():
            remove_path(old_path)

    # Ensure runtime directory exists (do NOT delete it — insights must survive).
    rd.mkdir(parents=True, exist_ok=True)

    # Ephemeral files — always recreate from template.
    (rd / CONTEXT_FILE).write_text(
        read_template(CONTEXT_FILE, "Context"), encoding="utf-8",
    )
    (rd / TRACE_FILE).write_text(
        read_template(TRACE_FILE, "Trace"), encoding="utf-8",
    )

    # Persistent file — preserve if it has real content.
    insights_path = rd / INSIGHTS_FILE
    has_prior_insights = (
        insights_path.exists() and insights_path.stat().st_size > 100
    )

    if not has_prior_insights:
        insights_path.write_text(
            read_template(INSIGHTS_FILE, "Insights"), encoding="utf-8",
        )

    # Compress insights if they have grown too long.
    if has_prior_insights:
        summarize_insights(insights_path)

    # Build output message.
    ctx_parts = [
        f"{SKILL_TAG} Runtime ready at {rd}.",
        f"Use {rd / CONTEXT_FILE} for task state snapshot,",
        f"{rd / TRACE_FILE} for execution trace,",
        f"{rd / INSIGHTS_FILE} for cross-session insights.",
    ]
    if has_prior_insights:
        ctx_parts.append(
            f"Found insights from previous sessions at {insights_path}. "
            "Review before starting."
        )

    output({
        "decision": "allow",
        "agent_context": " ".join(ctx_parts),
        "telemetry": {"runtime_dir": str(rd)},
    })


if __name__ == "__main__":
    main()
