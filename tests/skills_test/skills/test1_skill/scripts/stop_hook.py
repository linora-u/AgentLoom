#!/usr/bin/env python3
"""Stop hook for test fixture — logs and returns reason."""
import json
import os

hook_log = os.environ.get("HOOK_LOG")
if hook_log:
    with open(hook_log, "a", encoding="utf-8") as f:
        f.write("stop\n")

print(json.dumps({
    "decision": "allow",
    "reason": "print-stop\nlyc333",
}))
