#!/usr/bin/env python3
"""PreToolUse hook for test fixture — reads data files, logs, returns context."""
import json
import os
import sys

skill_dir = os.getcwd()
sys.path.insert(0, os.path.join(skill_dir, "scripts"))
import helper

with open(os.path.join(skill_dir, "data.txt"), "r", encoding="utf-8") as f:
    data = f.read().strip()
with open(os.path.join(skill_dir, "config.json"), "r", encoding="utf-8") as f:
    cfg = json.load(f)

hook_log = os.environ.get("HOOK_LOG")
if hook_log:
    with open(hook_log, "a", encoding="utf-8") as f:
        f.write(f"pre:{data}:{cfg['name']}:{helper.get_value()}\n")

print(json.dumps({
    "decision": "allow",
    "agent_context": "1111\n222\nlyc1111",
}))
