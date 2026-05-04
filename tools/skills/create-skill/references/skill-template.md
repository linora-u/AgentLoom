# Skill Template and Field Reference

## Complete SKILL.md Template

```
---
name: my-skill
description: "Clear description of what the Skill does. Use when: XXX scenario."
version: "1.0.0"
allowed-tools: "Read, Write, Edit, Bash, Glob, Grep"
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
  TaskCompleted:
    - hooks:
        - type: command
          command: python ./scripts/on_task_complete.py
  StopFailure:
    - hooks:
        - type: command
          command: python ./scripts/on_task_fail.py
  PreToolUse:
    - matcher: "Write|Edit|Bash"
      hooks:
        - type: command
          command: python ./scripts/on_pre_tool_use.py
  PostToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_post_tool_use.py
  PostToolUseFailure:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_post_tool_error.py
  SubagentStart:
    - matcher: "*"                  # Optional: match Worker Agent name, "*" matches all
      hooks:
        - type: command
          command: python ./scripts/on_subtask_start.py
  SubagentStop:
    - matcher: "*"                  # Optional: match Worker Agent name
      hooks:
        - type: command
          command: python ./scripts/on_subtask_finish.py
  Stop:
    - hooks:
        - type: command
          command: python ./scripts/on_stop.py
---

# My Skill

Description of Skill functionality.

## Use Cases
- Suitable for...
- Not suitable for...

## Operation Steps
1. First...
2. Then...
3. Finally...

## Notes
- ...
```

---

## YAML Frontmatter Field Reference

| Field | Type | Required | Default | Description |
|------|------|------|--------|------|
| `name` | `string` | No | Directory name | Unique Skill identifier |
| `description` | `string` | Strongly recommended | `""` | LLM uses this to decide whether to use the Skill |
| `version` | `string` | No | `null` | Semantic version number |
| `allowed-tools` | `string` / `list` | No | `null` | List of tools used |
| `hooks` | `dict` | No | `null` | Hook event definitions |

> **Note**: `platform` and `invocation-control` are **not** defined in SKILL.md; they are configured on the referencing side (system.yaml or Agent YAML).

---

## Abstract Tool Name Mapping

It is recommended to use abstract names in `allowed-tools` and `matcher`:

| Abstract Name | Claude Platform Actual Name |
|--------|-------------------|
| `Read` | `read_file` |
| `Write` | `write_markdown_file` |
| `Edit` | `edit_file` |
| `Bash` | `shell_tool` |
| `Glob` | `list_files_glob` |
| `Grep` | `ripgrep_search_directory` |

---

## 9 Hook Events Quick Reference

| Event | Requires matcher | tool_name Value | Typical Use |
|------|:-----------:|:------------:|---------|
| `TaskCreated` | ✖ | `"task"` | Initialize runtime directory |
| `TaskCompleted` | ✖ | `"task"` | Clean up resources, send notifications |
| `StopFailure` | ✖ | `"task"` | Record failure reasons |
| `SubagentStart` | Optional | Worker name | Subtask progress tracking. Matcher can match specific Worker Agent names, e.g., `"worker_a\|worker_b"`; use `"*"` to match all Workers |
| `SubagentStop` | Optional | Worker name | Subtask result logging. Matcher rules same as SubagentStart |
| `PreToolUse` | ✅ | Actual tool name | Validate input, modify parameters |
| `PostToolUse` | ✅ | Actual tool name | Process output, record logs |
| `PostToolUseFailure` | ✅ | Actual tool name | Error handling |
| `Stop` | ✖ | `"final_answer"` | Final state validation |

---

## Referencing Side Configuration Quick Reference (system.yaml / Agent YAML)

### invocation-control

```yaml
skills:
  - path: "skills/my-skill"
    invocation-control:
      allow-model: true              # true (on-demand) / false (hidden) / "force-inject" (force-inject)
      allow-hook: true               # true / false
```

### Three Skill Type Configurations

```yaml
# Force-inject type (core Skill)
- path: "skills/agent-recall-with-files"
  invocation-control:
    allow-model: "force-inject"
    allow-hook: true

# On-demand type (default)
- path: "skills/my-domain-skill"
# invocation-control can be omitted, defaults to allow-model: true, allow-hook: true

# Hidden type (background monitoring)
- path: "skills/agent-visualization"
  invocation-control:
    allow-model: false
    allow-hook: true
```

---

## Minimal Skill (No Hooks, LLM Instructions Only)

```
---
name: coding-standards
description: "Team coding standards. Use when: code review, new feature development reference."
version: "1.0.0"
---

# Coding Standards

## Python Standards
- Use type hints
- Functions should not exceed 50 lines
- ...
```

This type of Skill is best suited as a `force-inject` type, directly injecting the standards into the Agent's system prompt.
