---
name: create-skill
description: "Use when: creating a new AgentLoom Skill from scratch based on user requirements. Covers SKILL.md generation (frontmatter + markdown body), Hook scripts scaffolding, common.py utilities, registration to system.yaml or Agent YAML, and post-creation validation. DO NOT USE for modifying existing skills or non-AgentLoom project."
---

# Create AgentLoom Skill

AgentLoom Skill creation skill. Can be invoked by **Copilot Chat / Copilot Codex / Claude Code / AgentLoom Agent** to automatically (or interactively) generate a complete Skill directory structure compliant with the `docs/en/skills_config.md` specification based on user requirements.

> **📖 Companion Reference Documents** (consult as needed):
> - [references/skill-template.md](./references/skill-template.md) — SKILL.md template, YAML frontmatter complete field reference, Hook event quick reference
> - [references/hook-scripts-guide.md](./references/hook-scripts-guide.md) — Complete Hook script development guide (environment variables, JSON output, exit codes, common.py patterns)
>
> **📖 Authoritative Specification Documents** (must read before generation):
> - `docs/en/skills_config.md` — AgentLoom Skills configuration complete reference (the specification source for this Skill)
>
> Reference paths are relative to the current Skill root directory.

## Applicable Scenarios

- User says "Help me create a new Skill" or "I want to write a Skill to do XXX"
- User describes logic that needs to be automatically executed during the Agent lifecycle (requires Hooks)
- User wants to inject domain knowledge or operational specifications into an Agent (requires LLM instructions)
- User wants a reusable capability extension package across multiple Agents/workflows

## Non-applicable Scenarios

- Modifying an existing Skill's configuration (directly edit the corresponding SKILL.md)
- Creating an Application or Agent workflow (use the `create-app` Skill)
- Only need a single utility function without Skill packaging
- Non-AgentLoom framework projects

## Execution Strategy

| Environment | Strategy |
|------|------|
| **Interactive** (VS Code Copilot Chat / terminal conversation) | Fill in missing information first, then confirm the plan, generate files after receiving confirmation |
| **Autonomous** (Copilot Codex / Claude Code / batch processing) | Extract information from prompt; when unable to ask questions, generate directly based on "inferable information + default strategy", attach an "assumptions list" |

> **Core Principle**: When encountering unclear or uncertain points, **ask the user directly** — do not make assumptions.

## Execution Prerequisites (Required)

- **Navigate to the AgentLoom root directory first** before executing any operations of this Skill (creating directories, generating files, registering configurations, etc.).
- **Root directory identification criteria**: The `config/llm.yaml` file exists.
  - ⚠️ Do not use `config/system.yaml` for identification, as application-level directories may also contain this file (e.g., `applications/ai_quality_analysis/config/system.yaml`), which cannot uniquely identify the project root.
  - `config/llm.yaml` is globally unique and only exists in the AgentLoom root directory.
- All paths are resolved relative to the AgentLoom root directory.

## Path Strategy

- **Skill directory location**: Defaults to `skills/<skill-name>/` (relative to AgentLoom project root)
- If the user specifies a different path (e.g., `applications/xxx/skills/`), place it as requested
- All file paths are resolved relative to the project root (the directory containing `config/llm.yaml`)

---

## Phase 1: Requirements Gathering

**Requirements gathering must be completed before generating any files.**

### Pre-action: Read Authoritative Specification

Before starting creation, **you must first read** the key chapters of the `docs/en/skills_config.md` document to ensure accurate understanding of:
- SKILL.md file format (Chapter 3)
- YAML Frontmatter all fields (Chapter 4)
- Hook system (Chapter 6)
- Hook script development (Chapter 7)

### Information Extraction Checklist

Extract the following information from the user's prompt or conversation. **Bold items are required**; others have default values and can be skipped:

| # | Information Item | Required | Default | Description |
|---|--------|------|--------|------|
| 1 | **Skill name** | ✅ | — | Lowercase + hyphens, e.g., `task-logger`, used as directory name |
| 2 | **One-line description** | ✅ | — | Used for the `description` field in SKILL.md |
| 3 | **Skill type** | ✅ | — | Force-inject / On-demand / Hidden (determines `invocation-control.allow-model`) |
| 4 | **Core functionality description** | ✅ | — | What the Skill does, when to use it, what the operation steps are |
| 5 | Whether Hooks are needed | ❌ | Determined as needed | Hooks are needed if logic must execute before/after tool calls or during task lifecycle |
| 6 | Hook event list | ✅ when using Hooks | — | Choose from 9 events: TaskCreated, TaskCompleted, StopFailure, SubagentStart, SubagentStop, PreToolUse, PostToolUse, PostToolUseFailure, Stop |
| 7 | Tools to intercept | ✅ for PreToolUse/PostToolUse/PostToolUseFailure | `"*"` | Used for `matcher`, e.g., `"Write\|Edit\|Bash"`. SubagentStart/SubagentStop can also optionally use matcher (matching Worker Agent names) |
| 8 | allowed-tools | ❌ | `null` | List of tools the Skill will use |
| 9 | version | ❌ | `"1.0.0"` | Semantic version number |
| 10 | Placement path | ❌ | `skills/<name>/` | Skill directory location |
| 11 | Registration location | ❌ | Ask after creation | `config/system.yaml` (global) or a specific Agent YAML (local) |

### Skill Type Selection Guide

Help the user choose the correct Skill type:

| Type | `allow-model` Value | Applicable Scenario | Typical Examples |
|------|-----------------|---------|---------|
| **Force-inject** | `"force-inject"` | Core capability, LLM must always follow | Memory system, security specifications, coding standards |
| **On-demand** | `true` (default) | Domain assistance, LLM invokes as needed | API operation guides, specific workflow specifications |
| **Hidden** | `false` | Background monitoring, LLM does not need to be aware | Event collection, visualization, logging |

> **Recommendation**: If the user is unsure, recommend `"force-inject"` for critical Skills (to prevent LLM from forgetting to call `load_skill()`).

> **⚠️ Remote Environment Note (Docker / E2B)**: When `execution_env.type` is `"docker"` or `"e2b"`, the framework **skips loading all default tools** (including `load_skill` and `list_skills`). This means Agents in remote environments **cannot actively call `load_skill()`**. For Skills that must work in remote environments, use `"force-inject"` to embed instructions directly into the system prompt, or explicitly declare `load_skill` in the Agent YAML's `tools:` field.

### Hook Requirements Assessment Guide

Determine whether Hooks are needed and which events to use based on user requirements:

| User Requirement | Required Hook | Description |
|---------|------------|------|
| Initialize environment/files at task start | `TaskCreated` | Create directories, read historical state |
| Clean up/notify at task completion | `TaskCompleted` | Clean up temporary files, send notifications |
| Record/rollback on task failure | `StopFailure` | Record failure reasons, rollback state |
| Validate/modify input before tool calls | `PreToolUse` | Path validation, parameter rewriting, permission checks |
| Process output/logs after tool calls | `PostToolUse` | Log recording, output filtering |
| Handle tool execution exceptions | `PostToolUseFailure` | Error handling, rollback operations |
| Track subtask start/completion | `SubagentStart` / `SubagentStop` | Progress tracking |
| Check before Agent gives final answer | `Stop` | Ensure required steps are completed |
| Only need LLM instructions, no automation | No Hooks needed | Only write the Markdown body |

> **⚠️ `PostToolUse` and `PostToolUseFailure` are mutually exclusive**: For the same tool call, only one of them will be triggered — `PostToolUse` on success, `PostToolUseFailure` on exception. They will never both fire for the same invocation.

### tool_input Key Fields Quick Reference

When writing Hook scripts, understand what key fields `tool_input` contains for different events:

| Event | Key Fields in `tool_input` |
|------|---------------------------|
| `TaskCreated` | `task_id`, `cwd`, `task_text` (task text), `agent_name`, `worker_agents` (Worker name list) |
| `TaskCompleted` / `StopFailure` | `task_id`, `cwd`, `task_text`, `agent_name`; StopFailure additionally contains `error`, `error_type` |
| `SubagentStart` | `agent_name` (Worker Agent name), `sub_task_id` |
| `SubagentStop` | `agent_name`, `sub_task_id`, `success` (boolean); on failure additionally contains `error` |
| `PreToolUse` / `PostToolUse` / `PostToolUseFailure` | Complete tool call input parameters (varies by tool) |
| `Stop` | `final_answer` (the final answer the Agent is about to give) |

### Interactive Mode: When Required Information Is Missing

If the prompt is missing required information (#1-#4), ask the user. **Ask all missing items at once**:

```
The following information is needed to create the Skill:
1. Skill name? (recommended: lowercase + hyphens, e.g., task-logger)
2. One-line description of functionality?
3. Skill type? (Force-inject / On-demand / Hidden)
4. What is the core functionality? (what it does, when to use it, operation steps)
5. Are Hooks needed? If so, which events should they be attached to?
```

---

## Phase 2: Plan Confirmation

**Before generating any files**, you must present the complete generation plan to the user.

### Plan Template

```markdown
## Skill Generation Plan

### Basic Information
- **Name**: {name}
- **Description**: {description}
- **Version**: {version}
- **Type**: {Force-inject / On-demand / Hidden}
- **Directory**: {path}

### Directory Structure
{Show the complete directory tree to be generated}

### SKILL.md Frontmatter
{Show key YAML frontmatter configuration}

### Hook Plan
{If there are Hooks, list each event's script and behavior}

### Markdown Body Summary
{Summary of LLM instruction main content}

### Registration Method
{How it will be registered to the configuration file}

Please confirm whether to proceed with generation? If adjustments are needed, please let me know.
```

> **Wait for user confirmation before starting Phase 3.** Confirmation can be skipped in autonomous scenarios.

---

## Phase 3: File Generation

### Generation Order

Generate files strictly in the following order:

1. **Create directory structure**
2. **Generate SKILL.md** (frontmatter + markdown body)
3. **Generate scripts/common.py** (if Hooks are present)
4. **Generate each Hook script** (if Hooks are present)
5. **Generate templates/** (if template requirements exist)
6. **Generate references/** (if reference documentation is needed)

### 3.1 SKILL.md Generation Specification

#### Frontmatter Specification

```yaml
---
name: {skill-name}                    # Must match directory name
description: "{clear functionality description}"  # LLM uses this to decide whether to use the Skill
version: "1.0.0"
allowed-tools: "{tool list}"            # Recommended to use abstract names: Read, Write, Edit, Bash, Glob, Grep
hooks:                                 # If Hooks are present
  {EventName}:
    - matcher: "{pattern}"             # Required for tool events, not needed for lifecycle events
      hooks:
        - type: command
          command: python ./scripts/{script_name}.py
---
```

**Frontmatter Checklist**:
- [ ] `name` matches the directory name
- [ ] `description` is clear and does not exceed 1024 characters
- [ ] `description` includes trigger phrases like "Use when" / "Applicable for"
- [ ] `allowed-tools` uses abstract tool names (e.g., `Read` instead of `read_file`)
- [ ] Hook `matcher` uses abstract tool names (automatically mapped at load time)
- [ ] YAML values containing colons are wrapped in quotes
- [ ] Indentation uses spaces, not tabs
- [ ] **Do NOT** put `platform` or `invocation-control` in frontmatter — they are only configured on the referencing side (system.yaml / Agent YAML)

#### Markdown Body Specification

The Markdown body is the content returned to the LLM by `load_skill()`, which the LLM must follow. Recommended structure:

```markdown
# {Skill Name}

One-line description of what this Skill does.

## Use Cases
- When to use
- When not to use

## Operation Steps
1. First step...
2. Second step...
3. Third step...

## Notes
- Important constraints and rules
```

**Body Checklist**:
- [ ] Starts with a clear functionality description
- [ ] Has explicit use cases and non-applicable scenarios
- [ ] Has specific operation steps (avoid vague instructions)
- [ ] If there are runtime files, specifies file paths and formats
- [ ] Language matches the target audience (use Chinese for Chinese projects)

### 3.2 Hook Script Generation Specification

#### common.py Template

Every Skill with Hooks should generate `scripts/common.py`:

```python
# scripts/common.py
import json
import os
from pathlib import Path


def get_agent_name() -> str:
    """Get current Agent name from environment variable"""
    return os.environ.get("AGENT_NAME", "") or "default"


def get_tool_name() -> str:
    """Get current tool name from environment variable"""
    return os.environ.get("TOOL_NAME", "") or "unknown"


def get_task_id() -> str:
    """Get task ID from environment variable"""
    return os.environ.get("TASK_ID", "") or ""


def get_hook_event() -> str:
    """Get Hook event name from environment variable"""
    return os.environ.get("HOOK_EVENT", "") or "Unknown"


def get_hook_context() -> dict:
    """Parse HOOK_CONTEXT_JSON"""
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


def get_tool_response():
    """Extract tool_response from Hook context"""
    return get_hook_context().get("tool_response")


def output(result: dict) -> None:
    """Print JSON result to stdout"""
    print(json.dumps(result, ensure_ascii=False))


def _find_agent_loom_root() -> Path:
    """Derive the AgentLoom project root directory.

    Resolution order:
    1. $AGENT_LOOM_RUNTIME_ROOT env var (for tests with temp dirs).
    2. Walk upward from this file and look for config/llm.yaml
       — the globally unique AgentLoom root marker.
    3. pyproject.toml fallback (backward compat).
    4. cwd fallback.
    """
    env_root = os.environ.get("AGENT_LOOM_RUNTIME_ROOT", "").strip()
    if env_root:
        return Path(env_root)

    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "config" / "llm.yaml").exists():
            return current
        current = current.parent

    candidate = Path(__file__).resolve().parent.parent.parent.parent
    if (candidate / "pyproject.toml").exists():
        return candidate

    return Path.cwd()


def runtime_dir(agent_name: str) -> Path:
    """Return <agent_loom_root>/.runtime/<agent_name> path"""
    return _find_agent_loom_root() / ".runtime" / agent_name
```

> **Root directory detection priority**: `$AGENT_LOOM_RUNTIME_ROOT` environment variable > upward traversal for `config/llm.yaml` > `pyproject.toml` fallback > current working directory.
> `config/llm.yaml` is preferred because it is the globally unique identifier file for the AgentLoom project. Upward traversal ensures correct detection regardless of how deeply the Skill is nested (e.g., `applications/xxx/skills/my-skill/`).

#### Hook Script Working Directory (cwd)

The `cwd` when executing Hook scripts is always the **Skill directory** (the directory containing SKILL.md), not the project root.
Therefore, the `./scripts/` path in `command: python ./scripts/on_task_start.py` is resolved relative to the Skill directory.

#### Hook Script Template

Each Hook script follows a unified structure:

```python
# scripts/on_{event}.py
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from common import get_agent_name, get_hook_context, output


def main():
    agent = get_agent_name()
    context = get_hook_context()

    # ← Write Hook logic here

    output({
        "decision": "allow",          # allow / block / modify
        # "modified_input": {},        # Can modify tool input during PreToolUse
        # "modified_response": {},     # Can modify tool output during PostToolUse
        # "agent_context": "",         # Inject into Agent system prompt
        # "user_message": "",          # Send message to user
        # "reason": "",                # Reason description
        # "telemetry": {},             # Custom telemetry data
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Avoid script exceptions causing non-zero exit code → unexpected block
        output({"decision": "allow", "reason": f"Hook error (safe allow): {e}"})
```

> **Best Practice**: Wrap the `main()` call with `try/except`, outputting `allow` with a reason on exception.
> If the script exits with a non-zero exit code, the framework will **force block** (even if the JSON says `allow`), which is usually unintended behavior.

**`decision: "block"` Actual Effects by Event**:

| Event | block Effect |
|------|-----------|
| `PreToolUse` | ✅ Directly prevents tool execution |
| `PostToolUse` / `PostToolUseFailure` | ⚠️ Cannot undo completed execution, only prevents result propagation |
| `Stop` | Prevents Agent from giving final answer |
| Lifecycle events | Does not interrupt main task flow, only skips subsequent Hooks |

> If the goal is to prevent an operation from happening, intercept at the `PreToolUse` stage.

**Hook Script Checklist**:
- [ ] Starts with `sys.path.insert(0, os.path.dirname(__file__))`
- [ ] Imports utility functions from `common`
- [ ] Has a `main()` function and `if __name__ == "__main__"` entry point
- [ ] Uses `output()` to emit JSON (do not directly `print` non-JSON content)
- [ ] Only outputs the 7 fields defined in the specification (decision, modified_input, modified_response, agent_context, user_message, reason, telemetry)
- [ ] Exception handling prevents the script from exiting with a non-zero exit code (unless intentionally blocking) — wrapping `main()` with `try/except` is recommended
- [ ] Note the Hook timeout limit (default 20 seconds), customizable via the `timeout` field:

```yaml
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
          timeout: 60    # Unit: seconds, default 20
```

---

## Phase 4: Registration Configuration

After creating the Skill files, ask the user how to register:

### Prompt Template

```
Skill creation is complete! Please choose a registration method:

1. **Global registration** (shared by all Agents): Add to config/system.yaml
2. **Agent-level registration** (specific Agent only): Add to the Agent YAML's skills field
3. **Auto-discovery** (already auto-discovered when placed in the skills/ directory, no additional registration needed)
4. **Skip registration for now**

If you choose 1 or 2, you also need to confirm:
- invocation-control configuration? (force-inject / true / false)
- allow-hook? (true / false)
```

> **Important Note on Auto-discovery**:
> - Auto-discovery **only scans** the `AGENT_ROOT/skills/` directory (i.e., the `skills/` directory under the AgentLoom root), recursively searching for `SKILL.md` / `skill.md` files.
> - Skills in the `tools/skills/` directory are tool Skills for AI assistants and are **not in the auto-discovery path**; they will not be automatically loaded by AgentLoom Agents.
> - If the Skill is placed in a non-standard path like `applications/xxx/skills/`, it **must be manually registered** in `system.yaml` or Agent YAML.

### Registration Configuration Format

**Global registration** (`config/system.yaml`):

```yaml
skills:
  # ... existing skills ...
  - path: "skills/{skill-name}"
    invocation-control:
      allow-model: {true / false / "force-inject"}
      allow-hook: true
```

**Agent-level registration** (Agent YAML):

```yaml
skills:
  - path: "skills/{skill-name}"
    platform: "Claude"
```

**String shorthand** (simplest form, defaults to `allow-model: true, allow-hook: true`):

```yaml
skills:
  - "skills/{skill-name}"
```

> **⚠️ Naming Conflict Warning**: If a Skill with the same `name` already exists (loaded from global config, auto-discovery, or another Agent YAML), the later-loaded one will **silently override** the earlier one (with a warning log). Before creating a new Skill, check existing names with `list_skills()` or review `config/system.yaml` and the `skills/` directory to avoid accidental overrides.

> **Note**: If the Skill is placed in the `skills/` directory (`AGENT_ROOT/skills/`, i.e., the `skills/` directory under the AgentLoom root), explicit path registration in `system.yaml` or Agent YAML is not required — the framework will automatically scan and discover it. However, **`invocation-control` parameters still need to be configured on the referencing side** (defaults are `allow-model: true, allow-hook: true`).
>
> ⚠️ Auto-discovery **does not include** `tools/skills/` and `applications/xxx/skills/` or other paths — Skills in these locations must be manually registered.

---

## Phase 5: Validation

### Post-generation Validation Checklist

- [ ] SKILL.md exists and is correctly formatted (frontmatter + markdown)
- [ ] `name` field matches the directory name
- [ ] `description` is non-empty and meaningful
- [ ] All Hook script files exist and are executable
- [ ] `common.py` exists (if Hooks are present)
- [ ] All Python scripts have correct syntax (no SyntaxError)
- [ ] Hook `matcher` uses abstract tool names
- [ ] `allowed-tools` uses abstract tool names
- [ ] Registration configuration has been added (if requested by user)
- [ ] Directory structure conforms to the specification

### Validation Commands

```bash
# Check SKILL.md format (replace <skill-path> with the actual Skill path, e.g., skills/task-logger)
cd AgentLoom && .venv/bin/python -c "
from src.lib.smolagents.skills.parser import parse_skill_file
meta, content = parse_skill_file('<skill-path>/SKILL.md')
print(f'Name: {meta.name}')
print(f'Description: {meta.description[:80]}...')
print(f'Hooks: {list(meta.hooks.keys()) if meta.hooks else \"None\"}')
print(f'Allowed tools: {meta.allowed_tools}')
print('✅ SKILL.md parsed successfully')
"
```

```bash
# Check Hook script syntax (replace <skill-path> with the actual path)
cd AgentLoom && .venv/bin/python -m py_compile <skill-path>/scripts/common.py
cd AgentLoom && .venv/bin/python -m py_compile <skill-path>/scripts/on_task_start.py
# ... other scripts
```

---

## Reference: Existing Skill Examples

### agent-recall-with-files (Force-inject Type)

```
skills/agent-recall-with-files/
├── SKILL.md              # 6.0.0, 8 Hooks, force-inject
├── scripts/
│   ├── common.py
│   ├── on_task_start.py
│   ├── on_task_complete.py
│   ├── on_task_fail.py
│   ├── on_pre_tool_use.py
│   ├── on_post_tool_use.py
│   ├── on_subtask_start.py
│   ├── on_subtask_finish.py
│   └── on_stop.py
└── templates/
    ├── context.md
    └── trace.md
```

### agent-visualization (Hidden Type)

```
skills/agent-visualization/
├── SKILL.md              # 1.0.0, 8 Hooks, allow-model: false
└── scripts/
    ├── common.py
    ├── on_task_start.py
    ├── on_task_complete.py
    ├── on_task_fail.py
    ├── on_subtask_start.py
    ├── on_subtask_finish.py
    ├── on_pre_tool_use.py
    ├── on_post_tool_use.py
    └── on_post_tool_error.py
```
