#!/usr/bin/env python3
"""Hook: PostToolUse — freshness-driven reminder engine.

Instead of sending the same reminder every tool call, this hook:
1. Reads the current step_number from the framework (via env).
2. Checks file mtimes against a write tracker to detect staleness.
3. Sends a reminder only when files are stale enough, with cooldown
   to prevent spamming.
"""

from common import (
    CONTEXT_FILE, TRACE_FILE, SKILL_TAG, STALENESS_CONFIG,
    TURNS_BETWEEN_REMINDERS,
    runtime_dir, get_runtime_agent_path, get_step_number, output,
    load_write_tracker, save_write_tracker, detect_writes_and_update,
)


def main() -> None:
    agent = get_runtime_agent_path()
    rd = runtime_dir(agent)
    step = get_step_number()

    # Grace period: let agent focus in early steps, don't nag.
    if step <= 3:
        output({"decision": "allow"})
        return

    # Load tracker and detect writes via mtime comparison.
    tracker = load_write_tracker(rd)
    staleness = detect_writes_and_update(rd, tracker, step)

    # Reminder cooldown: avoid spamming after a recent reminder.
    last_reminded = tracker.get("last_reminded_at_step", 0)
    if step - last_reminded < TURNS_BETWEEN_REMINDERS:
        save_write_tracker(rd, tracker)
        output({"decision": "allow"})
        return

    # Evaluate each file's staleness against thresholds.
    gentle_files = []
    urgent_files = []

    for fname in [TRACE_FILE, CONTEXT_FILE]:
        cfg = STALENESS_CONFIG[fname]
        stale = staleness.get(fname, -1)

        if stale == -1:
            # Never written — more urgent than "stale".
            urgent_files.append(f"{fname} (never written, still empty template)")
        elif stale == 0:
            pass  # Just written, skip.
        elif cfg["urgent_after"] and stale >= cfg["urgent_after"]:
            urgent_files.append(f"{fname} (last updated {stale} steps ago)")
        elif cfg["gentle_after"] and stale >= cfg["gentle_after"]:
            gentle_files.append(f"{fname} (last updated {stale} steps ago)")

    # Build reminder message based on urgency.
    if urgent_files:
        msg = (
            f"{SKILL_TAG} WARNING: Runtime files are significantly stale "
            f"at step {step}:\n"
            + "\n".join(f"  - {f}" for f in urgent_files)
        )
        if gentle_files:
            msg += "\nAlso consider updating:\n"
            msg += "\n".join(f"  - {f}" for f in gentle_files)
        msg += (
            "\n\nUpdate these files NOW with your current progress. "
            "Trace should reflect recent actions; context should reflect "
            "current understanding of the task."
        )
        tracker["last_reminded_at_step"] = step
        save_write_tracker(rd, tracker)
        output({"decision": "allow", "agent_context": msg})

    elif gentle_files:
        msg = (
            f"{SKILL_TAG} Consider updating your runtime files "
            f"(step {step}):\n"
            + "\n".join(f"  - {f}" for f in gentle_files)
        )
        tracker["last_reminded_at_step"] = step
        save_write_tracker(rd, tracker)
        output({"decision": "allow", "agent_context": msg})

    else:
        # All files are fresh — stay silent.
        save_write_tracker(rd, tracker)
        output({"decision": "allow"})


if __name__ == "__main__":
    main()
