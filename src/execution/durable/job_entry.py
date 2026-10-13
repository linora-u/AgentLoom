"""One OS-owned invocation. Identity handshake precedes all host/Runtime code."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import time
import traceback

import psutil

from .client import TaskDescription
from .files import atomic_json, read_json
from .host import HostSpec, exception_outcomes


def main():
    job = Path(sys.argv[1]).resolve(strict=True)
    if os.getpid() != os.getpgrp():
        raise RuntimeError("OS job must own its process group")
    atomic_json(job / "identity.json", {"pid": os.getpid(), "creation": psutil.Process().create_time(),
                                       "group": os.getpgrp()})
    # No host code, model request or business permission before acknowledgment.
    while not (job / "authorized.json").exists():
        time.sleep(0.02)
    descriptor = read_json(job / "description.json")
    task = TaskDescription.from_dict(descriptor["task"])
    host = HostSpec(**descriptor["host"]).build()
    from agentloom.execution.process_scope import bind_managed_process_scope

    try:
        with bind_managed_process_scope():
            result = host.invoke(task, descriptor["invocation_id"])
        atomic_json(job / "result.json", {"result": result})
    except BaseException as error:
        outcomes = exception_outcomes(error)
        outcome = outcomes[0] if outcomes else None
        atomic_json(job / "result.json", {"error": {"error_type": type(error).__name__, "message": str(error),
            "traceback": "".join(traceback.format_exception(error)),
            "retryable": getattr(error, "retryable", False), "category": getattr(error, "category", "invocation")},
            "outcome": outcome.to_dict() if outcome is not None else None,
            "outcomes": [item.to_dict() for item in outcomes]})
        raise


if __name__ == "__main__":
    main()
