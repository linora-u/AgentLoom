---
name: create-app
description: "Use when creating a new AgentLoom-compatible Application scaffold (single-agent or supervisor+workers), including workflow YAML, worker configs, entry scripts, custom tools, and post-generation validation."
---

# Create AgentLoom Application

AgentLoom Application scaffolding generation skill. Can be invoked by **Copilot Codex / Claude Code / AgentLoom Agent** to automatically (or interactively) generate a complete Application directory structure and configuration files based on user requirements.

> **📖 Companion Reference Documents** (consult as needed):
> - [references/quick-reference.md](./references/quick-reference.md) — Full predefined tool table, model_type/execution_env selection rules, configuration constraint checklist
> - [references/full-example.md](./references/full-example.md) — **Complete end-to-end example**: `code_review` Application from requirements to file generation full workflow + single Agent mode example
> - [references/templates.md](./references/templates.md) — Complete generation templates for all Phase 3 files
> - [references/troubleshooting.md](./references/troubleshooting.md) — Common configuration error troubleshooting
> - [references/agent-yaml-schema.json](./references/agent-yaml-schema.json) — Agent YAML JSON Schema for IDE completion and validation
>
> Reference paths are relative to the current Skill root directory.

## Applicable Scenarios

- User says "help me create a new Application" or "I want to set up a new Agent workflow"
- User has a specific task they want to accomplish with multi-Agent collaboration but doesn't know how to configure it
- User wants to quickly set up a standard Supervisor + N Worker workflow
- User only needs a standalone Agent (**single Agent mode**, no Supervisor required)

## Non-Applicable Scenarios

- Modifying configuration of an existing Application (directly edit the corresponding YAML instead)
- Only need a single tool function without Agent orchestration
- Projects not based on the AgentLoom framework

## Execution Strategy

This Skill supports two execution modes:

| Environment | Strategy |
|-------------|----------|
| **Interactive** (VS Code Copilot Chat / terminal dialog) | First fill in missing information, then confirm the plan; generate files after receiving confirmation |
| **Autonomous** (Copilot Codex / Claude Code / batch processing) | Extract information from user prompt; when unable to ask questions, generate directly using "inferable information + default strategy" and explicitly annotate assumptions in the output without blocking execution |

> **Execution Principles**:
> - Interactive: Strictly follow "confirm plan first, then generate files".
> - Autonomous: When no interactive channel is available, do not wait for confirmation — generate directly and attach an "assumptions list".

## Path Strategy (Unified Rules)

- Treat "project directory" as the working root directory for this task.
- If the user provides a path, use that path as the project root directory.
- If the user only provides a project name, first search accessible workspaces for a directory with the same or closest name.
- If multiple candidate directories exist, prioritize directories containing project identifier files such as `config/llm.yaml`, `pyproject.toml`, `package.json`, `.git`, `src`.
- All input/output file paths are resolved relative to the project root directory.
- Before modifying files, confirm the target file exists; before creating new files, confirm the parent directory can be created.

> **Note for AI Callers**: Both the project root directory and the Skill root directory should be dynamically obtained at runtime (e.g., via workspace path or user context), not hardcoded in scripts. For scripts that need paths, pass the path as a parameter directly.

## Root Directory Prerequisites (Must Be Satisfied First)

- Before executing any detection/update commands, first navigate to the AgentLoom root directory.
- Root directory criteria: prioritize using the parent directory of `config/llm.yaml` as the project root. This is the best approach because Application-level directories might also have their own `config/system.yaml` or other configurations, which can cause confusion.
- If the current directory does not meet the criteria, switch to the AgentLoom root directory first, then proceed with subsequent phases (requirements gathering, plan confirmation, file generation, configuration validation, execution).

## Two Modes

| Mode | When to Use | What's Needed |
|------|-------------|---------------|
| **Supervisor + N Worker** | Task can be split into multiple phases requiring division of labor | Supervisor YAML + N Worker YAMLs |
| **Single Agent** | Task is simple enough for one Agent to complete | Only one YAML (no `worker_agents`, no `agent_function_schema`) |

> **Decision Criteria**: If the user's description involves only one responsibility/one step, recommend single Agent mode first.

---

## Phase 1: Requirements Gathering

**Requirements gathering must be completed before generating any files.**

### Information Extraction Checklist

Extract the following information from the user's prompt or conversation. **Bold items are required**; others have default values and can be skipped:

| # | Information Item | Required | Default | How to Determine |
|---|-----------------|----------|---------|------------------|
| 1 | **Application name** | ✅ | — | Must be provided by user; used as directory name; lowercase + underscore recommended |
| 2 | **One-line feature description** | ✅ | — | Must be provided by user; used for Agent description |
| 3 | **Mode selection: single Agent / multi-Agent** | ✅ | — | Task can be split into multiple phases → multi-Agent; single responsibility → single Agent |
| 4 | Multi-Agent: phase names and responsibilities | Required for multi-Agent ✅ | — | Extract from user description, or suggest splits based on task nature |
| 5 | model_type | ❌ | Inherits `config/llm.yaml`'s `model.default_model_type` | Confirm after reading project config: use default / explicitly specify |
| 6 | tool_call_type | ❌ | `code_act` | Rarely needs changing |
| 7 | Predefined tool list | ❌ | Auto-recommended based on task | Analysis → `read_file`+`get_file_outline`; Modification → `edit_file`; Reporting → `write_markdown_file` |
| 8 | Custom tools | ❌ | None | When user explicitly mentions needing custom Python functions |
| 9 | planning_interval | ❌ | Not set | Recommended `3` for complex Workers |
| 10 | max_steps | ❌ | `80` (recommended `40` for Workers) | Increase for particularly large tasks |
| 11 | execution_env | ❌ | `local` | When user mentions isolation/Docker |
| 12 | Application-level config/system.yaml | ❌ | Not generated | When global configuration needs to be overridden |
| 13 | Custom sysprompt | ❌ | Not generated | When user explicitly requests it |
| 14 | Private skills | ❌ | None | When user mentions custom Skills |

### `model_type` Discovery and Confirmation (Must Execute)

Before writing any Agent YAML, first read `config/llm.yaml` from the project root:

1. Extract available types from the `model` node (excluding reserved keys `default_model_type` and non-dict values).
2. Read `model.default_model_type` as the default model type.
3. In interactive scenarios, confirm with the user:
   - Whether to use the `default_model_type`;
   - If not, display "available types in the project + custom" for selection.
4. In autonomous scenarios:
   - If config is readable, default to using `default_model_type`;
   - If reading fails, use the "inherit project default model type" strategy and declare it in the "assumptions list".

> Constraint: The available options for `model_type` are determined by the project configuration, not hardcoded as `powerful/fast/summary`.

### Smart Recommendation Rules

When the user hasn't explicitly specified tools, auto-recommend based on task type:

| Task Type | Recommended Tools |
|-----------|-------------------|
| Code analysis/review | `read_file`, `get_file_outline`, `browse_directory`, `ripgrep_search_directory` |
| Code modification/refactoring | `read_file`, `edit_file`, `get_file_outline` |
| Report/document generation | `write_markdown_file`, `read_file` |
| Git/PR review | `get_git_diff_content`, `read_file`, `ripgrep_search_directory` |
| Build/test | `shell_tool`, `read_file` |
| Batch file processing | `list_files_glob`, `read_file`, `write_file` |

### Interactive Mode: When Required Information Is Missing

If the prompt is missing required information (#1-#4), ask the user. **Ask all missing items at once**, do not split across multiple rounds:

```
The following information is needed to generate the Application:
1. Application name? (lowercase + underscore recommended, e.g., code_review)
2. One-line feature description?
3. Single Agent or multi-Agent? If multi-Agent, how should the phases be divided?
```

If `config/llm.yaml` has been identified, follow up with (optional but recommended):

```
4. Which model_type strategy to use?
   - Use project default (default_model_type)
   - Explicitly specify (choose from available model_type list in the project)
   - Custom (must be already defined in config/llm.yaml)
```

---

## Phase 2: Plan Confirmation

**Before generating any files**, you must present the complete generation plan to the user.

> 💡 **Full example** can be found in `references/full-example.md`, including the complete plan text for `code_review`.

### Multi-Agent Mode Plan Template

```markdown
## 📋 Application Generation Plan

**Name**: <app_name>
**Description**: <one-line description>
**Mode**: Supervisor + N Worker

### Directory Structure Preview
applications/<app_name>/
├── <app_name>_app.py              # Entry script
├── agent_tools/                    # Custom tools (if any)
│   └── <tool_module>.py
├── config/                         # Application-level config (if any)
│   └── system.yaml
└── workflows/
    ├── <app_name>_agent.yaml       # Supervisor YAML
    └── worker_agents/
        ├── <worker_name_a>.yaml
        ├── <worker_name_b>.yaml
        └── ...

### Supervisor Configuration Summary
- name: <supervisor_name>
- model_type: <use default_model_type or explicitly specified value>
- tool_call_type: <tool_call_type>
- Custom tools: <list>
- Worker count: N

### Worker Configuration Summary
| Stage | Name | Responsibility | model_type Strategy | Tools |
|-------|------|----------------|---------------------|-------|
| 1 | <worker_a> | ... | Inherit default or explicitly specify | [...] |
| 2 | <worker_b> | ... | Inherit default or explicitly specify | [...] |
```

### Single Agent Mode Plan Template

```markdown
## 📋 Application Generation Plan

**Name**: <app_name>
**Description**: <one-line description>
**Mode**: Single Agent (no Supervisor orchestration)

### Directory Structure Preview
applications/<app_name>/
├── <app_name>_app.py              # Entry script
└── workflows/
    └── <app_name>_agent.yaml      # The only Agent YAML

### Agent Configuration Summary
- name: <agent_name>
- model_type: <use default_model_type or explicitly specified value>
- tool_call_type: <tool_call_type>
- Tools: <list>
- max_steps: <N>
```

Phase transition rules:

- **Interactive**: Wait for user confirmation (reply "ok" / "confirm" / provide modification suggestions) before entering Phase 3.
- **Autonomous**: When no interactive channel is available, output "plan + assumptions list" then proceed directly to Phase 3.

---

## Phase 3: File Generation

After entering Phase 3, **generate all files in the following order** (interactive mode enters after confirmation; autonomous mode enters after outputting the plan and assumptions).

> **📄 Full templates** available at [references/templates.md](./references/templates.md); only the file checklist and key points are listed below.
> **📐 YAML Schema** available at [references/agent-yaml-schema.json](./references/agent-yaml-schema.json) for IDE completion and validation.

### 3.1 Supervisor YAML
- **Path**: `applications/<app_name>/workflows/<app_name>_agent.yaml`
- **Required fields**: `name`, `description`, `workflow` (`|` multiline text, or non-empty `list[str]` for sequential workflows)
- **Workflow Best Practice**: The `workflow` field should ideally follow a five-section structure: 1. Background, 2. Core Responsibilities & Constraints, 3. Execution Flow (use ````mermaid```` code blocks — the framework automatically extracts this to inject strict instructions), 4. Detailed Steps, 5. Output Requirements.
- **Key fields**: `model_type` (optional, inherits default when omitted), `tool_call_type`, `tools`, `worker_agents` (use `path` only), `skills` (optional), `execution_env`
- **Full template** → See [templates.md](./references/templates.md) §3.1

### 3.2 Worker YAML (one file per phase)
- **Path**: `applications/<app_name>/workflows/worker_agents/step<N>_<name>.yaml`
- **Difference from Supervisor**: Requires `agent_function_schema` (description + inputs + output), does not need `worker_agents`
- **Private Skills**: Optionally add `skills` field to bind private skill packs to this Worker
- **Concurrency**: If this Worker will be batch-invoked on multiple inputs, add `concurrency: auto` (or a fixed integer). The application layer can then call `tool.batch(tasks)` for automatic parallel execution
- **Recommendation**: `planning_interval: 3`, `max_steps: 40`
- **Full template** → See [templates.md](./references/templates.md) §3.2

### 3.3 Entry Script
- **Path**: `applications/<app_name>/<app_name>_app.py`
- **Key point**: Ensure `project_root` and the YAML path in `run_app()` match the actual paths
- **Full template** → See [templates.md](./references/templates.md) §3.3

### 3.4 Custom Tools (if any)
- **Path**: `applications/<app_name>/agent_tools/<module_name>.py`
- **Key point**: Plain Python functions without `@tool` decorator; description extracted from docstring; `module` + `function` must be paired in YAML
- **Full template** → See [templates.md](./references/templates.md) §3.4

### 3.5 Application-Level config/system.yaml (if any)
- **Path**: `applications/<app_name>/config/system.yaml`
- **Key point**: Deep merged on top of global `config/system.yaml`.
- **Whitelist constraint**: Agent YAML can only override 7 fields (`system`, `smart_summary`, `tool_access_control`, `execution_env`, `code_agent`, `tools`, `prompt`). If you need to override global configs outside of this whitelist, you must generate this Application-level `system.yaml`.
- **Full template** → See [templates.md](./references/templates.md) §3.5

### 3.6 Custom sysprompt (if any)
- **Path**: `applications/<app_name>/sysprompt/code_agent.yaml`
- Only generate when explicitly requested by the user. The framework default prompt is sufficient for most scenarios.

### 3.7 Single Agent Mode YAML (replaces 3.1 and 3.2)
- **Path**: `applications/<app_name>/workflows/<app_name>_agent.yaml`
- **Difference from multi-Agent**: Does not need `worker_agents`, does not need `agent_function_schema`
- **Private Skills**: Optionally add `skills` field (supports string / dict / list formats)
- **Full template** → See [templates.md](./references/templates.md) §3.7
- **Full example** → `references/full-example.md` `simple_scanner` example at the end

### 3.8 Private Skills (optional)
- **Write location**: Top-level `skills` field of the corresponding Agent YAML (Supervisor / Worker / single Agent all supported)
- **Supported formats**:
  - String: `skills: "skills/agent-recall-with-files"`
  - Dict: `skills: {path: "skills/agent-recall-with-files", platform: "Claude"}`
  - List: `skills: ["skills/a", {path: "skills/b", invocation-control: {allow-model: true, allow-hook: true}}]`
- **Recommendation**: Default to list format for easy extension of multiple skills and invocation-control configuration

### 3.9 Markdown Format Agent Configuration (Optional Alternative Format)

In addition to the standard `.yaml` format, the framework also supports writing Agent configuration files in `.md` (Markdown) format:

- **Format rules**: Place `name`, `description`, and other metadata in a `` ```yaml `` code block at the beginning of the file; the Markdown body after the code block is automatically used as the `workflow` field content
- **Use case**: When the `workflow` content is complex with many Markdown headings/tables, using `.md` format provides a better editing experience and IDE highlighting
- **Path**: `applications/<app_name>/workflows/<app_name>_agent.md` (note the `.md` extension)
- **Limitation**: The default recommendation is still `.yaml` format; `.md` format is an optional alternative

Example:

```markdown
```yaml
name: "my_agent"
description: "A demo agent"
tool_call_type: "code_act"
```

# Workflow

## Background
You are a...

## Steps
1. ...
2. ...
```

> Note: When using `.md` format, the `workflow` field should NOT appear inside the YAML code block — it is automatically populated from the file’s Markdown body.

---

## Phase 4: Configuration Validation (Must Execute)

After all files are generated, first run the validation script, which outputs JSON by default. Only proceed to the run phase when validation passes (exit code = 0).

```bash
# <project_root>: Project root directory (dynamically obtained by AI)
# <skill_root>:   Current Skill root directory (dynamically obtained by AI)
cd <project_root>
.venv/bin/python <skill_root>/scripts/validate_application_yaml.py \
  --app-root applications/<app_name>
```

### Validation Script Conventions

- Only supports one argument: `--app-root applications/<app_name>` (required)
- Path basis:
  - Validation script path: AI dynamically obtains the absolute path based on the Skill's directory
  - `--app-root`: Resolved relative to the project root directory
  - The current directory must be within the project directory tree at execution time (the script automatically searches upward for `config/llm.yaml` to locate the project root; test environments typically lack `config/llm.yaml` since it contains sensitive information not committed to VCS, so the script falls back to `config/system.yaml`)
- Python interpreter: Use the Python environment configured for the project
- Default output: JSON (stdout)
- Exit codes:
  - `0`: All validations passed
  - `1`: Configuration errors exist
  - `2`: Argument error or script runtime exception
- JSON output structure:
  - `summary`
  - `errors[]` (each item includes: `file`, `field`, `rule`, `message`, `suggestion`)

---

## Phase 5: Run Guide

After validation passes, tell the user how to run (the `<project_root>` below is dynamically obtained by AI):

```markdown
## 🚀 How to Run

### Method 1: Using runner (recommended)
cd <project_root>
.venv/bin/python src/runner.py applications/<app_name>/workflows/<app_name>_agent.yaml

### Method 2: Using entry script
cd <project_root>
.venv/bin/python applications/<app_name>/<app_name>_app.py

### Method 3: Using AgentLoom CLI (if installed)
cd <project_root>
loom run applications/<app_name>/workflows/<app_name>_agent.yaml
```

> ℹ️ **Checkpoint/Resume is enabled by default**: Every application you create automatically gets checkpoint/resume/heartbeat capability — no extra code needed. If a run is interrupted, re-running with the same task ID will resume from where it left off. To adjust behavior (e.g. retain artifacts for debugging), configure `checkpoint.*` in `config/system.yaml`. See `references/quick-reference.md` Section 6 for the field table.

---

## Predefined Tools Quick Reference (TOP 10 Most Used)

> Full 30+ tool list available at `references/quick-reference.md`.

| Tool Name | Function | Recommended Scenario |
|-----------|----------|---------------------|
| `read_file` | Read full file content | Almost all analysis-type Workers |
| `get_file_outline` | Get code outline | Code structure analysis |
| `browse_directory` | Browse directory structure | Supervisor getting a global view |
| `ripgrep_search_directory` | High-performance regex search | Keyword/pattern location |
| `edit_file` | Find-and-replace editing | Code modification Workers |
| `write_markdown_file` | Write Markdown | Report generation Workers |
| `shell_tool` | Execute Shell commands | Build, test, Git |
| `list_files_glob` | Glob file search | Batch file discovery |
| `get_git_diff_content` | Get Git diff | PR review |
| `write_file` | Create new file | Generating output files |

---

## Key Constraint Reminders (Must Follow When Generating Files)

| # | Constraint | Consequence |
|---|-----------|-------------|
| 1 | **Agent YAML must not contain `model`/`llm`/`langfuse`** | Framework auto-filters + warning |
| 2 | **`worker_agents` must use `path` only, `name` is prohibited** | Error: unable to load |
| 2a | **`worker_agents.path` shorthand must include file extension** (e.g. `scan.yaml`, not `scan`) | Error: missing file extension |
| 2b | **Supervisor YAML must be in `workflows/`**, Worker YAML must be in `workflows/worker_agents/`** | Error: directory not found |
| 3 | **Custom tools are plain functions (no decorators), description comes from docstring** | YAML description is ignored |
| 4 | **`module` and `function` must be paired** | Writing only one causes an error |
| 5 | **Single `workflow` uses `\|`; sequential `workflow` uses non-empty `list[str]` items, each preferably `\|`** | Otherwise formatting is lost or validation fails |
| 6 | **Lists override, not append** | Overriding `default_loaded_tools` requires writing the complete list |
| 7 | **Worker return values are always strings** | `None` → `""`, others → `str()` |
| 8 | **`agent_function_schema.inputs` keys must be valid Python identifiers** | Must satisfy `isidentifier()`, no hyphens or leading digits |

> Full constraint checklist + available values table available at `references/quick-reference.md`.

---

## Complete End-to-End Example

> **Strongly recommended to read** `references/full-example.md` first.
> This document contains a complete `code_review` Application example (Supervisor + 3 Workers),
> demonstrating the full workflow from requirements gathering → plan confirmation → 6 generated files,
> as well as a `simple_scanner` single Agent mode example.

---

## Post-Generation Verification Checklist

After all files are generated, perform the following checks to ensure correctness:

- [ ] Run validation script: `cd <project_root> && .venv/bin/python <skill_root>/scripts/validate_application_yaml.py --app-root applications/<app_name>`
- [ ] Validation script exit code is `0` and JSON output has `errors=[]`
- [ ] All YAML files have correct syntax (`name`, `description`, `workflow` — all three required fields are present and non-empty)
- [ ] `workflow` field uses `|` multiline text block, or a non-empty list whose items are non-empty multiline text blocks
- [ ] Each item in `worker_agents` uses `path` (not `name`)
- [ ] `worker_agents.path` shorthand includes a file extension (e.g. `scan.yaml`, not `scan`)
- [ ] Supervisor YAML is in `workflows/`, Worker YAML is in `workflows/worker_agents/`
- [ ] Files referenced by `worker_agents` paths all exist
- [ ] `worker_agents.path` points to a file (not a directory)
- [ ] Custom tool `module` and `function` appear in pairs
- [ ] Custom tools are plain Python functions (no `@tool` decorator) with complete docstrings
- [ ] Agent YAML does not contain `model`/`llm`/`langfuse` fields
- [ ] `model_type` strategy has been confirmed: use `default_model_type` or explicitly specify an available type in the project
- [ ] If `execution_env` is configured, `type` must be one of `local` / `docker` / `e2b` / `wasm`
- [ ] If `skills` is configured, its structure must be `list / dict / string`
- [ ] The YAML path in the entry script `_app.py` matches the actual file path
- [ ] Keys in `agent_function_schema.inputs` are valid Python identifiers
