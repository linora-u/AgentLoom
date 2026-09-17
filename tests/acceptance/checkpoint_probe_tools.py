"""A bounded handoff barrier used only by copied real-checkpoint Applications."""
from __future__ import annotations

import time
from pathlib import Path


def record_checkpoint_worker_output(workspace: str, output: str) -> str:
    """Record a real Worker return and pause until the host permits continuation.

    Args:
        workspace: Dedicated checkpoint acceptance workspace, supplied by the host.
        output: The exact Worker tool return, without rewriting or summarizing it.
    """
    root = Path(workspace).resolve()
    release = root / "handoff_release"
    name = "worker_output_after.txt" if release.exists() else "worker_output_before.txt"
    target = root / name
    # A repeated observed handoff is a test failure, never silently overwritten.
    with target.open("x", encoding="utf-8") as stream:
        stream.write(output)
    deadline = time.monotonic() + 180
    while not release.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("checkpoint handoff was not released within 180 seconds")
        time.sleep(0.2)
    return "Exact Worker return recorded; proceed to Supervisor final verification."
