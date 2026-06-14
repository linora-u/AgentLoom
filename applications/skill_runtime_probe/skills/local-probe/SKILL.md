---
name: local-probe
description: Validate on-demand skill loading, argument passing, bundled resources, and script execution.
allowed-tools: load_skill, read_skill_resource, run_skill_script
argument-hint: "<probe-task>"
arguments: [probe-task]
when_to_use: Use when validating AgentLoom on-demand skill loading.
---

# Local Probe

When this skill is loaded:

1. Acknowledge that this is an on-demand skill.
2. Read `references/guide.md` only if the task asks for resource verification.
3. Run `scripts/write_artifact.sh` only if the task asks for script execution.
4. Report the artifact path from the script audit output.
