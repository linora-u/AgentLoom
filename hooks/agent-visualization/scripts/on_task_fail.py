#!/usr/bin/env python3
"""Hook: StopFailure — delegate error recording to the terminal handler."""

import sys

from common import HOOK_TAG, output
from on_task_complete import main

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{HOOK_TAG} on_task_fail error: {exc}", file=sys.stderr)
        output({"decision": "allow"})
