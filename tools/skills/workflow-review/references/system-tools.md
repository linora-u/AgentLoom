# Workflow Review — Tool Capability Dynamic Discovery Guide

This document is used to dynamically identify the tool capabilities of a target Application during review, avoiding reliance on fixed tool tables.

## Core Principles

- Do not assume the target project's system tool set matches the current repository
- Do not rely on hardcoded tool counts or fixed names
- Discover capabilities first, then draw conclusions
- All conclusions are based on "effective configuration", not "multiple files listing side by side"

---

## Step 0: Root Directory Prerequisites

The following must be satisfied before executing scans:

- Current directory is the AgentLoom root directory
- Criteria: `config/llm.yaml` exists

If not satisfied, stop immediately and correct the working directory first.

---

## Step 1: Discover Configuration Sources and Compute Effective Values

Collect configuration files along the override chain (low priority -> high priority):

1. Project-level: `<project_root>/config/system.yaml`
2. Application-level: `<app_path>/config/system.yaml`

Computation rules:

- `dict` fields: deep merge
- `list` fields: full replacement (not append)

Must output:

- Effective `default_loaded_tools` (final value)
- Source layer of `default_loaded_tools` (last definition location)
- If the final value is an empty list, explicitly state whether it was "explicitly overridden to empty by upper layer" or "not configured"

---

## Step 2: Discover Mappings and Execution Environment Semantics

### 2.1 Mapping Check

1. First check `tools_mapping.Claude`
2. If empty, check legacy `tools.mapping`
3. If both exist, `tools_mapping.Claude` takes precedence; legacy is only noted as "ignored"

### 2.2 Execution Environment Check

Collect separately for Supervisor/Worker:

- `execution_env.type`

Determine default tool availability based on runtime rules:

- `execution_env.type` is `docker` / `e2b`: default tool list is entirely skipped
- Other types: default tools load according to effective `default_loaded_tools`

---

## Step 3: Discover Agent's Actually Available Capabilities

Collect separately for Supervisor/Worker:

1. Explicit `tools` declarations in YAML/Markdown
2. `worker_agents` path forms and suffixes (`.md` / `.yaml` / `.yml`)
3. `agent_tools/*.py` public functions (function name + docstring summary)

Suggested output format:

```markdown
## Tool Capability Matrix
| Agent | Explicit Tools | Default Tools (Effective) | Mapping Source | execution_env | Custom Tool Capabilities | Notes |
|-------|---------------|--------------------------|---------------|---------------|-------------------------|-------|
```

---

## Step 4: Capability Alignment (Requirements vs Capabilities)

Extract operational intent from the prompt, then map to capability types:

- File reading/searching
- File writing/editing
- Structured parsing (AST/symbols)
- Shell execution/verification
- Formatted output
- External system calls

Evaluation logic:

1. Target capability is covered: Mark as "directly usable"
2. Capability achievable through existing tool composition: Mark as "achievable via orchestration"
3. No coverage: Recommend a new custom Tool and describe the input/output contract

---

## Step 5: When to Recommend Creating a New Tool

Recommend creating a new tool when any of the following conditions are met:

- The prompt has high-frequency deterministic steps with no stable implementation currently available
- Multiple Workers are implementing the same type of deterministic logic repeatedly
- Existing tools are usable but input/output parameters don't fit the current scenario, making Agent-side assembly complex

Suggested output template:

```markdown
[Improvement Recommendation]
- New Tool: <tool_name>
- Target Capability: <capability boundary>
- Input: <key parameters>
- Output: <structured return>
- Impact Scope: <which Workers/Supervisors use it>
```

---

## Review Pitfalls to Avoid

- Judging other projects as "missing tools" based solely on the current repository's default tools
- Asserting default tools are available without confirming `execution_env`
- Misjudging "achievable via composition" as "must create new tool"
- Drawing high-confidence conclusions without configuration evidence
- Ignoring list replacement semantics and treating the base layer `default_loaded_tools` as the final value
