# Per-Agent Shell Security Override Examples

## Scenario 1: Read-Only Audit Agent

Only allows viewing commands, blocks all writes and destructive operations.

```yaml
name: "readonly_auditor"
description: "Read-only code audit agent"
model_type: "powerful"
tool_call_type: "code_act"

# Per-agent config: tools list + shell_settings override
tools:
  - name: "shell_tool"
  - name: "read_file"
  - name: "grep_search"
shell_settings:
  allowed_commands:
    - "ls"
    - "cat"
    - "head"
    - "tail"
    - "grep"
    - "find"
    - "wc"
    - "pwd"
    - "file"
    - "stat"
  allowed_operators: ["|", "&&"]
  block_destructive: true
  allowed_paths: ["."]

workflow: |
  You are a read-only code audit agent. You can only view file contents, not modify them.
```

## Scenario 2: Developer Agent

Relaxes `$()` and `${}` for build scripts, but keeps the safety baseline.

```yaml
name: "developer"
description: "Development and testing agent"
model_type: "powerful"
tool_call_type: "code_act"

tools:
  - name: "shell_tool"
  - name: "edit_file"
shell_settings:
  allowed_commands: "*"
  allowed_operators: "*"
  security_checks:
    command_substitution: false     # Allow $(), needed for build scripts
    parameter_expansion: false      # Allow ${}, needed for variable handling
    dangerous_shell_prefix: true    # Still block sudo
    destructive_patterns: true      # Still block rm -rf /
  allowed_paths: [".", "/tmp"]
  background_tasks:
    stall_threshold_seconds: 30     # Faster stall detection

workflow: |
  You are a development agent. You can write code, run builds and tests.
```

## Scenario 3: Minimal Permission Agent — No Shell

Does not declare `shell_tool`, so the agent cannot execute any shell commands.

```yaml
name: "text_analyzer"
description: "Pure text analysis agent, no shell needed"
model_type: "fast"
tool_call_type: "code_act"

tools:
  - name: "read_file"
  - name: "grep_search"

workflow: |
  You are a text analysis agent. You can only read and search files.
```

## Scenario 4: CI/CD Pipeline Agent

Allows broad commands but restricts to project directory and /tmp.

```yaml
name: "ci_runner"
description: "CI/CD pipeline execution agent"
model_type: "powerful"
tool_call_type: "code_act"

tools:
  - name: "shell_tool"
  - name: "read_file"
shell_settings:
  allowed_commands: "*"
  allowed_operators: "*"
  security_checks:
    command_substitution: false
    parameter_expansion: false
    process_substitution: false
    incomplete_commands: false
  allowed_paths: [".", "/tmp"]
  block_destructive: true
  background_tasks:
    enabled: true
    auto_background_on_timeout: true
    stall_threshold_seconds: 20

workflow: |
  You are a CI/CD agent. Run builds, tests, and deployment scripts.
```

## Override Merge Rules

| Data Type | Merge Behavior |
|-----------|---------------|
| Dictionary (`security_checks`) | Per-key override; undeclared keys keep parent value |
| List (`allowed_commands`, `dangerous_paths`) | Complete replacement |
| Scalar (`block_destructive`, `stall_threshold_seconds`) | Complete replacement |

> Lists are **replaced entirely**, not appended. When overriding `dangerous_paths`, include the full list.
