#!/usr/bin/env python3
"""Hook: Stop — default-allow with a reminder to keep runtime files current."""

from common import CONTEXT_FILE, TRACE_FILE, SKILL_TAG, runtime_dir, get_runtime_agent_path, output


def main() -> None:
    agent = get_runtime_agent_path()
    rd = runtime_dir(agent)

    output({
        "decision": "allow",
        "reason": (
            f"{SKILL_TAG} Stop default-allow. "
            f"Ensure {rd / CONTEXT_FILE} and {rd / TRACE_FILE} reflect final state."
        ),
        "telemetry": {"status": "disabled", "runtime_dir": str(rd)},
    })


if __name__ == "__main__":
    main()
