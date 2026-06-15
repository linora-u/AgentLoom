---
name: network-locked-probe
description: Validate explicit network command blocking and resource path escape failure.
allowed-tools: run_skill_script, read_skill_resource
when_to_use: Use when validating explicit network restrictions.
---

# Network Locked Probe

This skill may run local scripts, but common network commands are blocked by the
agent YAML config.
