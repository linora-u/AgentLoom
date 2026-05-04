# AgentLoom Skills Configuration Complete Reference

> **Document scope**: This document details how to create a Skill from scratch, including directory structure, all SKILL.md parameters, Hook system, script development, and complete configuration examples.
> For the `skills` field in Agent YAML, see [Agent Configuration Reference](agent_config.md).
> For global skills configuration, see [System Configuration Reference](system_config.md).
> For override relationships between configuration files, see [Configuration System Overview](config-overview.md).

Skills are **reusable Agent capability extension packages** in the AgentLoom framework. Through Skills, you can inject domain knowledge, mount lifecycle Hooks, and execute custom logic before/after tool calls — extending Agent behavior without modifying framework code.

---

## Table of Contents

- [1. What is a Skill & When to Use](#1-what-is-a-skill--when-to-use)
- [2. Skill Directory Structure Specification](#2-skill-directory-structure-specification)
- [3. SKILL.md File Format](#3-skillmd-file-format)
- [4. YAML Frontmatter Field Details](#4-yaml-frontmatter-field-details)
  - [4.1 name — Skill Name](#41-name--skill-name)
  - [4.2 description — Skill Description](#42-description--skill-description)
  - [4.3 version — Version Number](#43-version--version-number)
  - [4.4 allowed-tools — Allowed Tools](#44-allowed-tools--allowed-tools)
  - [4.5 hooks — Hook Event Definitions](#45-hooks--hook-event-definitions)
- [5. Reference Configuration Parameters (system.yaml / Agent YAML)](#5-reference-configuration-parameters-systemyaml--agent-yaml)
  - [5.1 platform — Platform Identifier](#51-platform--platform-identifier)
  - [5.2 invocation-control — Invocation Control and Visibility](#52-invocation-control--invocation-control-and-visibility)
- [6. Hook System Complete Guide](#6-hook-system-complete-guide)
  - [6.1 Hook Definition Syntax](#61-hook-definition-syntax)
  - [6.2 All 9 Hook Events](#62-all-9-hook-events)
  - [6.3 matcher Matching Rules](#63-matcher-matching-rules)
  - [6.4 Hook Registration Timing](#64-hook-registration-timing)
- [7. Hook Script Development Guide](#7-hook-script-development-guide)
  - [7.1 Execution Environment Variables](#71-execution-environment-variables)
  - [7.2 HOOK_CONTEXT_JSON Structure](#72-hook_context_json-structure)
  - [7.3 Output JSON Format (stdout)](#73-output-json-format-stdout)
  - [7.4 decision Three Values](#74-decision-three-values)
  - [7.5 Exit Code Handling Rules](#75-exit-code-handling-rules)
  - [7.6 Hook Execution Flow](#76-hook-execution-flow)
  - [7.7 Common Utility Functions Pattern (common.py)](#77-common-utility-functions-pattern-commonpy)
- [8. Skill Loading and Reference Configuration](#8-skill-loading-and-reference-configuration)
  - [8.1 Three-Layer Loading Mechanism](#81-three-layer-loading-mechanism)
  - [8.2 Skill Reference Syntax](#82-skill-reference-syntax)
  - [8.3 Tool Name Mapping](#83-tool-name-mapping)
- [9. Creating a Skill from Scratch Tutorial](#9-creating-a-skill-from-scratch-tutorial)
- [10. Built-in Skills](#10-built-in-skills)
  - [10.1 agent-recall-with-files](#101-agent-recall-with-files)
  - [10.2 agent-visualization](#102-agent-visualization)
- [11. load_skill() and list_skills() API](#11-load_skill-and-list_skills-api)
- [12. Complete Configuration Examples](#12-complete-configuration-examples)
- [13. FAQ](#13-faq)
- [Appendix: Field Quick Reference Table](#appendix-field-quick-reference-table)

---

## 1. What is a Skill & When to Use

### 1.1 Three Types

The framework divides Skills into three types through the `invocation-control.allow-model` tri-state parameter (see [Section 5.2](#52-invocation-control--invocation-control-and-visibility)):

| Type | `allow-model` Value | LLM Awareness | Typical Use |
|------|-------------------|-------------|----------|
| **Force-inject Skill** | `"force-inject"` | Complete instructions embedded in system prompt during Agent initialization; LLM **always follows**, no `load_skill()` needed | Memory systems, security specifications, core capabilities |
| **On-demand Skill** | `true` (default) | Appears in skill catalog (`<available_skills>`); LLM decides when to call `load_skill()` | Domain operation guides, workflow specifications |
| **Hidden Skill** | `false` | LLM completely unaware of the Skill; runs silently through Hooks only | Event collection, visualization, transparent monitoring |

### 1.2 Use Cases

**Recommended to use Skills when:**
- Need to reuse the same set of operational specifications across multiple Agents or workflows
- Need custom logic before/after tool calls (path validation, logging, input rewriting)
- Need cross-session persistence of Agent experience and state
- Need automatic operations triggered at specific Agent lifecycle moments

**No need for Skills when:**
- Just a one-time simple task, no reuse needed
- Logic is very simple, can be written directly in the Agent's `workflow` field
- No tool interception or lifecycle operations involved

---

## 2. Skill Directory Structure Specification

A complete Skill directory structure looks like:

```
skills/
└── my-skill/                    # Skill directory, name serves as default name
    ├── SKILL.md                 # 【Required】Core definition file
    ├── scripts/                 # 【Recommended】Hook script directory
    │   ├── common.py            #   Shared utility functions (for other Hook scripts to import)
    │   ├── on_task_start.py     #   TaskCreated event Hook
    │   ├── on_task_complete.py  #   TaskCompleted event Hook
    │   ├── on_task_fail.py      #   StopFailure event Hook
    │   ├── on_pre_tool_use.py   #   PreToolUse event Hook
    │   ├── on_post_tool_use.py  #   PostToolUse event Hook
    │   ├── on_subtask_start.py  #   SubagentStart event Hook
    │   └── on_subtask_finish.py #   SubagentStop event Hook
    ├── templates/               # 【Optional】Runtime file templates
    │   ├── context.md           #   context.md initial template
    │   └── trace.md             #   trace.md initial template
    └── references/              # 【Optional】Supplementary reference docs
        ├── examples.md          #   Usage examples (for LLM reference)
        └── reference.md         #   Quick reference manual
```

### File/Directory Descriptions

| File/Directory | Required | Description |
|-----------|---------|------|
| `SKILL.md` | **Required** | Contains YAML frontmatter (metadata) and Markdown body (LLM instructions). The framework discovers Skills by scanning for this file |
| `scripts/` | Recommended (required if Hooks exist) | Houses Hook scripts. The `./` in Hook commands like `python ./scripts/xxx.py` is relative to the **Skill directory** (where `SKILL.md` resides) |
| `scripts/common.py` | Recommended | Encapsulates utility functions shared across multiple Hooks (reading env vars, outputting JSON, file operations), avoiding code duplication |
| `templates/` | Optional | Stores initial templates for runtime files. Can be read and written to `.runtime/` directory at `TaskCreated` |
| `references/` | Optional | Human-readable reference docs. Can be referenced as links in SKILL.md body; LLM reads them on demand |

> **Note**: The framework recursively scans directories, identifying files named `skill.md` or `skills.md` (case-insensitive, including `SKILL.md`/`SKILLS.MD`). The subdirectory name serves as the default Skill name.

---

## 3. SKILL.md File Format

The SKILL.md file consists of two parts:

```
---
# YAML Frontmatter (metadata, parsed by framework)
name: my-skill
description: "Describe what this Skill does"
version: "1.0.0"
allowed-tools: "Read, Write, Bash"
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
---

# Markdown Body (LLM instructions, returned by load_skill())

# My Skill

Write operational guides for the LLM here. When the LLM calls load_skill("my-skill"),
the content here is returned in full, and the LLM must follow these instructions.

## Use Cases
...

## Steps
1. First...
2. Then...
```

---

## 4. YAML Frontmatter Field Details

All following fields are defined within the `---` fences at the top of the `SKILL.md` file.

### 4.1 name — Skill Name

| Item | Description |
|------|------|
| Type | `string` |
| Required | No |
| Default | Skill directory folder name |

Uniquely identifies this Skill in the global Skill registry. Used when the LLM calls `load_skill("<name>")`.

**Parsing rule**: If the field is missing, not a string, or an empty string, the framework automatically uses the directory name where `SKILL.md` resides.

```yaml
# Explicitly specify name (can differ from directory name)
name: my-custom-skill

# Omit to default to directory name, e.g., directory skills/my-skill/ → name = "my-skill"
```

> ⚠️ **Name conflicts**: Two Skills with the same name cannot coexist in an Agent's Skill view. If override occurs, the framework outputs a warning and uses the later-loaded Skill.

---

### 4.2 description — Skill Description

| Item | Description |
|------|------|
| Type | `string` |
| Required | **Strongly recommended** |
| Default | `""` (empty string) |

Displayed in the LLM-visible skill catalog. The LLM decides whether to use this Skill based on this description. **The clearer the description, the better the LLM can use the Skill at the right time.**

Supports YAML multi-line syntax (`|` or `>`):

```yaml
# Single line
description: "Cross-session memory system, persisting Agent experience and insights through files."

# Multi-line (using | to preserve newlines)
description: |
  Cross-session file memory system.
  Suitable for: multi-step tasks, resuming interrupted tasks, learning from historical experience.
  Not suitable for: simple one-time tasks.
```

**Parsing rule**: Non-string type values are silently converted to empty strings.

---

### 4.3 version — Version Number

| Item | Description |
|------|------|
| Type | `string` |
| Required | No |
| Default | `null` |

Semantic version number, used for documentation only. The framework does not enforce version constraints or compatibility checks.

```yaml
version: "1.0.0"
version: "2.3.1"
```

---

### 4.4 allowed-tools — Allowed Tools

| Item | Description |
|------|------|
| Type | `string` or `list[string]` |
| Required | No |
| Default | `null` |

Declares which tools this Skill will use. During loading, abstract tool names (e.g., `Read`) are mapped to actual tool names (e.g., `read_file`) via `tools_mapping`.

**String format** (comma, pipe, or space separated — all three equivalent):

```yaml
allowed-tools: "Read, Write, Bash"
allowed-tools: "Read|Write|Bash"
allowed-tools: "Read Write Bash"
```

**List format**:

```yaml
allowed-tools:
  - "Read"
  - "Write"
  - "Bash"
```

> **Tip**: Use abstract tool names (`Read`/`Write`/`Bash`/`Glob`/`Grep`/`Edit`) with `tools_mapping` to make Skills automatically adapt to actual tool names across different platforms. See [Tool Name Mapping](#83-tool-name-mapping) for specific mappings.

---

### 4.5 hooks — Hook Event Definitions

| Item | Description |
|------|------|
| Type | `dict` |
| Required | No |
| Default | `null` |

Defines which lifecycle events this Skill attaches scripts to. Keys are event names (corresponding to `HookEvent` enum values), values are hook definition lists.

See [Section 6: Hook System Complete Guide](#6-hook-system-complete-guide) for detailed syntax.

```yaml
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
  PreToolUse:
    - matcher: "Write|Edit|Bash"
      hooks:
        - type: command
          command: python ./scripts/on_pre_tool_use.py
```

---

## 5. Reference Configuration Parameters (system.yaml / Agent YAML)

The following parameters are **not defined within SKILL.md** but specified when referencing the Skill (in `config/system.yaml` or Agent YAML `skills:` field).

### 5.1 platform — Platform Identifier

| Item | Description |
|------|------|
| Type | `string` |
| Default | `"Claude"` |
| Set Location | `config/system.yaml` or Agent YAML `skills:` entry |

Specifies which platform's tool name mapping this Skill uses. Used to convert abstract tool names in `allowed-tools` and Hook `matcher` to actual tool function names.

```yaml
skills:
  - path: "skills/my-skill"
    platform: "Claude"    # Default, uses Claude platform tool mapping
```

Tool name mapping relationships are defined under `tools_mapping` in `config/system.yaml`. See [Tool Name Mapping](#83-tool-name-mapping).

> **Note**: `platform` can only be set in reference configuration (system.yaml or Agent YAML `skills:` entries), **not** in SKILL.md frontmatter (the framework does not parse the `platform` field from frontmatter). Defaults to `"Claude"` when not specified.

---

### 5.2 invocation-control — Invocation Control and Visibility

| Item | Description |
|------|------|
| Type | `dict` (nested object) |
| Required | No |
| Default | `{"allow-model": true, "allow-hook": true}` |
| Set Location | `config/system.yaml` or Agent YAML `skills:` entry |

Precisely controls skill visibility, loading strategy, and Hook permissions through nested options. This field is configured on the **reference side** (system.yaml or Agent YAML `skills:` entries), not in SKILL.md.

```yaml
skills:
  - path: "skills/agent-recall-with-files"
    invocation-control:
      allow-model: "force-inject"
      allow-hook: true
```

#### allow-model — Tri-State Control

`allow-model` supports three values, uniformly controlling the relationship between LLM and the Skill:

| Value | Meaning | In `<available_skills>` Catalog | `load_skill()` Behavior | System Prompt Injection |
|------|------|------|------|------|
| `true` (default) | **On-demand** — LLM visible, must actively call `load_skill()` | ✅ Appears | ✅ Returns full instructions | ❌ Not injected |
| `false` | **Hidden from LLM** — LLM completely unaware of the Skill | ❌ Not shown | ❌ Returns error | ❌ Not injected |
| `"force-inject"` | **Force-inject** — Skill instructions embedded in system prompt at Agent initialization | ❌ Not shown (already in prompt) | ⚠️ Returns dedup notice | ✅ Injected into `<force_injected_skills>` |

#### allow-hook — Boolean Switch

- `allow-hook: true` (default): Hooks register and trigger normally.
- `allow-hook: false`: This Skill's Hooks are **not registered** and will not be triggered.

`allow-hook` and `allow-model` are **orthogonal dimensions** — even if `allow-model: false` (LLM invisible), as long as `allow-hook: true`, Hooks still execute in the background.

#### Examples

```yaml
skills:
  # Passive Skill: LLM unaware, only runs via Hooks in background
  - path: "skills/agent-visualization"
    invocation-control:
      allow-model: false
      allow-hook: true

  # Core Skill: Full instructions force-injected into system prompt
  - path: "skills/agent-recall-with-files"
    invocation-control:
      allow-model: "force-inject"
      allow-hook: true

  # Regular Skill: LLM loads on demand (default behavior, invocation-control can be omitted)
  - path: "skills/my-skill"
```

**Fault-tolerant parsing**:
- `allow-model` supports lenient parsing: `true`/`"true"`/`"yes"`/`"on"`/`"y"`/`1` → `true`; `false`/`"false"`/`"no"`/`"off"`/`"n"`/`""`/`0` → `false`; `"force-inject"`/`"force_inject"`/`"inject"` → `"force-inject"` (case-insensitive).
- `allow-hook` uses the same lenient boolean parsing rules.
- Both default to `true` when not specified.

> **💡 Recommended: use `"force-inject"`**
>
> Analysis shows that when `allow-model: true` (on-demand), if the Skill's `description` isn't precise enough, the LLM may **not proactively call `load_skill()`**, instead writing its own code to handle tasks the Skill was meant for — bypassing the Skill's preset operational specifications.
>
> Setting critical Skills to `allow-model: "force-inject"` avoids this: Skill instructions are embedded directly in the system prompt at Agent initialization, so the LLM **doesn't need to decide whether to call** and will definitely follow the Skill instructions.
>
> **Recommended strategy**:
> - Core Skills (e.g., memory system, security specs) → `"force-inject"`, ensuring LLM always follows
> - Domain helper Skills (e.g., specific API operation guides) → `true` (on-demand), saving tokens
> - Background monitoring Skills (e.g., event collection) → `false`, completely transparent to LLM

---

## 6. Hook System Complete Guide

### 6.1 Hook Definition Syntax

```yaml
hooks:
  <EventName>:             # Event name, see event list below
    - matcher: "<pattern>" # Optional, defaults to "*" (matches all tools)
      hooks:
        - type: command    # Currently only "command" type is supported
          command: "python ./scripts/on_xxx.py"  # Execute command, cwd = Skill directory
```

Each event can have multiple hook definitions (list), each containing multiple `hooks` entries.

**Key points**:
- `matcher` field: Filters which tool names trigger this Hook; lifecycle events (TaskCreated, etc.) don't need matcher. All events default `matcher` to `"*"` (match all); explicitly setting to `null` also converts to `"*"`
- `type: command` is currently the framework's **only supported Hook type**
- `command` execution working directory (`cwd`) is fixed to the **Skill directory** (where `SKILL.md` resides), so `./scripts/xxx.py` path resolution works correctly
- Hook script execution has a **timeout limit** (default 20 seconds). After timeout, the script process is terminated and the Hook fails with `block` decision. Custom timeout can be set via `timeout` field in hook action (unit: seconds), e.g., `timeout: 60`

---

### 6.2 All 9 Hook Events

The framework supports the following 9 Hook events (from `HookEvent` enum):

| Event Name | YAML Key | Trigger Timing | Needs matcher | `tool_name` Value | Typical Use |
|--------|-----------|---------|-------------|---------------|---------|
| Task Start | `TaskCreated` | When Agent begins task execution | No | `"task"` | Initialize runtime directory, read historical state |
| Task Complete | `TaskCompleted` | When task completes successfully | No | `"task"` | Clean up resources, send notifications, record results |
| Task Fail | `StopFailure` | When task execution fails | No | `"task"` | Record failure reasons, clean up state |
| Subtask Start | `SubagentStart` | When Worker Agent is activated | Optional (defaults to `"*"`, matches all) | Worker Agent name | Subtask progress tracking |
| Subtask Finish | `SubagentStop` | When Worker Agent completes | Optional (defaults to `"*"`, matches all) | Worker Agent name | Subtask result logging |
| Pre Tool Use | `PreToolUse` | **Before** tool execution | Yes (tool name) | Actual tool function name | Validate input, modify parameters, inject context |
| Post Tool Use | `PostToolUse` | After tool **successfully executes** | Yes (tool name) | Actual tool function name | Process output, record logs |
| Post Tool Error | `PostToolUseFailure` | When tool **execution throws** | Yes (tool name) | Actual tool function name | Error handling, rollback operations |
| Stop | `Stop` | When Agent prepares to give final answer | No | `"final_answer"` | Final state validation, ensure work completion |

> **Concurrent execution note**: When the application layer uses `tool.batch()` to batch-invoke the same Worker concurrently, `SubagentStart` and `SubagentStop` events are **triggered independently in each concurrent Worker instance**, each carrying its own unique `sub_task_id`. If Hook scripts need to write to shared files or global state, they should handle concurrent write safety themselves (e.g., write to separate files keyed by `sub_task_id`, or use file locks).

---

### 6.3 matcher Matching Rules

The `matcher` value uses Python `re.fullmatch()` for **strict full-string matching**, while also natively supporting standard regex syntax (e.g., `.*` or `|`).

| matcher Pattern | Matching Behavior |
|-------------|---------|
| `"*"` | Matches **all tools** (special value, bypasses regex, direct pass-through) |
| `"shell_tool"` | **Strictly and only matches** the tool named `shell_tool`, won't match `shell_tool_extra` |
| `"Write\|Edit\|Bash"` | Matches one of these three tools (full match) |
| `".*read.*"` | Any regex, matches any tool name containing `read` |

**Matching judgment priority**: The framework checks in this order:

1. If matcher is `"*"` → **direct pass-through** (no regex)
2. If matcher **exactly equals** the tool name → match
3. Otherwise use `re.fullmatch(pattern, tool_name)` for **regex full-string matching**; if regex syntax is invalid, output warning and skip

> **Note**: `matcher` uses the tool's **actual name** (after `tools_mapping` mapping). If `platform: "Claude"` is configured, abstract names like `"Write|Edit"` in SKILL.md are automatically mapped to `"write_markdown_file|edit_file"` during loading.

---

### 6.4 Hook Registration Timing

The framework uses a two-phase registration strategy:

1. **Eager registration**: When the framework scans `SKILL.md` and parses metadata, **all 9 events' Hooks are immediately registered** to `HookManager`. This ensures lifecycle and low-level error interception Hooks don't miss trigger opportunities, and passive Skills (disallowed for model loading) can properly capture events.

2. **Lazy loading**: The Skill's Markdown body (LLM instructions) is only read on the first `load_skill()` call. But since all events were registered in step one, lazy loading doesn't affect Hook triggering.

---

## 7. Hook Script Development Guide

### 7.1 Execution Environment Variables

Hook scripts are executed via Shell; the framework injects the following **5 environment variables** into the subprocess before execution:

| Environment Variable | Description | Default | Example Value |
|---------|------|--------|--------|
| `AGENT_NAME` | Name of the currently executing Agent | `"default"` | `"supervisor_agent"` |
| `TASK_ID` | Unique ID of the current task | `""` (empty string) | `"task_abc123"` |
| `TOOL_NAME` | Tool name that triggered this Hook | `""` (empty string) | `"shell_tool"` |
| `HOOK_EVENT` | Event name | `""` (empty string) | `"PreToolUse"` |
| `HOOK_CONTEXT_JSON` | Complete context information (JSON string) | `"{}"` | See next section |

> **Working directory**: Hook script execution `cwd` is always the **Skill directory** (where `SKILL.md` resides), so relative paths in scripts (e.g., `./scripts/xxx.py`) resolve based on the Skill directory.

---

### 7.2 HOOK_CONTEXT_JSON Structure

`HOOK_CONTEXT_JSON` is a JSON-serialized string containing these fields:

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "cwd": "/home/user/AgentLoom",
  "hook_event_name": "PreToolUse",
  "tool_name": "shell_tool",
  "tool_input": {
    "command": "ls -la"
  },
  "tool_response": null
}
```

| Field | Description |
|------|------|
| `session_id` | Unique session identifier (UUID) |
| `cwd` | Working directory at execution time |
| `hook_event_name` | Event name, same as `HOOK_EVENT` env var |
| `tool_name` | Tool name, same as `TOOL_NAME` env var |
| `tool_input` | Complete tool call input parameters |
| `tool_response` | Tool execution result, varies by event (see details below) |

**`tool_response` values per event**:

| Event | `tool_response` Value | Description |
|------|-------------------|------|
| `PreToolUse` | `null` | Tool not yet executed |
| `PostToolUse` | `{"result": <tool return value>}` | Successful tool execution result |
| `PostToolUseFailure` | `{"error": "<exception message>", "error_type": "<exception class>"}` | Tool execution exception |
| `TaskCompleted` | `{"result": <final task result>}` | Task successful completion output |
| `StopFailure` | `{"error": "<error info>", "error_type": "<exception class>"}` | Task failure exception info |
| `Stop` | `{"memory_steps": <step count>}` or `null` | Has value when `memory.steps` is a list, null otherwise |
| `TaskCreated` | `null` | — |
| `SubagentStart` | `null` | — |
| `SubagentStop` | `null` | tool_input additionally contains `"success": true/false`; failure also includes `"error": "<exception message>"` |

**Reading context in Python scripts**:

```python
import json
import os

context = json.loads(os.environ.get("HOOK_CONTEXT_JSON", "{}"))
tool_input = context.get("tool_input", {})
agent_name = os.environ.get("AGENT_NAME", "default")
```

---

### 7.3 Output JSON Format (stdout)

Hook scripts communicate results to the framework by **printing a JSON object to stdout**. Supports the following **7 fields** (any other key in output is treated as a contract violation, causing the Hook to fail with `block` decision):

```json
{
  "decision": "allow",
  "modified_input": { "key": "new tool input parameters" },
  "modified_response": { "result": "modified tool output" },
  "agent_context": "text to inject into Agent system prompt",
  "user_message": "message to display to the user",
  "reason": "reason description (for block error description)",
  "telemetry": { "custom_key": "custom telemetry data" }
}
```

| Field | Type | Description |
|------|------|------|
| `decision` | `string` | Optional, defaults to `"allow"`. Must be `"allow"`, `"block"`, or `"modify"` (see next section) |
| `modified_input` | `dict` | Tool input fields to override (only effective with `decision: "modify"`, merged onto original input, only write fields to change) |
| `modified_response` | `dict` | Modified tool output (only effective with `decision: "modify"`) |
| `agent_context` | `string` | Extra context appended to Agent system prompt, for injecting memory, state, etc. |
| `user_message` | `string` | Message sent to user/interface (passed through to user_message_sink) |
| `reason` | `string` | Explains the interception or handling reason. Recommended for `block`; system provides defaults if not filled. Optional for `allow`/`modify` |
| `telemetry` | `dict` | Custom telemetry/debug data, written to logs |

---

### 7.4 decision Three Values

| Value | Meaning |
|------|------|
| `"allow"` | Allow the current operation to continue |
| `"block"` | Prevent the current stage from continuing. Actual effect depends on which event the Hook is in |
| `"modify"` | Adjust input or output before continuing. `PreToolUse` can modify input; `PostToolUse` can modify output |

#### `decision: "block"` Actual Effects in Different Events

`block` doesn't always mean "prevent tool execution". Its effect depends on the Hook's trigger timing:

| Event | Actual Effect | Understanding |
|------|-------------|------|
| **`PreToolUse`** | Can directly prevent tool execution | Suitable for pre-validation, permission control, risk interception |
| **`PostToolUse`** | Does NOT undo completed tool execution, but can prevent results from propagating further | Suitable for secondary judgment on results or restricting returned content |
| **`PostToolUseFailure`** | Does not change the original error propagation result | Mainly for supplementary logging, state cleanup, appending context |
| **`Stop`** | Can prevent Agent from giving final answer | Suitable for final checks, ensuring necessary steps are completed |
| **`TaskCreated`**, **`TaskCompleted`**, **`StopFailure`**, **`SubagentStart`**, **`SubagentStop`** | Does NOT interrupt the task main flow, but stops subsequent Hooks from executing | Suitable for initialization, logging, notifications, state organization |

---

### 7.5 Exit Code Handling Rules

The framework determines the final `HookResult` based on the script's **exit code** and **stdout content** combination:

| stdout Content | Exit Code | Result |
|------------|--------|------|
| Empty | `0` | ✅ Default `allow`, execution successful |
| Empty | Non-`0` | ❌ `block`, reason: stderr content or "exit code N" |
| Valid JSON | `0` | ✅ Execute per JSON `decision` |
| Valid JSON | Non-`0` | ❌ Force `block`, ignore JSON `decision` |
| Non-JSON string | Any | ❌ `block`, prompt "output must be JSON" |
| JSON with unknown keys | `0` | ❌ `block`, prompt "unsupported fields" |

---

### 7.6 Hook Execution Flow

When an event triggers, the framework **sequentially** executes all Hooks matching that event + matcher in registration order:

1. Execute matching Hooks in registration order. **Cross-Skill execution order depends on Skill loading order**: Global Skills (`config/system.yaml`) → Auto-discovered Skills (`AGENT_ROOT/skills/`) → Agent-level Skills (Agent YAML); within the same Skill, multiple Hooks execute in YAML declaration order
2. If any Hook returns `block` → **immediate interruption**, subsequent Hooks are not executed
3. If any Hook returns `modify` → passes `modified_input` to subsequent Hooks and tool execution
4. **`agent_context` accumulates**: Multiple Hooks' `agent_context` are concatenated with newlines, all injected into Agent prompt
5. **`user_message` accumulates**: Same as above, all messages are sent

---

### 7.7 Common Utility Functions Pattern (common.py)

Recommend encapsulating common functions in `scripts/common.py`, referencing `agent-recall-with-files` implementation:

```python
# scripts/common.py
import json
import os
from pathlib import Path


def get_agent_name() -> str:
    """Get current Agent name from environment variable, default 'default'"""
    return os.environ.get("AGENT_NAME", "") or "default"


def get_tool_name() -> str:
    """Get current tool name from environment variable"""
    return os.environ.get("TOOL_NAME", "") or "unknown"


def get_hook_context() -> dict:
    """Parse HOOK_CONTEXT_JSON environment variable to dict"""
    raw = os.environ.get("HOOK_CONTEXT_JSON", "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def get_tool_input() -> dict:
    """Extract tool_input from Hook context"""
    ti = get_hook_context().get("tool_input")
    return ti if isinstance(ti, dict) else {}


def output(result: dict) -> None:
    """Print JSON result to stdout (framework reads HookResult from stdout)"""
    print(json.dumps(result, ensure_ascii=False))


def runtime_dir(agent_name: str) -> Path:
    """Return <agent_loom_root>/.runtime/<agent_name> path"""
    # scripts/ → my-skill/ → skills/ → AgentLoom/
    candidate = Path(__file__).resolve().parent.parent.parent.parent
    if (candidate / "pyproject.toml").exists():
        root = candidate
    else:
        root = Path(os.environ.get("AGENT_LOOM_RUNTIME_ROOT", Path.cwd()))
    return root / ".runtime" / agent_name
```

In Hook scripts (note adding `sys.path` for imports):

```python
# scripts/on_task_start.py
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))  # Add scripts/ directory to Python path
from common import get_agent_name, runtime_dir, output


def main():
    agent = get_agent_name()
    rd = runtime_dir(agent)
    rd.mkdir(parents=True, exist_ok=True)

    output({
        "decision": "allow",
        "agent_context": f"[my-skill] Runtime directory ready at {rd}",
        "telemetry": {"runtime_dir": str(rd)},
    })


if __name__ == "__main__":
    main()
```

---

## 8. Skill Loading and Reference Configuration

### 8.1 Three-Layer Loading Mechanism

The framework loads Skills in this order; later-loaded Skills with the same name override earlier ones (with a warning):

```
Layer 1: Global Skills (skills: field in config/system.yaml)
           ↓ Shared by all Agents
Layer 2: Auto-discovery (recursive scan for `skill.md`/`skills.md` in AGENT_ROOT/skills/)
           ↓ Shared by all Agents (auto-loaded)
Layer 3: Agent-level Skills (skills: field in current Agent YAML)
           ↓ Current Agent only
```

> `AGENT_ROOT` is the project root directory containing `config/system.yaml` (`C.agent_root`), not the directory of the Agent YAML file.

---

### 8.2 Skill Reference Syntax

#### Config Format Overview

The `skills` field supports three formats; the framework internally normalizes all to lists:

**Format 1: Single string (simplest)**

```yaml
skills: "skills/my-skill"
# Equivalent to: skills: [{path: "skills/my-skill"}]
# platform defaults to "Claude"
```

**Format 2: Single dictionary**

```yaml
skills:
  path: "skills/my-skill"
  platform: "Claude"        # Can be omitted, defaults to "Claude"
```

**Format 3: List (recommended, can mix strings and dicts)**

```yaml
skills:
  - "skills/skill-a"                       # String item, platform defaults to "Claude"
  - path: "skills/skill-b"                # Dict item, platform defaults to "Claude"
  - path: "skills/skill-c"
    platform: "GPT"                        # Explicitly specify platform
```

> Dictionary and string formats are automatically converted to single-element lists. When `platform` is not specified, it defaults to `"Claude"`, used for abstract-to-actual tool name mapping in `tools_mapping`.

#### Field Description

| Sub-field | Type | Default | Required | Description |
|--------|------|--------|------|------|
| `path` | `string` | — | ✅ Required | Skill directory path. Relative paths resolved based on `AGENT_ROOT`; absolute paths also supported |
| `platform` | `string` | `"Claude"` | ❌ Optional | Tool name mapping platform (can only be set here, not supported in SKILL.md frontmatter) |

#### Common Usage Examples

**Minimal configuration (path only)**:

```yaml
skills:
  - path: "skills/agent-recall-with-files"
```

**Full configuration (with all options)**:

```yaml
skills:
  - path: "skills/agent-recall-with-files"  # Relative to AGENT_ROOT or absolute path
    platform: "Claude"                       # Tool name mapping platform (default "Claude")
```

**Path resolution rules**:
- **Relative path**: Resolved relative to `AGENT_ROOT` (project root directory containing `config/system.yaml`)
- **Absolute path**: Used directly

**Global configuration in `config/system.yaml`** (applies to all Agents):

```yaml
# config/system.yaml
skills:
  - path: "skills/agent-recall-with-files"
  - path: "skills/agent-visualization"
```

**Local configuration in Agent YAML** (applies only to that Agent):

```yaml
# applications/my-app/workflows/my-agent.yaml
skills:
  - path: "skills/my-domain-skill"
  - path: "applications/my-app/skills/local-skill"
    platform: "Claude"
```

---

### 8.3 Tool Name Mapping

To decouple Skill definitions from concrete tool functions, the framework defines abstract-to-actual tool name mappings via `tools_mapping` in `config/system.yaml`:

```yaml
# config/system.yaml
tools:
  tools_mapping:
    Claude:
      Read:  "read_file"
      Write: "write_markdown_file"
      Bash:  "shell_tool"
      Glob:  "list_files_glob"
      Grep:  "ripgrep_search_directory"
      Edit:  "edit_file"
```

**Mapping scope**: When loading Skills, the framework automatically applies mapping to:

1. Tool names in the `allowed-tools` field (`Read` → `read_file`)
2. Tool names in Hook `matcher` (`"Write|Edit"` → `"write_markdown_file|edit_file"`)

Therefore, using abstract names in SKILL.md is recommended — the Skill works on any platform supporting that mapping:

```yaml
# SKILL.md — Use abstract names (recommended)
allowed-tools: "Read, Write, Edit, Bash, Glob, Grep"
hooks:
  PreToolUse:
    - matcher: "Write|Edit|Bash|Read|Glob|Grep"  # Auto-mapped to actual tool names during loading
      hooks:
        - type: command
          command: python ./scripts/on_pre_tool_use.py
```

---

## 9. Creating a Skill from Scratch Tutorial

This tutorial creates a **"task-logger"** Skill that automatically logs at task start and end.

### Step 1: Create Directory Structure

```bash
mkdir -p skills/task-logger/scripts
```

### Step 2: Write SKILL.md

```
---
name: task-logger
description: "Automatically logs task start/completion time to log file. Applicable to all scenarios requiring task execution tracking."
version: "1.0.0"
allowed-tools: "Write, Bash"
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
          command: python ./scripts/on_task_complete.py
---

# Task Logger

This Skill automatically records timestamps to .logs/task_log.txt at task start and completion.

You don't need to do anything manually — Hooks automatically log in the background. Just focus on completing your task.
```

### Step 3: Write common.py

```python
# skills/task-logger/scripts/common.py
import json
import os
from pathlib import Path


def get_agent_name() -> str:
    return os.environ.get("AGENT_NAME", "") or "default"


def get_hook_event() -> str:
    return os.environ.get("HOOK_EVENT", "") or "Unknown"


def get_log_path() -> Path:
    """Write log files under .logs/ in the project root"""
    candidate = Path(__file__).resolve().parent.parent.parent.parent
    root = candidate if (candidate / "pyproject.toml").exists() else Path.cwd()
    log_dir = root / ".logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "task_log.txt"


def output(result: dict) -> None:
    print(json.dumps(result, ensure_ascii=False))
```

### Step 4: Write on_task_start.py

```python
# skills/task-logger/scripts/on_task_start.py
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from common import get_agent_name, get_log_path, output
from datetime import datetime


def main():
    agent = get_agent_name()
    log_path = get_log_path()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] [START] Agent: {agent}\n")
    output({
        "decision": "allow",
        "agent_context": f"[task-logger] Task logged to {log_path}",
        "telemetry": {"logged_at": timestamp},
    })


if __name__ == "__main__":
    main()
```

### Step 5: Write on_task_complete.py

```python
# skills/task-logger/scripts/on_task_complete.py
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from common import get_agent_name, get_hook_event, get_log_path, output
from datetime import datetime


def main():
    agent = get_agent_name()
    event = get_hook_event()   # "TaskCompleted" or "StopFailure"
    log_path = get_log_path()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = "COMPLETE" if event == "TaskCompleted" else "FAIL"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] [{status}] Agent: {agent}\n")
    output({
        "decision": "allow",
        "telemetry": {"logged_at": timestamp, "status": status},
    })


if __name__ == "__main__":
    main()
```

### Step 6: Register to Agent or Global Configuration

**Option A: Apply only to a specific Agent** (in Agent YAML):

```yaml
# applications/my-app/workflows/my-agent.yaml
name: "my_agent"
skills:
  - path: "skills/task-logger"
```

**Option B: Apply globally to all Agents** (in system.yaml):

```yaml
# config/system.yaml
skills:
  - path: "skills/task-logger"
```

### Step 7: Verify Execution

Run the Agent and check the logs:

```bash
cat .logs/task_log.txt
# [2026-03-22 10:30:00] [START] Agent: my_agent
# [2026-03-22 10:35:42] [COMPLETE] Agent: my_agent
```

---

## 10. Built-in Skills

### 10.1 agent-recall-with-files

> ⚠️ **Weak-LLM Compatibility Note**: This skill is **disabled by default** (commented out in `config/system.yaml`). Its mechanism — appending recall prompts (context.md full text, trace.md tail 20 lines, insights.md tail 30 lines) at the end of tool result messages via `PreToolUse`/`PostToolUse` lifecycle hooks — causes **attention sparsity** in weaker LLMs. These hook outputs are wrapped in `<system-reminder>` tags by the framework-level `HookManager` (a generic mechanism for all hooks, not specific to this skill). The appended instructions are often ignored, or they fragment the LLM's semantic understanding of consecutive tool call results, degrading subsequent decision quality. **Enable it manually only when using strong LLMs** (e.g., Claude Sonnet/Opus, GPT-4o) that can reliably attend to appended system reminders.

**Path**: `skills/agent-recall-with-files/`
**Version**: 6.0.0
**Type**: Active (LLM visible)

**Function**: Cross-session file memory system. Maintains 3 runtime files per Agent, enabling task state persistence and experience accumulation.

#### Runtime Files (located at `<agent_loom_root>/.runtime/<agent_name>/`)

| File | Lifecycle | Purpose |
|------|---------|------|
| `context.md` | Reset each task | Task objectives, current state snapshot, remaining work. For quick recovery after task interruption |
| `trace.md` | Reset each task | Chronological action logs, append-only |
| `insights.md` | **Permanently preserved** | Cross-session experience: pitfall records, decision rationale, key facts. Never automatically cleared |

#### insights.md Tag System

Entries recorded in `insights.md` should use these tags:

| Tag | Purpose | Example |
|------|------|------|
| `[pitfall]` | Pitfall records | `[2026-03-22] [pitfall] This API must call init() first, otherwise returns null` |
| `[decision]` | Important decisions | `[2026-03-22] [decision] Chose async approach due to performance requirements` |
| `[fact]` | Key facts | `[2026-03-22] [fact] Config file located at config/llm.yaml` |
| `[dependency]` | Dependencies | `[2026-03-22] [dependency] Module B depends on A being initialized first` |
| `[perf]` | Performance-related | `[2026-03-22] [perf] Batch operations 10x faster than sequential` |
| `[config]` | Configuration info | `[2026-03-22] [config] Timeout setting in execution_env.timeout` |

#### 8 Hooks' Specific Behaviors

| Hook | Behavior |
|------|------|
| `TaskCreated` | Rebuild context.md and trace.md (clear and rewrite); preserve existing insights.md; auto-compress when exceeding 80 lines |
| `PreToolUse` | Inject into Agent: full context.md + latest 20 lines of trace.md + latest 30 lines of insights.md; also normalize `.runtime/` paths in tool input |
| `PostToolUse` | Remind Agent to update trace.md, context.md, insights.md |
| `TaskCompleted` | Remind Agent to complete final state recording |
| `StopFailure` | Emphasize recording failure reasons as `[pitfall]` in insights.md |
| `SubagentStart` | Remind Agent to record subtask progress in trace.md |
| `SubagentStop` | If subtask failed, emphasize recording as `[pitfall]` |
| `Stop` | Default allow; remind Agent to ensure runtime files reflect final state |

#### Recommended Configuration

```yaml
# config/system.yaml or Agent YAML
skills:
  - path: "skills/agent-recall-with-files"
    invocation-control:
      allow-model: "force-inject"   # Force-inject into system prompt
      allow-hook: true
```

---

### 10.2 agent-visualization

**Path**: `skills/agent-visualization/`
**Version**: 1.0.0
**Type**: Passive (`allow-model: false`, LLM invisible)

**Function**: Transparent event collector. Automatically collects Agent lifecycle events into a `visualization.json` timeline for visualizing Agent execution processes.

#### Key Configuration

```yaml
# config/system.yaml
skills:
  - path: "skills/agent-visualization"
    invocation-control:
      allow-model: false   # LLM completely unaware of this Skill
      allow-hook: true
```

#### Registered Hooks (8 total)

| Hook Event | Behavior |
|-----------|------|
| `TaskCreated` | Initialize `visualization.json`, register supervisor and all worker agents to config; write start event (`status: "thinking"`) |
| `TaskCompleted` | Write completed event (`status: "completed"`) |
| `StopFailure` | Write error event (`status: "error"`) |
| `SubagentStart` | Dynamically add worker to config; write supervisor's agent_call event (`status: "waiting"`) and worker's activated event (`status: "thinking"`) |
| `SubagentStop` | Write worker's completed/error event; write supervisor's agent_return event (`status: "reviewing"`) |
| `PreToolUse` | Write tool_call event (`status: "codeact"`); filter internal tools |
| `PostToolUse` | Update the most recent timeline event's description with tool return value |
| `PostToolUseFailure` | Update the most recent timeline event with error info, change `status` to `"error"` |

#### File Path and Worker Event Routing

`visualization.json` is created by the `TaskCreated` hook at `.runtime/<supervisor_name>/visualization.json`.

**Key mechanism**: All Worker events are **not** written to their respective `.runtime/<worker_name>/` directories; instead, they are routed through `find_supervisor_viz_path()` to the **supervisor's `visualization.json`**. This function traverses `.runtime/*/visualization.json` and finds the file containing an agent with `type == "supervisor"` in `config.agents`. This way, all Agents' timelines are consolidated in a single file for unified visualization.

> **Initialization phase redirect**: If a Worker Agent has not yet been "activated" (no corresponding `activated` event in the timeline), its tool calls are automatically attributed to the supervisor (because the Worker is still in the framework initialization phase, such as Skill Hook scanning).

#### visualization.json Structure Example

```json
{
  "config": {
    "title": "Agent Execution: supervisor_agent",
    "agents": [
      {"name": "supervisor_agent", "type": "supervisor"},
      {"name": "worker_agent_a", "type": "worker"}
    ]
  },
  "timeline": [
    {
      "step": 1,
      "agent_name": "supervisor_agent",
      "agent_type": "supervisor",
      "event_type": "start",
      "status": "thinking",
      "description": "Task started"
    },
    {
      "step": 2,
      "agent_name": "supervisor_agent",
      "agent_type": "supervisor",
      "event_type": "tool_call",
      "status": "codeact",
      "tool_name": "shell_tool",
      "description": "Calling tool: shell_tool"
    }
  ]
}
```

#### status Value Reference Table

| status | Meaning | Usage Scenario |
|--------|------|----------|
| `"thinking"` | Agent is thinking | TaskCreated (supervisor), SubagentStart (worker activation) |
| `"codeact"` | Executing tool call | PreToolUse (all tools) |
| `"waiting"` | Supervisor waiting for worker to return | SubagentStart (supervisor side) |
| `"reviewing"` | Supervisor reviewing worker results | SubagentStop (supervisor side) |
| `"completed"` | Successfully completed | TaskCompleted, SubagentStop (worker success) |
| `"error"` | Execution error | StopFailure, PostToolUseFailure, SubagentStop (worker failure) |

The following tool calls are **filtered** and not recorded to the timeline (framework internal tools):
`validate_workspace_path`, `shell_hook_wrapper`, `final_answer`

---

## 11. load_skill() and list_skills() API

These two tool functions are the interface for Agents to interact with the Skill system at runtime, included by default in `default_loaded_tools` in `config/system.yaml`.

> **Remote Environment Considerations (Docker / E2B)**
>
> When `execution_env.type` is `"docker"` or `"e2b"`, the framework skips loading **all** default tools in `default_loaded_tools` (including `load_skill` and `list_skills`), so the Agent cannot proactively call these two tools at runtime. This is an intentional design decision — the framework applies **list-level skipping** for docker/e2b mode (rather than evaluating each tool individually), because most default tools (`shell_tool`, `read_file`, etc.) depend on the local filesystem and are unavailable in remote environments. Although `load_skill` and `list_skills` don't depend on the local filesystem (they read from memory), they are also skipped.
>
> **However, the Skills system itself is unaffected**:
> - SkillsManager is always initialized, Hooks are always registered and triggered
> - `allow-model: "force-inject"` Skills are always injected into the system prompt
>
> **Alternatives**:
> - **Recommended**: Set critical Skills to `allow-model: "force-inject"`, no `load_skill()` call needed
> - **Alternative**: Explicitly declare `load_skill` / `list_skills` in the Agent YAML's `tools:` field
>
> **Default Tool Classification**:
>
> | Tool | Depends on Local Filesystem | Loaded by Default in docker/e2b |
> |------|:-:|:-:|
> | `load_skill` | No (reads from memory) | ❌ |
> | `list_skills` | No (reads from memory) | ❌ |
> | `shell_tool` | Yes | ❌ |
> | `read_file` | Yes | ❌ |
> | `list_files_glob` | Yes | ❌ |
> | `ripgrep_search_directory` | Yes | ❌ |
> | `edit_file` | Yes | ❌ |
> | `write_markdown_file` | Yes | ❌ |

### load_skill()

**Function**: Load the specified Skill's complete instructions, returning them to the LLM for execution.

```python
load_skill(skill: str, args: Optional[str] = None) -> str
```

| Parameter | Type | Description |
|------|------|------|
| `skill` | `string` | Skill name identifier, e.g., `"task-logger"` |
| `args` | `string` (optional) | Arguments string passed through to Skill context |

Returns an XML-structured string containing Skill name, description, allowed tools list, and complete instruction body. Raises `ValueError` if Skill doesn't exist, listing all available names.

**Special case**: If the Skill declares `allow-model: "force-inject"`, the call returns a deduplication notice:

```xml
<skill_already_loaded>
Skill 'agent-recall-with-files' has already been force-injected into the system prompt.
Its full instructions are already in your context under <force_injected_skills>.
You do NOT need to call load_skill for this skill.
</skill_already_loaded>
```

---

### list_skills()

**Function**: List all LLM-visible Skills (not hidden by `allow-model: false`) and their descriptions.
Even if a Skill declares `allow-model: "force-inject"`, it still appears in `list_skills()` (because it is not hidden).

> **Clarification**: `list_skills()` returns all Skills not hidden by `allow-model: false` (including `force-inject` Skills). The `<available_skills>` catalog in the system prompt only includes `allow-model: true` (on-demand) Skills, not `force-inject` ones (since their instructions are already in `<force_injected_skills>`). The two have different scopes.

```python
list_skills(include_description: bool = True) -> str
```

**Return value example**:

```json
[
  {"name": "agent-recall-with-files", "description": "Cross-session experience recall..."},
  {"name": "task-logger", "description": "Automatically logs task start/completion time to log file"}
]
```

---

## 12. Complete Configuration Examples

### Example 1: Minimal SKILL.md (No Hooks, Pure Instructions)

```
---
name: code-review-guide
description: "Code review process guide. Ensure all critical checkpoints are covered during code reviews."
---

# Code Review Guide

## Items to Check

1. **Functional correctness**: Does the logic meet requirements
2. **Security**: SQL injection, XSS, and other risks
3. **Performance**: Any obvious performance bottlenecks
```

---

### Example 2: Full SKILL.md (With All Fields and Multiple Hooks)

```yaml
---
name: safe-file-ops
description: "Safe file operations Skill. Automatically backs up before all file write operations to prevent accidental overwriting of important files."
version: "2.0.0"
allowed-tools: "Read, Write, Edit, Bash"
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
  PreToolUse:
    - matcher: "Write|Edit"
      hooks:
        - type: command
          command: python ./scripts/on_pre_write.py
  PostToolUse:
    - matcher: "Write|Edit"
      hooks:
        - type: command
          command: python ./scripts/on_post_write.py
  StopFailure:
    - hooks:
        - type: command
          command: python ./scripts/on_task_fail.py
---

# Safe File Operations

When using this Skill for file operations, the system automatically backs up target files before writing.
```

---

### Example 3: system.yaml Global Configuration

```yaml
# config/system.yaml
skills:
  - path: "skills/agent-recall-with-files"  # Configured with allow-model: "force-inject" in system.yaml
    invocation-control:
      allow-model: "force-inject"
      allow-hook: true

  - path: "skills/agent-visualization"      # Configured with allow-model: false in system.yaml
    invocation-control:
      allow-model: false
      allow-hook: true

  - path: "skills/company-standards"        # Custom global Skill (default allow-model: true)
```

---

### Example 4: Agent YAML Referencing Skills

```yaml
# applications/my-app/workflows/my-agent.yaml
name: "my_agent"
description: "My Agent"
model_type: "powerful"

skills:
  - path: "skills/task-logger"                   # Relative path
  - path: "applications/my-app/skills/domain"   # Application-local Skill
    platform: "Claude"
  - path: "skills/critical-workflow"
```

---

## 13. FAQ

**Q: Hook isn't being triggered, how to troubleshoot?**

1. Check event name spelling: case-sensitive, must exactly match the table (e.g., `TaskCreated`, not `task_start`)
2. Check if `matcher` and actual tool name match (note tool names after `tools_mapping` mapping)
3. Check run logs, search for `Loaded skill metadata: <name>` to confirm Skill loaded successfully
4. Verify Hook script path `./scripts/xxx.py` is correct relative to Skill directory

---

**Q: Hook script error blocked tool execution, but I want it to default to allow?**

Ensure the script always exits with code `0` and outputs valid JSON:

```python
try:
    # ... your logic ...
    output({"decision": "allow"})
except Exception as e:
    output({"decision": "allow", "reason": f"Hook error (ignored): {e}"})
    sys.exit(0)
```

---

**Q: For `allow-model: "force-inject"` Skills, do Hooks still execute?**

Yes. `allow-model` only affects how Skill instructions are presented to the LLM, **not Hook execution**. Hooks are controlled by `allow-hook` — the two are orthogonal dimensions.

---

**Q: Can SKILL.md body reference files under `references/`?**

Yes, via Markdown links, but the framework **does not auto-load linked files**. The LLM needs to proactively read them via `read_file` tool. This is a progressive loading mechanism to avoid consuming too many tokens at once.

---

**Q: `import common` reports module not found?**

Hook script working directory is the Skill directory; manually add `scripts/` to the Python path:

```python
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from common import ...
```

---

**Q: Can multiple Hook scripts be attached to the same event?**

Yes, add multiple entries in the `hooks` list; they execute in order:

```yaml
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start_a.py
        - type: command
          command: python ./scripts/on_task_start_b.py
```

If any returns `block`, subsequent execution is interrupted.

---

**Q: If a passive skill has `allow-model: false`, do Hooks still execute?**

Yes. As long as `allow-hook: true` (which is the default), the system still automatically triggers its Hook mechanism in the background.
If `allow-hook: false`, the Skill's Hooks are not registered and will not be triggered. This is why these are split into two orthogonal parameters.

---

## Appendix: Field Quick Reference Table

### SKILL.md Frontmatter Fields (Parsed Directly by Framework)

| Field | Type | Required | Default | Brief Description |
|------|------|------|--------|---------|
| `name` | `string` | No | Directory name | Globally unique identifier |
| `description` | `string` | Recommended | `""` | Description shown in LLM skill catalog |
| `version` | `string` | No | `null` | Semantic version number, documentation only |
| `allowed-tools` | `string` or `list` | No | `null` | Declares available tools, supports tool name mapping |
| `hooks` | `dict` | No | `null` | Lifecycle Hook definitions |

### Reference Configuration Parameters (Set in system.yaml / Agent YAML)

| Field | Type | Default | Brief Description |
|------|------|--------|---------|
| `path` | `string` | None (required) | Skill directory path (relative or absolute) |
| `platform` | `string` | `"Claude"` | Tool name mapping platform identifier |
| `invocation-control` | `dict` | `{"allow-model": true, "allow-hook": true}` | Tri-state LLM visibility and loading strategy control (`true`/`false`/`"force-inject"`), and Hook permissions |

### All 9 Hook Events

| Event Name | YAML Key | Needs matcher | `tool_name` Value |
|--------|-----------|----------------|---------------|
| Task Start | `TaskCreated` | No | `"task"` |
| Task Complete | `TaskCompleted` | No | `"task"` |
| Task Fail | `StopFailure` | No | `"task"` |
| Subtask Start | `SubagentStart` | Optional (defaults to `"*"`) | Worker Agent name |
| Subtask Finish | `SubagentStop` | Optional (defaults to `"*"`) | Worker Agent name |
| Pre Tool Use | `PreToolUse` | Yes (tool name) | Actual tool function name |
| Post Tool Use | `PostToolUse` | Yes (tool name) | Actual tool function name |
| Post Tool Error | `PostToolUseFailure` | Yes (tool name) | Actual tool function name |
| Stop | `Stop` | No | `"final_answer"` |

### Hook Script Output Fields Quick Reference

| Field | Type | Description |
|------|------|------|
| `decision` | `string` | `"allow"` / `"block"` / `"modify"` (optional, defaults to `"allow"`) |
| `modified_input` | `dict` | Merge-overwrite tool input (`modify` mode, only include fields to change) |
| `modified_response` | `dict` | Modify tool output (`modify` mode) |
| `agent_context` | `string` | Text injected into Agent prompt |
| `user_message` | `string` | Message displayed to the user |
| `reason` | `string` | Explanation for interception or processing; recommended for `block` to provide clearer feedback |
| `telemetry` | `dict` | Custom telemetry/debug data |
