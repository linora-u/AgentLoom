#!/usr/bin/env python3
"""Hook: Stop — allow termination and report final-recording guidance."""

from common import CONTEXT_FILE, HOOK_TAG, TRACE_FILE, get_runtime_agent_path, output, runtime_dir


def main() -> None:
    agent = get_runtime_agent_path()
    rd = runtime_dir(agent)

    output({
        "decision": "allow",
        "reason": (
            f"{HOOK_TAG} Stop default-allow. "
            f"Ensure {rd / CONTEXT_FILE} and {rd / TRACE_FILE} reflect final state."
        ),
        "telemetry": {"status": "disabled", "runtime_dir": str(rd)},
    })


if __name__ == "__main__":
    main()
