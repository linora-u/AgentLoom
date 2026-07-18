#!/usr/bin/env python3
"""Hook: StopFailure — Emit error event. Delegates to on_task_complete.py logic."""

import sys

from common import output
from on_task_complete import main

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[agent-visualization] on_task_fail error: {exc}", file=sys.stderr)
        output({"decision": "allow"})
