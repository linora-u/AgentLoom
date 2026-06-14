---
name: script-probe
description: Validate default third-party script execution for shell, Python, and Node.
allowed-tools: run_skill_script, check_skill_dependencies
when_to_use: Use when validating AgentLoom skill script execution.
---

# Script Probe

Run the bundled scripts through `run_skill_script`:

- `sh scripts/shell_probe.sh`
- `python scripts/python_probe.py`
- `node scripts/node_probe.js`

Each script writes an artifact under `$AGENTLOOM_SKILL_WORKSPACE`.
