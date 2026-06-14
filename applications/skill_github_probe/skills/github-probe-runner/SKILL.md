---
name: github-probe-runner
description: Fetch and validate open-source GitHub skills through AgentLoom skill loading and script execution.
allowed-tools: run_skill_script, read_skill_resource
when_to_use: Use when validating third-party GitHub skills against AgentLoom.
---

# GitHub Probe Runner

Run:

```bash
python scripts/fetch_and_validate_skills.py
```

The script clones fixed target repositories, loads each target skill with the
AgentLoom runtime, runs dependency checks, executes runnable scripts, and writes
`applications/skill_github_probe/reports/skill_validation_report.md`.

Target repositories, refs, skill paths, categories, and command expectations are
application-specific and live under `skill_github_probe.targets` in:

```text
applications/skill_github_probe/config/system.yaml
```
