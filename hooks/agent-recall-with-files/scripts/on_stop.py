#!/usr/bin/env python3
"""Hook: Stop — allow termination and report final-recording guidance."""

from common import CONTEXT_FILE, HOOK_TAG, TRACE_FILE, output, task_workspace_dir


def main() -> None:
    workspace = task_workspace_dir()

    output({
        "decision": "allow",
        "reason": (
            f"{HOOK_TAG} Stop default-allow. "
            f"Ensure {workspace / CONTEXT_FILE} and {workspace / TRACE_FILE} reflect final state."
        ),
        "telemetry": {"status": "disabled", "task_workspace": str(workspace)},
    })


if __name__ == "__main__":
    main()
