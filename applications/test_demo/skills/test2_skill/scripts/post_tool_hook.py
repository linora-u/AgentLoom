#!/usr/bin/env python3
"""PostToolUse hook for test fixture — logs and returns context."""
import json
import os

hook_log = os.environ.get("HOOK_LOG")
if hook_log:
    with open(hook_log, "a", encoding="utf-8") as f:
        f.write("post\n")

print(json.dumps({
    "decision": "allow",
    "agent_context": "print-post\nlyc2222",
}))
