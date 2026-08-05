# AgentLoom Agent YAML Configuration Complete Reference

> **Document scope**: This document details **every** configuration parameter in Agent YAML.
> For override relationships between configuration files, see [Configuration System Overview](config-overview.md).
> For `config/system.yaml`, see [System Configuration Reference](system_config.md).
> For `config/llm.yaml`, see [LLM Configuration Reference](llm_config.md).

Agent YAML is the configuration file in the AgentLoom framework that **defines the behavior of a single Agent**, controlling the Agent's role description, workflow instructions, available tools, model selection, execution environment, skill packages, and more. Agents are divided into two roles: **Supervisor** (multi-Agent orchestrator) and **Worker** (specific task executor).

> ⚠️ **LLM Configuration Isolation**: `model`/`llm`/`langfuse` in Agent YAML are automatically filtered with a warning. LLM parameters can only be defined in `config/llm.yaml`; Agents select which predefined model type to use via the `model_type` field.

---

## Table of Contents

- [1. Two Agent Roles](#1-two-agent-roles)
- [2. Quick Reference: Complete YAML Templates](#2-quick-reference-complete-yaml-templates)
- [3. Field Reference Manual](#3-field-reference-manual)
  - [3.1 Required Fields](#31-required-fields)
  - [3.2 Optional Common Fields](#32-optional-common-fields)
  - [3.3 Supervisor-Specific Fields](#33-supervisor-specific-fields)
  - [3.4 Worker-Specific Fields](#34-worker-specific-fields)
  - [3.5 execution_env — Execution Environment](#35-execution_env--execution-environment)
  - [3.6 tool_call_type — Interaction Mode](#36-tool_call_type--interaction-mode)
  - [3.7 model_type — Model Selection](#37-model_type--model-selection)
  - [3.8 skills — Skill Package Configuration](#38-skills--skill-package-configuration)
  - [3.9 prompt — Custom Prompt](#39-prompt--custom-prompt)
  - [3.10 planning_interval — Planning Interval](#310-planning_interval--planning-interval)
  - [3.11 todo.mode — Task Tracking](#311-todomode--task-tracking)
  - [3.11 concurrency — Concurrency Configuration](#311-concurrency--concurrency-configuration)
- [4. Tool Configuration Details](#4-tool-configuration-details)
  - [4.4 Advanced Pattern: Wrapping Agent as a Python Tool Function](#44-advanced-pattern-wrapping-agent-as-a-python-tool-function)
- [5. Worker Export as Callable Tool](#5-worker-export-as-callable-tool)
- [6. worker_agents Path Resolution Rules](#6-worker_agents-path-resolution-rules)
- [7. Common Errors and Troubleshooting](#7-common-errors-and-troubleshooting)
- [8. Complete Practical Examples](#8-complete-practical-examples)
- [9. Configuration Override Relationships](#9-configuration-override-relationships)
- [Appendix: Field Quick Reference Table](#appendix-field-quick-reference-table)

---

## 1. Two Agent Roles

| Role | Purpose | File Location | Core Characteristic |
|------|------|----------|----------|
| **Supervisor** | Multi-Agent collaboration orchestrator | `applications/<app>/workflows/<name>.yaml` | Has `worker_agents` field, schedules multiple Workers |
| **Worker** | Specific task executor | `applications/<app>/workflows/worker_agents/<name>.yaml` | Has `agent_function_schema` field, can be exported as a tool for Supervisor to call |

```
Supervisor (Main Agent)
  ├── Calls Worker A (project_scan)
  ├── Calls Worker B (data_analysis)
  └── Calls Worker C (report_generation)
```

> **Single Agent Mode**: If only one Agent is needed to work independently, just write a Worker YAML — no Supervisor required.

---

## 2. Quick Reference: Complete YAML Templates

### 2.1 Supervisor Complete Template

```yaml
# ============================================================
# Supervisor Agent Configuration Template
# File location: applications/<app>/workflows/<agent_name>.yaml
# ============================================================

# ---- Required Fields (3) ----
name: "my_check_agent"
description: |
  As the code review supervisor agent, your core responsibility is...
workflow: |
  # My Check Workflow
  ## Steps
  1. Call get_module_context to retrieve context
  2. Call project_scan for preparation
  3. Output the final report

# ---- Optional Fields ----
tools:
  - name: "get_module_context"
    module: "applications.my_app.agent_tools.module_context"
    function: "get_module_context"

model_type: "powerful"                   # Options: "powerful", "fast", "summary", or custom key
tool_call_type: "code_act"               # Options: "code_act", "tool_call"

# ---- Supervisor-Specific ----
worker_agents:
  - path: "applications/my_app/workflows/worker_agents/project_scan.yaml"
  - path: "applications/my_app/workflows/worker_agents/data_analysis.yaml"

# ---- Other Optional Fields ----
execution_env:
  type: "local"                          # Options: "local", "docker", "e2b", "wasm"

prompt:
  path: "applications/my_app/sysprompt/code_agent.yaml"

skills:
  - path: "applications/my_app/skills"
    platform: "Claude"                   # Optional: specify skill platform adaptation
```

### 2.2 Worker Complete Template

```yaml
# ============================================================
# Worker Agent Configuration Template
# File location: applications/<app>/workflows/worker_agents/<name>.yaml
# ============================================================

# ---- Required Fields (3) ----
name: "project_scan"
description: "Project structure scanning agent"
workflow: |
  You are a senior engineer responsible for...
  ## Output Requirements
  - A. File inventory and role classification
  - B. Dependency graph

# ---- Optional Fields ----
tools:
  - name: "read_file"
  - name: "write_file"
  - name: "get_file_outline"
  - name: "get_module_context"
    module: "applications.my_app.agent_tools.module_context"
    function: "get_module_context"

model_type: "powerful"
tool_call_type: "code_act"
max_steps: 40                            # Maximum execution steps (default: 80)
planning_interval: 3                     # Force re-planning every N steps
todo: {mode: "auto"}                     # auto | on | off

# ---- Worker-Specific: Callable Tool Contract ----
# Note: Parameter names under inputs are customizable, as long as they are valid Python identifiers
agent_function_schema:
  description: |
    Preparation phase analysis agent, responsible for static code scanning...
  inputs:
    param1:                              # Custom parameter name, valid Python identifier
      description: "Description of the first parameter"
      required: true
    param2:
      description: "Description of the second parameter"
      required: false
  output:
    description: "Analysis summary text, detailed report generated in workspace"

execution_env:
  type: "local"
```

---

## 3. Field Reference Manual

### 3.1 Required Fields

Supervisor and Worker share 3 required fields:

| Field | Type | Validation Rule | Description |
|------|------|----------|------|
| `name` | `str` | Non-empty string | Agent unique identifier. In Worker, also serves as the exported tool function name |
| `description` | `str` | Non-empty string | Agent role description. In Supervisor single-string workflows, participates in task assembly; list workflow items are executed as authored |
| `workflow` | `str` or `list[str]` | Non-empty string, or non-empty list of non-empty strings | Workflow instruction text. Supports Markdown and Mermaid flowcharts. See [Writing Guidelines](#workflow-writing-guidelines-and-recommendations) below |

#### Division of `description` and `workflow`

| Field | Responsibility | What to Write | What NOT to Write |
|------|------|--------|----------|
| `description` | **Role positioning** (one or two sentences) | "As XX agent, your core responsibility is YY" | Don't write detailed processes or specific steps |
| `workflow` | **Complete execution instructions** | Background, responsibilities, flowchart, stage descriptions, output requirements | Don't repeat the role positioning from description |

> For `workflow: |`, the framework keeps the existing single-run behavior and sends one assembled task to the LLM. For top-level Supervisor execution (`loom run` / `run_app`), `workflow: list[str]` executes each list item sequentially with the same runtime Agent; the first run uses normal reset behavior, later runs use `reset=False`, and the returned value is the last run result. AgentLoom does not add stage labels or wrapper instructions to list items. When a Worker is exported as a tool, list items are embedded in order in the generated task spec for that single tool call.

Sequential workflow example:

```yaml
workflow:
  - |
    # First workflow
    Build the initial analysis and save any findings that the next step should use.
  - |
    # Second workflow
    Continue from the previous run's memory and produce the final answer.
```

#### Goal Mode (Supervisor only)

```yaml
goal:
  enabled: true
  token_budget: 120000  # optional; omit for unlimited
```

`goal: true` and `goal: false` are also accepted. Mapping form requires an
explicit boolean `enabled` and allows only optional positive-integer
`token_budget`; unknown fields and permissive type coercions fail. Worker YAML
must not contain any `goal` key.

When enabled, the objective is derived from `description + workflow + runtime
task`. Prefer one multiline workflow. A list is numbered and merged into one
initial objective context instead of using the ordinary sequential multi-run
semantics above. Normal final answers and `max_steps` end only one continuation
segment; the root Supervisor must call `update_goal(complete, evidence)`. Worker
model usage counts against the same optional soft budget. See [Goal Mode](goal_mode.md)
for lifecycle, resume, persistence, CLI, TUI, and schedule behavior.

#### Workflow Writing Guidelines and Recommendations

`workflow` is the Agent's most critical configuration — it is essentially the **task instruction (Prompt)** sent to the LLM. A well-structured workflow can significantly improve Agent execution quality.

**Recommended Structure (Five-Part)**:

```
① Background & Role        ← Establish professional context
② Core Responsibilities & Constraints ← Define what must/must not be done
③ Execution Flow (Mermaid) ← Define steps and branches (framework special handling)
④ Detailed Step Descriptions ← Expand each flow node
⑤ Output Requirements      ← Constrain final deliverable format
```

**① Background & Role**

Establish professional context at the beginning of the workflow to let the LLM "get into character". The more specific the role, the more professional the output.

**Example**:

```markdown
You are a **senior code repository architecture analysis engineer**, skilled in code structure organization, module dependency analysis, and architecture documentation.
Your current task is to perform **directory-by-directory architecture analysis** on the target code repository, generating readable architecture documentation.
```

> **Key points**: State the professional background, current task objectives, and what is being analyzed.
> Reference Anthropic best practices: "Give Claude a role — Even a single sentence makes a difference."

**② Core Responsibilities & Constraints**

Use **numbered lists** to clearly define the Agent's mandatory responsibilities and prohibited behaviors. The more specific, the better — don't expect the LLM to infer your intent.

**Example**:

```markdown
### Core Responsibilities (Must Fulfill)
1. **Directory-by-directory analysis**: Call analysis tools for each target directory to generate architecture documentation
2. **Checkpoint resume**: Skip completed directories, only process incomplete or failed ones
3. **Result aggregation**: Output summary report after analysis (success/failure statistics + deliverable paths)

### Constraints (Prohibited Behaviors)
- ❌ Do not skip reporting failed directories
- ❌ Do not modify source code files
- ✅ All analysis conclusions must be based on actual code content, no speculation allowed
```

> **Key points**: Use numbered lists to ensure step completeness; use **bold** for critical rules; stating "do X" is more effective than "don't forget to do X" (e.g., "must call X first" is better than "don't forget to call X").

**③ Execution Flow (Mermaid Flowchart)**

Use Mermaid to define the core execution flow.

> ⚠️ **Framework Special Handling**: The framework automatically detects ` ```mermaid ` code blocks in workflow, extracts them, and wraps them with `<workflow>` XML tags. **When Mermaid blocks are present, the framework additionally injects a "must be followed strictly" instruction to the LLM**. Therefore, placing the core flow in a Mermaid block not only improves readability but also lets the framework strengthen flow constraints for you.

**Example**:

````markdown
```mermaid
flowchart TD
  A[Read config and target directory list] --> B[Call run_analysis_loop for directory-by-directory analysis]
  B --> C{Failed directories?}
  C -- Yes --> D[Call get_analysis_summary to output failure details]
  C -- No --> E[Call get_analysis_summary to output success summary]
  D --> F[End]
  E --> F
```
````

> **Key points**:
> - The flowchart should only show **main flow and key branches**; don't cram every detail into it
> - Node names should be clear, use descriptive text rather than coded abbreviations
> - Branch conditions use `{condition?}`, e.g., `C{Failed items?} -- Yes --> D[Retry]`
> - Mermaid syntax is validated by the framework (depends on `mermaid-syntax-parser`); syntax errors output a warning at runtime

**④ Detailed Step Descriptions**

Expand on each key node from the Mermaid flowchart. Recommended unified sub-structure:

**Example**:

```markdown
### Step 1: Directory-by-Directory Architecture Analysis

**Objective**: Complete LLM architecture analysis for all target directories.

**Input**:
- Output directory specified by environment variable `REPO_MAP_OUTPUT_DIR`

**Actions**:
1. Call the `run_analysis_loop` tool
2. The tool internally calls sub-Agents per directory in rank priority order
3. Completed directories are automatically skipped (checkpoint resume)

**Success Criteria**:
- All directories analyzed, or failed directories recorded to progress file

**Failure Handling**:
- Single directory failure → Record error, continue to next directory
- Tool throws exception → Stop immediately and report error
```

> **Key points**: Each step should have clear **success criteria** and **failure handling** to prevent the LLM from improvising when encountering exceptions.

**⑤ Output Requirements**

Clearly define the format, required content, and prohibitions for final deliverables.

**Example**:

```markdown
## Output Requirements
- Format: Markdown summary report
- Must include: Number of completed directories, failed directories, deliverable paths
- Failed directories must list failure reasons
- Do not omit any failure information
```

> **Key points**: If there are specific format requirements (e.g., JSON, Markdown tables, specific section structure), constrain them here.

#### Comprehensive Template

````yaml
workflow: |
  # [Task Name]

  ## Background
  You are a **[professional role]**. Your current task is [one sentence describing the task objective].

  ## Core Responsibilities
  1. **[Responsibility 1]**: [specific description]
  2. **[Responsibility 2]**: [specific description]
  3. **[Responsibility 3]**: [specific description]

  ## Constraints
  - ❌ [Prohibited behavior 1]
  - ❌ [Prohibited behavior 2]
  - ✅ [Recommended practice]

  ## Execution Flow

  ```mermaid
  flowchart TD
    A[Step 1: Get input] --> B[Step 2: Core processing]
    B --> C{All successful?}
    C -- Yes --> D[Step 3: Output success report]
    C -- No --> E[Step 3: Output failure details]
    D --> F[End]
    E --> F
  ```

  ## Step Details

  ### Step 1: [Name]
  **Objective**: ...
  **Actions**:
  1. ...
  **Success Criteria**: ...
  **Failure Handling**: ...

  ### Step 2: [Name]
  ...

  ## Output Requirements
  - Format: [format]
  - Must include: [content items]
  - Prohibited: [prohibited items]
````

#### Writing Notes

- **YAML format**: for a single workflow, use `workflow: |` to preserve newlines and indentation. For sequential workflows, use `workflow:` as a non-empty list of `|` blocks.
- **Avoid hardcoded paths**: Don't hardcode file paths in workflow; get them dynamically via tools (e.g., `get_module_context`)
- **Bold critical rules**: Use `**bold**` to highlight rules the LLM must follow
- **Numbered for ordering**: Use numbered lists (`1. 2. 3.`) for multi-step processes, not unordered lists
- **Mark inferences**: Require the LLM to label uncertain content with 【Inference】 to avoid hallucinations mixing into conclusions
- **Mermaid syntax**: Ensure Mermaid syntax is correct; the framework validates and outputs warnings on errors

---

### 3.2 Optional Common Fields

| Field | Type | Default | Description |
|------|------|--------|------|
| `tools` | `list[dict]` | `[]` | Tool list. See [Section 4](#4-tool-configuration-details) |
| `model_type` | `str` | Configured global `default_model_type` | Model selection. See [3.7](#37-model_type--model-selection) |
| `tool_call_type` | `str` | `"code_act"` | Agent interaction mode. See [3.6](#36-tool_call_type--interaction-mode) |
| `execution_env` | `dict` | `{type: "local"}` | Execution environment configuration. See [3.5](#35-execution_env--execution-environment) |
| `prompt` | `str` or `dict` | Framework built-in | Custom System Prompt template. See [3.9](#39-prompt--custom-prompt) |
| `planning_interval` | `int` | Not set | Force re-planning every N steps. See [3.10](#310-planning_interval--planning-interval) |
| `todo` | `dict` | `{mode: "auto"}` | Current-task progress tracking. See [3.11](#311-todomode--task-tracking) |
| `concurrency` | `int`/`str` | Not set | Concurrency level when this Agent is batch-invoked. See [3.11](#311-concurrency--concurrency-configuration) |
| `skills` | `list`/`dict`/`str` | Not set | Private skill package configuration. See [3.8](#38-skills--skill-package-configuration) |
| `hooks` | `dict` | Not set | Independent direct Hooks and explicit Hook Bundles. See [Hooks](hooks.md) |
| `max_steps` | `int` | `80` | Maximum execution steps. Agent is forcefully terminated when exceeded |

---

### 3.3 Supervisor-Specific Fields

| Field | Type | Default | Description |
|------|------|--------|------|
| `worker_agents` | `list[dict]` | `[]` | Worker Agent path list. Each item must have a `path` field; **`name` field is prohibited**. See [Section 6](#6-worker_agents-path-resolution-rules) |

---

### 3.4 Worker-Specific Fields

| Field | Type | Default | Description |
|------|------|--------|------|
| `agent_function_schema` | `dict` | Not set | Worker callable tool contract. Worker is exported as a tool when present and valid. See [Section 5](#5-worker-export-as-callable-tool) |

> ⚠️ **Worker config isolation**: A Worker's effective configuration is resolved from global/app config plus the **Worker YAML itself**. It does **not** inherit runtime overrides from the Supervisor that called it. If a Worker needs extra filesystem or shell permissions, repeat the relevant whitelisted overrides (for example `tool_access_control.path_validation`) in the Worker YAML.

---

### 3.5 `execution_env` — Execution Environment

| Sub-field | Type | Default | Required | Options | Description |
|--------|------|--------|------|--------|------|
| `type` | `str` | `"local"` | ❌ | `"local"` / `"docker"` / `"e2b"` / `"wasm"` | Executor type (auto-lowercased). `"host"` has been removed |
| `executor_kwargs` | `dict` | `{}` | ❌ | Free key-value pairs | Executor parameters, passed through as-is |

**`type` option descriptions**:

| Value | Description | Default Tool Loading |
|----|------|------------|
| `"local"` | Local execution, uses host Shell and filesystem | ✅ Loaded |
| `"docker"` | Docker container execution | ❌ Not loaded |
| `"e2b"` | E2B cloud sandbox execution | ❌ Not loaded |
| `"wasm"` | WebAssembly sandbox execution | ❌ Not loaded |

**Validation rules**: `type` must be a non-empty string and one of the 4 values above; `executor_kwargs` must be a dictionary. Shell path is auto-detected from the `$SHELL` environment variable.

**Examples**:

```yaml
# Local execution
execution_env:
  type: "local"

# Docker remote execution
execution_env:
  type: "docker"
  executor_kwargs:
    host: "127.0.0.1"
    port: 8888
    image_name: "my-jupyter-kernel:local"
```

> This field can override system configuration in Agent YAML (part of the [overlay whitelist](#92-overridable-field-whitelist)).

> ⚠️ **Mode restriction**: `execution_env` only takes effect in `code_act` mode. In `tool_call` mode, `executor_type` and `executor_kwargs` are silently ignored (`ToolCallingAgentV2` does not execute code, so execution environment settings are not applicable).

---

### 3.6 `tool_call_type` — Interaction Mode

| Option | Agent Type | Call Method | Flexibility | Recommended Scenario |
|--------|-----------|----------|--------|----------|
| `"tool_call"` | `ToolCallingAgentV2` | Structured tool_call messages | Structured (one tool call per step, clear and traceable) | **Recommended for Supervisor**, workflow Workers |
| `"code_act"` | `CodeAgentV2` | Writes Python code to call tools | High (loops, conditions, multi-step orchestration) | Workers that need coding, highly flexible tasks |

**Default**: `"code_act"`
**Validation**: Only `"code_act"` or `"tool_call"` allowed; other values raise an error.

#### How to Choose?

| Scenario | Recommended Mode | Reason |
|----------|-----------------|--------|
| **Supervisor orchestrating multiple Workers** | **`tool_call`** ✅ | Each step’s Worker call, parameters, and results are structured records, making it easy to monitor and audit each Worker’s execution status |
| **Workflow / fixed pipeline** | **`tool_call`** ✅ | Structured output, predictable, traceable, clear steps |
| **Coding / highly flexible tasks** | **`code_act`** ✅ | Needs loops, conditionals, exception handling, data transformation, and other Python programming capabilities |
| **Open-ended exploration tasks** | **`code_act`** ✅ | Uncertain number of steps, requires dynamic decision-making and complex control flow |

> **Core principle**: `tool_call` suits **highly structured** scenarios (orchestration, fixed workflows) where steps are clear and traceable, giving full visibility into each tool’s execution; `code_act` suits **highly flexible** scenarios (coding, complex logic) that leverage Python’s programming expressiveness.

> 💡 **Mode-specific parameters**: The following configurations only take effect in `code_act` mode and are silently ignored in `tool_call` mode:
>
> | Parameter | Reason |
> |-----------|--------|
> | `execution_env` (`executor_type` / `executor_kwargs`) | `tool_call` mode does not execute code, so no execution environment is needed |
> | `code_agent.additional_authorized_imports` | Import whitelists only apply to code execution |
> | `code_agent.additional_functions` | Built-in function whitelists only apply to code execution |

---

### 3.7 `model_type` — Model Selection

Agent YAML cannot directly modify LLM parameters, but can **select** which model type defined in `config/llm.yaml` to use via `model_type`.

| Predefined Type | Use Case | Description |
|-----------|----------|------|
| `"powerful"` | Supervisor orchestration, complex reasoning, code generation | Strong model, higher cost |
| `"fast"` | Simple classification, routing, lightweight Worker | Fast response, lower cost |
| `"summary"` | Text summarization, information extraction | Medium capability |

Custom type names from `llm.yaml` are also supported (e.g., `"code_review"`).

**Resolution logic**:
1. Agent YAML specifies `model_type` → Uses that value; if the type doesn't exist, **raises an error (`ValueError`) directly, no silent fallback**.
2. Not specified → Uses the global `config/llm.yaml` `model.default_model_type`. If `default_model_type` is omitted or the resolved type does not exist, **also raises an error directly**.

---

### 3.8 `skills` — Skill Package Configuration

`skills` in Agent YAML declares the Agent's private skill packages. **Not through the overlay whitelist**; loaded through an independent three-layer stacking mechanism.

#### Three-Layer Loading Order

```
Layer 1: config/system.yaml global skills      ← C.get("skills")
Layer 2: AGENT_ROOT/skills/ directory auto-discovery  ← load_skills_from_directory()
Layer 3: skills field in Agent YAML              ← Read directly from raw YAML dict
```

The three layers are **additive**, not overriding. Skills with the same name are overridden by later-loaded ones (with a warning).
`AGENT_ROOT` refers to the project root directory containing `config/system.yaml` (`C.agent_root`), not the directory of the current Agent YAML file.

#### Disabling Global Skills (opt-out)

Set `skills` to an empty list in the app-level `config/system.yaml` to **completely disable** Layer 1 and Layer 2 loading (global entries + directory auto-discovery are both skipped), leaving only Layer 3 Agent-private Skills:

```yaml
# applications/<app>/config/system.yaml
skills: []   # Explicit opt-out: skip all global skills including AGENT_ROOT/skills/ directory
```

| `skills` value | Behavior |
|---|---|
| Not configured / `null` | Global entries not loaded, but `AGENT_ROOT/skills/` directory is still auto-discovered |
| `[]` (empty list) | **Fully disabled**: both global entries and directory auto-discovery are skipped |
| `[entries...]` | Load specified entries AND auto-discover `AGENT_ROOT/skills/` directory |

#### Three Supported Formats

**Format 1: List format (recommended)**

```yaml
skills:
  - path: "skills/agent-recall-with-files"
    load-mode: "eager"
  - "skills/another-skill"              # Plain strings work as list items too
```

**Format 1b: Shared policy with `items`**

```yaml
skills:
  load-mode: "on-demand"
  allow-scripts: false
  allow-network: false
  items:
    - "skills/safe-review"
    - path: "skills/strict-review"
      load-mode: "eager"
```

**Format 2: Dictionary format (single skill)**

```yaml
skills:
  path: "skills/agent-recall-with-files"
  platform: "Claude"
```

**Format 3: String format (simplest)**

```yaml
skills: "skills/agent-recall-with-files"
```

> Dictionary and string formats are automatically converted to single-element lists for processing.

#### List Item Sub-fields

| Sub-field | Type | Default | Required | Description |
|--------|------|--------|------|------|
| `path` | `str` | — | ✅ Required | Skill package path. Relative paths resolve from `AGENT_ROOT`. The runtime loads only a package entrypoint named `SKILL.md` / `skill.md` (case-insensitive), not loose Markdown or `skills.md` |
| `platform` | `str` | `null` | ❌ Optional | Specify the platform the skill is adapted for (e.g., `"Claude"`), used by `tools_mapping` |
| `load-mode` | `str` | `on-demand` | ❌ Optional | `on-demand` puts only the catalogue in the prompt; `eager` injects the full skill body |
| `allow-scripts` | `bool` | `true` | ❌ Optional | Set to `false` to block `run_skill_script` for this skill |
| `allow-network` | `bool` | `true` | ❌ Optional | Set to `false` to block common network commands inside `run_skill_script` |

The group-level policy is inherited by `items`; an item-level field overrides it. Skill discovery and loading never declare, enable, or execute Hooks. A `hooks` field in `SKILL.md` or `enable-hooks` in Skill configuration is a migration error; configure the independent top-level [`hooks`](hooks.md) interface instead.

**Validation**: `skills` overall must be `list`, `dict`, or `str`; otherwise raises `skills must be a list, dict, or string path`.

---

### 3.9 `prompt` — Custom Prompt

Used to override the framework's built-in System Prompt template.

#### Two Formats

```yaml
# Format 1: Direct string path
prompt: "applications/my_app/sysprompt/code_agent.yaml"

# Format 2: Dictionary form (must include path key)
prompt:
  path: "applications/my_app/sysprompt/code_agent.yaml"
```

#### Path Resolution Rules

- **Relative path**: Resolved based on `AGENT_ROOT` (project root directory containing `config/system.yaml`)
- **Absolute path**: Used directly

#### Prompt Resolution Priority (highest to lowest)

| Priority | Source | Description |
|--------|------|------|
| 1 | Function parameter `prompt_template_path` | Explicitly passed in code |
| 2 | Agent YAML `prompt` field | Current document configuration |
| 3 | Model family variant | `<prompts_dir>/<family>/toolcalling_agent.yaml` (user activates by removing `.example` suffix) |
| 4 | Local override | `<prompts_dir>/structured_code_agent.yaml` or `toolcalling_agent.yaml` (user activates by removing `.example` suffix) |
| 5 | smolagents built-in default | smolagents package's built-in prompt (no file needed) |

> **Customization**: All `.example.yaml` files (including those under `anthropic/`, `openai/`, `gemini/` directories) are reference templates. To activate a custom prompt, simply remove the `.example` suffix:
> ```bash
> # Activate global custom prompt (code_act mode)
> mv structured_code_agent.example.yaml structured_code_agent.yaml
>
> # Activate anthropic model-family variant
> mv anthropic/toolcalling_agent.example.yaml anthropic/toolcalling_agent.yaml
> ```
> To revert to defaults, add the `.example` suffix back.

**Validation**: Dictionary form must include the `path` key; otherwise raises `must include 'path' when prompt is a mapping`. Prompt file must be a valid YAML mapping.

> This field can override system configuration in Agent YAML (part of the [overlay whitelist](#92-overridable-field-whitelist)).

---

### 3.10 `planning_interval` — Planning Interval

When set, the Agent is forced to perform a planning step every N steps.

**Type**: `int` (positive integer)
**Default**: Not set (periodic planning not enabled)

**Validation rules**:

| Input Value | Parse Result | Description |
|--------|---------|------|
| `3` | `3` | Normal positive integer |
| `"3"` | `3` | String integer auto-conversion supported |
| `0` / `-1` | Not set | Zero and negatives are equivalent to not set |
| `null` / omitted | Not set | Not enabled |
| `true` / `false` | Not set | Bool type is ignored (`true` doesn't become `1`) |
| `""` / `"abc"` | Not set | Empty or non-numeric strings are ignored |

**Example**:

```yaml
planning_interval: 3    # Force re-planning every 3 steps
```

`planning_interval` only controls periodic model planning. It does not enable,
disable, or schedule Todo calls. Configure Todo independently with [`todo.mode`](#311-todomode--task-tracking).

---

### 3.11 `todo.mode` — Task Tracking

Todo tracks the current Agent's progress for the current task. It is not a
long-term project manager and is independent from `planning_interval` and the
Agent's explicit `tools` list.

```yaml
todo:
  mode: "auto"  # auto | on | off; quote on/off for YAML 1.1 loaders
```

| Mode | Behavior |
|------|----------|
| `auto` | Default. `todo_write` is available and the model decides whether a multi-step task benefits from tracking. |
| `on` | `todo_write` is available. For a non-trivial multi-step task whose scope is already clear, the model is strongly instructed to make a standalone `todo_write` its first tool call. Minimal read-only discovery may happen first only when needed to ground the list. |
| `off` | The tool, Todo prompt policy, and current Todo snapshot are hidden from the model. |

The value may be set globally in `config/system.yaml`, in an Application YAML,
or in an Agent YAML. The more specific layer wins. Only `auto`, `on`, and `off`
are valid.

`todo_write` replaces the complete list atomically. Each item contains
`content` and one of `pending`, `in_progress`, `completed`, or `cancelled`.
There may be at most one `in_progress` item. `cancelled` requires a
`cancel_reason` and means the Agent has determined that the item is no longer
needed; failure or lack of time is not cancellation. Passing an empty list
clears the snapshot.

With checkpointing enabled, state is stored in the task checkpoint directory
as `todos.json`, isolated by Agent path and managed by the checkpoint resume,
locking, and cleanup lifecycle. With checkpointing disabled it lives only in
the current run's memory. A malformed file is quarantined with a warning and
execution continues with an empty snapshot. The current canonical snapshot is
re-injected as a system message on each model call; Todo does not add a separate
model call and does not block the final answer.

---

### 3.12 `concurrency` — Concurrency Configuration

Controls the maximum concurrency when this Agent is batch-invoked. Typically used for Worker Agents — when the application layer needs to batch-invoke the same Worker on multiple inputs (e.g., multiple directories, multiple files), this field determines how many Agent instances run simultaneously.

**Type**: `int` (positive integer) or `str` (`"auto"`)
**Default**: Not set (equivalent to `"auto"`)

**Possible values**:

| Value | Meaning |
|------|------|
| `auto` | Auto-calculated: `min(RPM, 10)`, actual request pacing controlled by rate limiter (`interval = 60/RPM`) |
| `1` | Explicit sequential execution, one at a time |
| `N` (positive int) | Fixed concurrency, at most N instances simultaneously |
| Not set / `null` | Equivalent to `auto` |

> In `auto` mode, RPM is read from the corresponding `model_type`'s `requests_per_minute` in `config/llm.yaml`. Thread count = `min(RPM, 10)`, the rate limiter paces actual requests at `60/RPM` second intervals.

**Thread safety**: The framework creates an **independent Agent instance** for each concurrent call (sharing Model and Config, but Agent's `memory`, `state`, and other stateful properties are fully isolated). This follows the design patterns of Cline's `new SubagentRunner()` and LangGraph's `Send()`.

**Examples**:

```yaml
# Worker Agent: directory analysis (supports concurrent batch invocation)
name: "dir_architecture_analysis"
model_type: "powerful"
concurrency: auto          # Auto-calculate concurrency

workflow: |
  ...
```

```yaml
# Worker Agent: fixed concurrency of 6
name: "file_processor"
model_type: "fast"
concurrency: 6
```

**Application-layer usage — `tool.batch()`**:

After a Worker Agent with `concurrency` configured is loaded via `create_agent_as_tool()`, the returned tool function has a `.batch()` method for one-line parallel execution:

```python
# Load Worker Agent as tool (returns single Callable, with built-in cache)
tool = YamlAgentFactory.create_agent_as_tool("worker.yaml")

# Build task list
tasks = [
    {"dir_path": "src/api", "index_content": "..."},
    {"dir_path": "src/utils", "index_content": "..."},
    {"dir_path": "src/core", "index_content": "..."},
]

# One-line parallel execution — auto-reads concurrency from YAML
results = tool.batch(tasks)

# Override YAML config
results = tool.batch(tasks, concurrency=3)

# With progress callback
results = tool.batch(tasks, on_progress=lambda done, total, r: print(f"{done}/{total}"))
```

**Priority chain**: `tool.batch(concurrency=N)` param > YAML `concurrency` field > `auto`

> ⚠️ **Applicable scenarios**: `concurrency` is for batch scenarios where "the same Worker is called multiple times with different inputs" (e.g., analyzing 100 directories, processing 50 files). It does not affect the Supervisor's own execution.

---

## 4. Tool Configuration Details

### 4.1 Two Tool Types

#### Predefined Tools (only need `name`)

```yaml
tools:
  - name: "read_file"
  - name: "shell_tool"
```

#### Fixed Tool Arguments

Use `fixed_args` when an Agent YAML should lock specific tool parameters. Fixed
arguments are bound by the framework, removed from the LLM-visible tool schema,
and cannot be overridden by a tool call.

```yaml
tools:
  - name: "codex"
    fixed_args:
      cwd: "."
      sandbox: "workspace-write"
      search: "false"
```

#### Predefined Tools + Metadata Override

Agent YAML can override per-tool metadata defined in `config/system.yaml` `tool_metadata` section:

```yaml
tools:
  - name: "grep_search"
    max_result_chars: 10000        # Override default 20000
    disable_type_coercion: true    # Disable auto type coercion for this tool
```

See [system_config.md §10 tool_metadata](system_config.md#10-tool_metadata--tool-metadata-configuration) for available fields.

#### Dynamically Loaded Tools (need `name` + `module` + `function`)

```yaml
tools:
  - name: "get_module_context"
    module: "applications.my_app.agent_tools.module_context"
    function: "get_module_context"
```

> **Important**: Dynamically loaded tools' descriptions are automatically extracted from the Python function's `__doc__` (Docstring). The `description` field in YAML, even if configured, is ignored by the framework. Write your documentation in the Python function directly.

**Validation rules**: `module` and `function` must appear together; specifying only one raises an error.

### 4.2 Complete Predefined Tool List

| Tool Name | Function |
|--------|----------|
| `read_file` | Read file content (supports offset/limit for ranges) |
| `write_file` | Create new file or overwrite existing |
| `edit_file` | Apply one or more unique text edits |
| `write_markdown_file` | Write Markdown file |
| `write_markdown_file_raw` | Write raw Markdown file |
| `append_markdown_sections` | Append Markdown sections |
| `get_file_outline` | Get code outline (functions/classes/structs) |
| `list_directory` | List directory structure |
| `grep_search` | Regex search file contents (powered by ripgrep) |
| `glob_search` | Glob pattern file search |
| `ast_grep_search_file` | AST pattern search |
| `lsp_find_definition` | Find symbol definition |
| `lsp_find_references` | Find symbol references |
| `lsp_get_document_symbols` | List document symbols |
| `lsp_hover` | Show hover/type information |
| `lsp_get_workspace_symbols` | Search workspace symbols |
| `loom_retrieve_context` | Retrieve compressed context refs |
| `shell_tool` | Execute shell commands (whitelist-restricted) |
| `check_background_task` | Read background task status and output |
| `kill_background_task` | Terminate a background task |
| `list_background_tasks` | List active and recent background tasks |
| `load_skill` | Load specified skill |
| `list_skills` | List available skills |
| `session_search` | Search redacted records from prior Runs |
| `session_scroll` | Read surrounding events from a prior Run |
| `memory` | Read or propose durable Project/Application facts |
| `skill_manage` | Create or update generated Skill proposals |
| `todo_write` | Update the current task plan when Todo is enabled |

For toolset membership, implementation-loading rules, and the validation
matrix, see [Built-in Tool Catalog](tool_catalog.md).

### 4.3 Tool Loading Priority

1. **Default toolsets**: Toolsets in `default_toolsets` from `config/system.yaml` are auto-loaded
2. **Agent tools**: Tools in the Agent YAML `tools` list
3. **Deduplication rule**: Same-named tools are overridden by later-loaded ones

> When `execution_env.type` is `"docker"` or `"e2b"`, default tools are **NOT auto-loaded**.

### 4.4 Advanced Pattern: Wrapping Agent as a Python Tool Function

When a Worker Agent call needs **complex pre/post processing** (e.g., loop orchestration, checkpoint resume, error isolation, progress persistence), you can wrap the Agent in a regular Python tool function, then register it in the Supervisor's `tools` field via `module + function`.

The core idea of this pattern is: **Python control flow + Agent intelligence** — let deterministic operations (read files, write files, loops, error handling) happen at the Python layer, and only delegate LLM reasoning parts to the Agent.

#### 4.4.1 When to Use This Pattern

| Scenario | Recommended Approach | Reason |
|------|----------|------|
| Call Agent once, return result directly | `worker_agents` auto-registration | Simple and direct, YAML declaration only |
| Need to read files/prepare context before calling Agent | **Python wrapper** | Deterministic operations shouldn't waste LLM tokens |
| Need to loop-call Agent (batch processing) | **Python wrapper** | Python for loops are more reliable than LLM CodeAct |
| Need checkpoint resume / progress persistence | **Python wrapper** | Write back progress file immediately per iteration, crash-safe |
| Need error isolation (single item failure doesn't interrupt) | **Python wrapper** | try-except precise capture, continue processing next item |
| Agent output needs post-processing (write files, format, aggregate) | **Python wrapper** | Deterministic operations at the Python layer |

#### 4.4.2 Three Agent-Tool Paths Comparison

| Dimension | Path A: `worker_agents` Auto-Register | Path B: Dynamic Tool (`module + function`) | Path C: Python-Wrapped Agent Tool |
|----------|--------------------------------|------------------------------------------|-------------------------------|
| **Registration** | Supervisor YAML `worker_agents` field | Supervisor YAML `tools` field (`module + function`) | Same as Path B (`tools` field `module + function`) |
| **Contains Agent** | ✅ Auto-creates Agent | ❌ Plain Python function | ✅ Function internally calls `create_agent_as_tool()` |
| **Requires Python Code** | ❌ Pure YAML declaration | ✅ Need to write tool function | ✅ Need to write wrapper function |
| **Pre/Post Processing** | ❌ None | ✅ Any Python logic | ✅ Any Python logic |
| **Control Flow** | ❌ Single call | ✅ Loops, conditionals, retries | ✅ Loops, conditionals, retries |
| **Error Isolation** | ❌ Failure terminates | ✅ Self-implemented | ✅ try-except per-item isolation |
| **Progress Persistence** | ❌ None | ✅ Self-implemented | ✅ Write back state file per iteration |
| **Use Cases** | Simple "call Agent once, return result" | Non-Agent tool functions (read files, call APIs, etc.) | Batch processing, Pipeline orchestration, checkpoint resume |
| **Config Complexity** | Low (only `path`) | Medium (write function + YAML registration) | High (write wrapper + Worker YAML + YAML registration) |
| **Reference** | [Section 5](#5-worker-export-as-callable-tool) | [4.1 Dynamic Tools](#41-two-tool-types) | This section (4.4) |

> **Difference between Path B and Path C**: Both use the same YAML registration method (`tools` field with `module + function`). The difference is that **Path C's Python function internally loads and calls an Agent via `YamlAgentFactory.create_agent_as_tool()`**, while Path B is a plain tool function without Agent involvement.

#### 4.4.3 Core API: `YamlAgentFactory.create_agent_as_tool()`

```python
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

tools = YamlAgentFactory.create_agent_as_tool(
    config_path,        # str | Path | dict — Worker YAML path (relative to AGENT_ROOT) or config dict
    agent_class=None,   # Optional, custom Agent class
    model=None,         # Optional, model instance
    execution_env=None, # Optional, execution environment instance
    logger=None,        # Optional, AgentLogger instance
)
# Returns: List[Callable] — Contains one callable function, signature defined by Worker's agent_function_schema
```

**Return value notes**:
- The function in the returned list is **called like a regular Python function**, with parameter names and types defined by `agent_function_schema.inputs`
- Return value is always a **string** (`None` → `""`, other values → `str(result)`)
- Worker YAML **must** contain a valid `agent_function_schema`; otherwise returns an empty list

#### 4.4.4 Design Principles (Four Best Practices)

| Principle | Description | Example |
|------|------|------|
| **① Lazy singleton** | Agent tool is only initialized on first call, reused afterwards | Global variable `_tool = None` + getter function |
| **② Separate pre/post** | Deterministic operations (read/write files, format validation) at Python layer, don't waste LLM tokens | Read index.md → Agent analysis → Write analysis.md |
| **③ Error isolation** | Each subtask wrapped in try-except; single item failure records error then continues | `entry["error_msg"] = str(e)` |
| **④ Immediate persistence** | Write back progress file immediately after each iteration; resume from checkpoint after crash | `_save_progress()` called at end of each loop |

#### 4.4.5 Generic Template: Minimal Wrapping (Single Call + Pre/Post Processing)

When you only need some deterministic processing before and after the Agent call, use this minimal template:

```python
# applications/<app>/agent_tools/my_agent_tool.py

from __future__ import annotations
from pathlib import Path

from src.lib.logging import get_logger
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

_AGENT_YAML = "applications/<app>/workflows/worker_agents/<worker>.yaml"


def analyze_with_context(file_path: str) -> str:
    """
    Agent call tool with pre/post processing.

    Pre: Read file content, validate format
    Agent: LLM analysis
    Post: Write analysis results

    Args:
        file_path: Path to the file to analyze

    Returns:
        Analysis result summary
    """
    logger = get_logger(__name__)

    # create_agent_as_tool has built-in cache, same YAML only creates once
    tool = YamlAgentFactory.create_agent_as_tool(_AGENT_YAML)
    if tool is None:
        raise RuntimeError(f"Failed to create agent tool from {_AGENT_YAML}")

    # -- Pre-processing (deterministic, no LLM token cost) --
    source = Path(file_path)
    if not source.exists():
        return f"Error: file not found: {file_path}"
    content = source.read_text(encoding="utf-8")
    if not content.strip():
        return f"Error: file is empty: {file_path}"

    # -- Call Agent (LLM reasoning) --
    logger.info(f"Analyzing {file_path}")
    result = tool(content=content)

    # -- Post-processing (deterministic) --
    output_path = source.with_suffix(".analysis.md")
    output_path.write_text(str(result), encoding="utf-8")

    return f"Analysis saved to {output_path}"
```

#### 4.4.6 Generic Template: Batch Processing + Checkpoint Resume

When you need to loop-call an Agent to process multiple subtasks, use this complete template:

```python
# applications/<app>/agent_tools/batch_agent_tool.py

from __future__ import annotations
import json
import traceback
from pathlib import Path

from src.lib.logging import get_logger
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

_AGENT_YAML = "applications/<app>/workflows/worker_agents/<worker>.yaml"

def _save_progress(path: Path, data: dict) -> None:
    """Persist immediately after each iteration (crash-safe)"""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run_batch_analysis(progress_file: str, retry_failed: bool = False) -> str:
    """
    Batch-call Agent to analyze multiple subtasks with checkpoint resume and error isolation.

    Progress file format (JSON):
    {
      "item_1": {"status": "pending", "input": "..."},
      "item_2": {"status": "completed", "output": "..."},
      "item_3": {"status": "failed", "error_msg": "..."},
    }

    Args:
        progress_file:  Progress file path (JSON format)
        retry_failed:   Whether to retry previously failed items

    Returns:
        Summary string with completed/failed/skipped counts
    """
    pf = Path(progress_file)
    if not pf.exists():
        raise FileNotFoundError(f"Progress file not found: {pf}")
    progress = json.loads(pf.read_text(encoding="utf-8"))

    # -- Checkpoint resume: reset crash-orphaned in_progress --
    for key, entry in progress.items():
        if entry["status"] == "in_progress":
            entry["status"] = "pending"
    # -- Optional: retry failed items --
    if retry_failed:
        for key, entry in progress.items():
            if entry["status"] == "failed":
                entry["status"] = "pending"
                entry.pop("error_msg", None)
    _save_progress(pf, progress)

    logger = get_logger(__name__)

    # create_agent_as_tool has built-in cache, same YAML only creates once
    tool = YamlAgentFactory.create_agent_as_tool(_AGENT_YAML)
    if tool is None:
        raise RuntimeError(f"Failed to create agent tool from {_AGENT_YAML}")

    stats = {"completed": 0, "failed": 0, "skipped": 0}

    for key, entry in progress.items():
        if entry["status"] in ("completed", "failed"):
            stats["skipped"] += 1
            continue

        # -- Pre-processing --
        entry["status"] = "in_progress"
        _save_progress(pf, progress)          # Mark in-progress (crash-safe)

        try:
            # -- Call Agent --
            result = tool(query=entry["input"])

            # -- Post-processing --
            entry["status"] = "completed"
            entry["output"] = str(result)
            entry.pop("error_msg", None)
            stats["completed"] += 1

        except Exception as e:
            # -- Error isolation: record and continue --
            entry["status"] = "failed"
            entry["error_msg"] = str(e)
            entry["error_trace"] = traceback.format_exc()
            stats["failed"] += 1

        _save_progress(pf, progress)          # Write back after each iteration

    return (
        f"Batch complete: {stats['completed']} completed, "
        f"{stats['failed']} failed, {stats['skipped']} skipped."
    )
```

#### 4.4.7 Supervisor YAML Registration

Register the wrapped Python function in the Supervisor's `tools` field:

```yaml
# Supervisor YAML
name: "my_supervisor"
description: "Orchestrate multi-step analysis process"
workflow: |
  1. Call run_batch_analysis to batch analyze all subtasks
  2. Based on returned summary, determine if retry is needed

tools:
  # Python tool function wrapping an Agent
  - name: "run_batch_analysis"
    module: "applications.<app>.agent_tools.batch_agent_tool"
    function: "run_batch_analysis"

  # Can also register other regular tools simultaneously
  - name: "read_file"
  - name: "list_directory"
```

> **Tool description auto-extraction**: The framework automatically extracts tool descriptions from the Python function's `__doc__` (Docstring). Ensure good documentation in your functions; the `description` field in YAML is ignored.

#### 4.4.8 Complete Practice: repo_map Architecture Analysis Pipeline

Below is an actual implementation from the `applications/repo_map` project, demonstrating the complete "Agent wrapped as Python tool function" pattern:

**Architecture Overview**:

```
Supervisor (repo_map_agent)
  │
  ├── tools (Python-wrapped Agent):
  │   ├── run_analysis_loop()     ← Python for loop + Agent call
  │   └── get_analysis_summary()  ← Pure Python, reads progress file
  │
  └── worker_agents:
      └── dir_architecture_analysis.yaml  ← Called internally by run_analysis_loop()
```

**Key Design Decisions**:

1. **`dir_architecture_analysis`** is a standard Worker Agent (with `agent_function_schema`), but is **NOT auto-registered to Supervisor via `worker_agents`**
2. Instead, **`run_analysis_loop()` manually loads and loop-calls it at the Python layer**, passing different directory `index.md` content each time
3. Python layer handles: read index.md (pre) → call Agent (LLM analysis) → write analysis.md (post) → update progress (persistence)
4. Single directory analysis failure doesn't affect other directories; failure info is recorded in `progress.json` for later inspection or retry

**File Organization**:

```
applications/repo_map/
├── workflows/
│   ├── repo_map_agent.yaml                        # Supervisor
│   └── worker_agents/
│       └── dir_architecture_analysis.yaml         # Worker (called by Python wrapper)
├── agent_tools/
│   └── pipeline_agent_tools.py                    # Python wrapper functions
└── repo_map_app.py                                # Application entry point
```

> 💡 **Core Philosophy**: Although `dir_architecture_analysis` is declared in `worker_agents`, the Supervisor actually uses `run_analysis_loop()` from `tools`, which internally loads and loop-calls that Worker Agent via `YamlAgentFactory.create_agent_as_tool()`. This achieves the optimal combination of **Python control flow reliability** and **LLM Agent intelligent reasoning**.

---

## 5. Worker Export as Callable Tool

> 💡 If your Worker Agent needs **pre/post processing** (file read/write, loops, error isolation, etc.), see [4.4 Advanced Pattern: Wrapping Agent as Python Tool Function](#44-advanced-pattern-wrapping-agent-as-a-python-tool-function). This section covers the **simplest approach** — Worker auto-exports as a tool via `agent_function_schema`, no extra Python code needed.

### 5.1 Core Mechanism

When a Worker YAML contains a valid `agent_function_schema`, the framework automatically exports that Worker as a callable tool. The Supervisor calls it directly by function name (i.e., the Worker's `name`). This is the simplest Agent-Tool path, suitable for scenarios without extra pre/post processing.

```
Supervisor → calls project_scan(query="Check CAN module") → Worker executes → Returns string result
```

### 5.2 `agent_function_schema` Complete Structure

> Parameter names are customizable, as long as they are valid Python identifiers (e.g., `query`, `file_path`, `module_name`).

```yaml
agent_function_schema:
  description: |                     # ✅ Required: tool description
    Preparation phase analysis agent...
  inputs:                            # ✅ Required: parameter definition dictionary (at least 1 parameter)
    param1:                          # Custom parameter name, valid Python identifier
      description: |                 # ✅ Required: parameter description
        Description of the first parameter
      required: true                 # ❌ Optional: whether required (default true)
    param2:                          # Optional parameter
      description: "Description of the second parameter"
      required: false
  output:                            # ✅ Required: output definition
    description: |                   # ✅ Required: output description
      Returns analysis summary text
```

### 5.3 Validation Rules

| Validation Item | Rule | Error Example |
|--------|------|----------|
| `description` | Non-empty string | `agent_function_schema.description must be a non-empty string` |
| `inputs` | Non-empty dictionary | `agent_function_schema.inputs must be a non-empty dictionary` |
| `inputs.<name>` key | Valid Python identifier (`isidentifier()`) | `inputs key 'xxx' must be a valid identifier` |
| `inputs.<name>.description` | Non-empty string | `inputs.xxx.description must be a non-empty string` |
| `inputs.<name>.required` | Boolean (can be omitted, defaults to true) | `inputs.xxx.required must be a boolean` |
| `inputs.<name>.type` | Can be omitted in YAML, normalized to `"string"` at runtime | — |
| `output` | Must be a dictionary | `output must be a dictionary` |
| `output.description` | Non-empty string | `output.description must be a non-empty string` |

> ⚠️ **Parameter Type Constraints (Important)**: When defining `inputs` parameters, **do NOT use ambiguous type annotations such as `Optional[...]` or `Union[...]`**.
>
> | Constraint | Explanation |
> |-----------|-------------|
> | **No `Optional[...]`** | The framework will raise an exception for such ambiguous types |
> | **No `Union[...]`** | Same reason — ambiguous parameter types confuse the Agent about what to pass, degrading AI decision quality |
> | **Express optionality correctly** | Use the `required: false` field to indicate a parameter is optional |
> | **Type normalization** | All parameters are normalized to `"string"` type at runtime |
>
> ```yaml
> # ✅ Correct: use `required` field for optionality
> inputs:
>   target_path:
>     description: "Analysis target path"
>     required: true
>   mode:
>     description: "Execution mode, defaults to standard"
>     required: false           # Use required: false, NOT Optional
>
> # ❌ Wrong: do NOT use ambiguous types
> # type: "Optional[str]"     → Framework will raise an exception
> # type: "Union[str, int]"   → Framework will raise an exception
> ```

### 5.4 Return Value Behavior

- Return value is **always a string**: `None` → `""`, other values → `str(result)`

---

## 6. worker_agents Path Resolution Rules

### 6.1 Directory Structure Constraints (Mandatory)

Agent YAML files **must** follow the directory structure below. The framework validates that the directories exist:

```
applications/{app_name}/
└── workflows/                          ← Supervisor YAML must be placed here
    ├── {app_name}_agent.yaml
    └── worker_agents/                  ← Worker YAML must be placed here
        ├── worker_a.yaml
        ├── worker_b.yaml
        └── analysis/                   ← Subdirectories allowed (no naming restrictions)
            └── deep_scan.yaml
```

- **Supervisor YAML** must be located under `applications/{app_name}/workflows/`
- **Worker YAML** must be located under `applications/{app_name}/workflows/worker_agents/`
- **Subdirectories are allowed** under `worker_agents/` with no naming restrictions, but Workers in subdirectories **cannot use shorthand filenames** — they must be referenced using full relative paths
- The framework automatically infers `{app_name}` (category) from the Supervisor YAML path, then locates the corresponding `worker_agents/` directory
- If `workflows/` or `worker_agents/` directory does not exist, loading will fail with an error

### 6.2 Three Path Forms

| Form | Detection Condition | Resolution | Example |
|------|----------|----------|------|
| **Absolute path** | Starts with `/` | Used directly | `/home/user/project/worker.yaml` |
| **Relative path** | Contains `/` or `\` | Concatenated with `AGENT_ROOT` | `applications/my_app/workflows/worker_agents/step0.yaml` |
| **Shorthand filename** | No directory separators, **must include extension** | Searched under `worker_agents/` | `project_scan.yaml` |

**Supported file extensions**: `.yaml`, `.yml`, `.md`

> **Note**: Shorthand filenames **must include a file extension** (e.g. `.yaml`). Names without extensions (e.g. `project_scan`) will raise an error.

### 6.3 Recommended Usage

```yaml
# ✅ Recommended: shorthand filename (when Worker is under the same app's worker_agents/, most concise)
worker_agents:
  - path: "project_scan.yaml"

# ✅ Full relative path (use when referencing Workers from another app)
worker_agents:
  - path: "applications/other_app/workflows/worker_agents/shared_worker.yaml"

# ✅ Full relative path (use when referencing Workers in a subdirectory of worker_agents/)
worker_agents:
  - path: "applications/my_app/workflows/worker_agents/analysis/deep_scan.yaml"

# ❌ Prohibited: missing file extension
worker_agents:
  - path: "project_scan"               # Error! Must end with .yaml/.yml/.md

# ❌ Prohibited: using name field
worker_agents:
  - name: "project_scan"               # Error! Only path is allowed
```

### 6.4 Pre-check Mechanism

The system performs a full pre-check on **all** entries before loading (directory existence, file existence, extension check, etc.). **If any single entry fails, all Workers are not loaded** (all-or-nothing strategy).

---

## 7. Common Errors and Troubleshooting

### 7.1 Missing Required Fields

| Error Message | Fix |
|----------|------|
| `Configuration is missing required field: name` | Add `name: "xxx"` |
| `Configuration is missing required field: description` | Add `description: "xxx"` |
| `workflow field must be a non-empty string or non-empty list of non-empty strings` | Use `workflow: \|` for one workflow, or a non-empty `workflow:` list whose items are non-empty strings |

### 7.2 Tool Configuration Errors

| Error Message | Fix |
|----------|------|
| `Tool configuration must be a dictionary` | Change to `- name: "xxx"` format |
| `Tool configuration is missing required 'name' field` | Add `name` field |
| `must include both 'module' and 'function' fields` | `module`/`function` must both be provided |

### 7.3 Worker Agents Errors

| Error Message | Fix |
|----------|------|
| `worker_agents must be a list` | Change to list format |
| `uses unsupported field 'name'; use 'path' only` | Change to `path` |
| `does not exist` | Check file path spelling |
| `has unsupported extension` | Use `.yaml`/`.yml`/`.md` |

### 7.4 Other Errors

| Error Message | Fix |
|----------|------|
| `tool_call_type must be 'tool_call' or 'code_act'` | Only these two values are allowed |
| `execution_env.type='host' is no longer supported` | Change to `"local"` |
| `skills must be a list, dict, or string path` | Use list/dict/string |

---

## 8. Complete Practical Examples

### 8.1 Repo Map Architecture Analysis Project (Supervisor + 1 Worker)

**Supervisor**: `applications/repo_map/workflows/repo_map_agent.yaml`

```yaml
name: "repo_map_agent"
description: |
  Repo Map architecture analysis Supervisor.
  Scanning and Markdown generation are handled directly by repo_map_app.py (pure Python, zero LLM).
  This Agent only calls run_analysis_loop for LLM architecture analysis per directory,
  then calls get_analysis_summary for the summary report.

model_type: "powerful"
tool_call_type: "code_act"

workflow: |
  # Repo Map Architecture Analysis Workflow

  ## Execution Principles
  - Read output_dir from environment variable `REPO_MAP_OUTPUT_DIR`
  - Call `run_analysis_loop` first, then call `get_analysis_summary` for summary
  - If run_analysis_loop throws an exception, stop immediately and report error

tools:
  - name: "run_analysis_loop"
    module: "applications.repo_map.agent_tools.pipeline_agent_tools"
    function: "run_analysis_loop"
  - name: "get_analysis_summary"
    module: "applications.repo_map.agent_tools.pipeline_agent_tools"
    function: "get_analysis_summary"
  - name: "read_file"
  - name: "list_directory"

worker_agents:
  - path: "applications/repo_map/workflows/worker_agents/dir_architecture_analysis.yaml"

execution_env:
  type: "local"
```

**Worker Example**: `dir_architecture_analysis.yaml`

```yaml
name: "dir_architecture_analysis"
description: |
  Perform LLM architecture analysis on a single directory.
  Receives dir_path and index_content, returns Markdown-formatted architecture analysis text.

model_type: "powerful"
tool_call_type: "code_act"

workflow: |
  # Single Directory Architecture Analysis
  Based on the provided index_content, analyze code structure and return Markdown architecture analysis text.
  ## Analysis Dimensions
  1. Core functionality  2. Key modules  3. Design patterns  4. Dependencies  5. Notes

tools: []

execution_env:
  type: "local"

agent_function_schema:
  description: |
    Perform LLM architecture analysis on a single directory, returning Markdown analysis text.
  inputs:
    dir_path:
      description: "Relative directory path to analyze, e.g. src/utils"
      required: true
    index_content:
      description: "Complete text content of the directory's index.md"
      required: true
  output:
    description: "Markdown-formatted architecture analysis text"
```

### 8.2 Minimal Configuration

```yaml
name: "simple_reader"
description: "A simple Agent that reads and analyzes specified file content"
workflow: |
  1. Read the user-specified file
  2. Analyze the file content
  3. Output the analysis result
tools:
  - name: "read_file"
```

### 8.3 Markdown (.md) Format for Writing Workers

Write configuration in a YAML code block at the beginning of the file; the remaining content automatically becomes the `workflow`:

````markdown
```yaml
name: "project_scan"
description: "Project structure scanning agent"
model_type: "powerful"
tools:
  - name: "read_file"
agent_function_schema:
  description: "Preparation phase analysis tool"
  inputs:
    param1:                          # Custom parameter name
      description: "Task description"
      required: true
  output:
    description: "Analysis summary"
```

# The following content automatically becomes the workflow

## Step 1: Scan Files
...
````

---

## 9. Configuration Override Relationships

> For complete override hierarchy, see [Configuration System Overview](config-overview.md). This section focuses on how Agent YAML overrides system configuration.

### 9.1 Override Mechanism

```
Global system configuration (config/system.yaml)
       ↓ deep merge
Application-level override (applications/<app>/config/system.yaml)
       ↓ deep merge (whitelisted fields only)
Agent YAML whitelisted fields
       ↓
Effective config
```

### 9.2 Overridable Field Whitelist

The following top-level fields in Agent YAML can override system configuration (source: `_WORKFLOW_OVERLAY_KEYS`):

| Field | Type Constraint | Description |
|------|----------|------|
| `system` | `dict` | System metadata (name, version, user_agent) |
| `model_request_headers` | `dict` | Model request header profiles |
| `smart_summary` | `any` | Context compression strategy |
| `context_engine` | `dict` | Reversible context compression limits |
| `tool_access_control` | `dict` | Working directory and path filtering |
| `execution_env` | `dict` | Execution environment type and Shell path |
| `code_agent` | `dict` | CodeAgent code execution permissions |
| `tools` | `list` | Agent tool list and its effective-config overlay |
| `shell_settings` | `any` | Shell safety settings |
| `tools_mapping` | `any` | Tool mapping override |
| `default_toolsets` / `toolsets` | `any` | Toolset defaults or replacement |
| `prompt` | `str`/`dict` | Custom System Prompt template path |
| `mcp_servers` | `str`/`list`/`dict` | MCP server configuration |
| `self_learning` | `dict` | History and optional memory-review policy |
| `todo` | `dict` | Todo mode (`auto`, `on`, or `off`) |

> ⚠️ **Important**: The whitelist above is evaluated **per Agent YAML**, not per call chain. When a Supervisor invokes a Worker, the Worker's `tool_access_control`, `execution_env`, `prompt`, and other whitelisted overrides are rebuilt from the Worker YAML instead of being inherited from the Supervisor.
>
> ```yaml
> # If both Supervisor and Worker access the same external directory,
> # both YAML files must declare the allowlist.
> tool_access_control:
>   path_validation:
>     - tools: ["read_file", "grep_search", "glob_search", "shell_tool"]
>       include_paths:
>         - "/absolute/path/outside/workspace"
> ```

### 9.3 Non-Overridable Fields

The following fields are processed independently as Agent properties and are not merged into system configuration:

| Field | Processing |
|------|----------|
| `name` / `description` / `workflow` | Agent's own properties |
| `tools` (`list[dict]`) | Agent tool list, different from system `tools` (dict) |
| `worker_agents` / `agent_function_schema` | Role-specific properties |
| `skills` | Independent three-layer stacking loading (see [3.8](#38-skills--skill-package-configuration)) |
| `model_type` / `tool_call_type` | Agent selection parameters |
| `max_steps` / `planning_interval` | Agent execution parameters |

### 9.4 LLM Configuration Isolation

The following keys in Agent YAML are **automatically filtered** (`_LLM_ONLY_TOP_LEVEL_KEYS`):

| Filtered Key | Only Valid Location |
|-------------|-------------|
| `model` | `config/llm.yaml` |
| `llm` | `config/llm.yaml` |
| `langfuse` | `config/llm.yaml` |

```
WARNING: Ignoring top-level key 'model' in agent config;
         LLM settings must come from config/llm.yaml only.
```

### 9.5 Deep Merge Rules

| Data Type | Merge Behavior |
|----------|----------|
| **Dictionary** | Recursive merge (deep merge per key) |
| **List** | **Complete replacement** (higher priority completely replaces lower priority) |
| **Scalar** | Complete replacement |

> ⚠️ Lists are completely replaced, not appended! Agent YAML `toolsets:` replaces global `default_toolsets`; `toolsets: []` disables built-in tools.

### 9.6 Override Examples

```yaml
# Agent-level execution environment switch
execution_env:
  type: "docker"
  executor_kwargs:
    host: "127.0.0.1"
    port: 8888

# Agent-level disable smart summary
smart_summary: false

# Agent-level execution environment
execution_env:
  type: "local"
```

### 9.7 Per-Agent Shell Security Configuration Override

Agent YAML uses **separate top-level keys** for shell security overrides:

- `tools:` — Tool list (list format), declares which tools the agent uses
- `shell_settings:` — Shell security override (dict format), overrides `shell_settings` from `config/system.yaml`

These are independent keys — no nesting required.

#### Scenario 1: Read-Only Audit Agent — Only allow view commands

```yaml
name: "readonly_auditor"
description: "Read-only code audit agent"
model_type: "powerful"
tool_call_type: "code_act"

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

workflow: |
  You are a read-only code audit agent. You can only view file contents, not modify them.
```

#### Scenario 2: Developer Agent — Relax $() and ${} but keep safety baseline

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
  background_tasks:
    stall_threshold_seconds: 30     # Faster stall detection

workflow: |
  You are a development agent. You can write code, run builds and tests.
```

#### Scenario 3: Minimal Permission Agent — No Shell

```yaml
name: "text_analyzer"
description: "Pure text analysis agent, no shell needed"
model_type: "fast"
tool_call_type: "code_act"

# No shell_tool declared — agent cannot execute any shell commands
# No shell_settings needed
tools:
  - name: "read_file"
  - name: "grep_search"

workflow: |
  You are a text analysis agent. You can only read and search files.
```

#### Security Check Sub-Toggles

The `security_checks` dictionary supports 10 independent toggles. Undeclared keys default to `true` (enabled):

| Sub-Key | Blocks | Recommendation |
|---------|--------|----------------|
| `command_substitution` | `$()` and backticks | Can disable for build scripts |
| `parameter_expansion` | `${}` parameter expansion | Can disable for build scripts |
| `process_substitution` | `<()` / `>()` process substitution | Generally keep enabled |
| `env_injection` | `LD_PRELOAD`, `PATH` injection | ❗ Always keep enabled |
| `control_characters` | Hidden control characters | ❗ Always keep enabled |
| `dangerous_shell_prefix` | `sudo`, `bash -c`, `env` etc. | ❗ Always keep enabled |
| `zsh_dangerous_commands` | `zmodload`, `ztcp` etc. | Generally keep enabled |
| `incomplete_commands` | Incomplete command fragments | Can disable for build scripts |
| `ifs_injection` | IFS variable manipulation | ❗ Always keep enabled |
| `destructive_patterns` | `rm -rf /`, `mkfs` etc. | ❗ Always keep enabled |

> See [system_config.md §8 Shell Security Configuration](system_config.md#8-shell--shell-tool-security-configuration) for full details.

#### Shell Security Audit Log

When an agent executes shell commands, security-related events (blocks, path violations, stall detection, timeouts, etc.) are automatically written to a dedicated audit log file:

**File location**: `.agentloom/runs/<application_id>/<run_id>/audit/shell.jsonl`

The same attempt's manifest and main log are `manifest.json` and `logs/runtime.log`. The audit file rotates at 10 MiB with two backups and remains available even when this run uses `--no-file-log`.

Configuration (in `config/system.yaml` or agent YAML `shell_settings`):

```yaml
shell_settings:
  audit_log:
    enabled: true         # Master switch (default: true)
    log_success: false    # Also log successful executions (default: false)
```

Each line is one JSON object with a timestamp, event type, agent name, command, details, and an **actionable suggestion** that tells you exactly which YAML setting to change:

```json
{"timestamp":"2026-04-08T13:41:46+00:00","event_type":"SECURITY_BLOCK","agent":"code_reviewer","command":"$(cat /etc/passwd)","check_id":"command_substitution","message":"Blocked: $() command substitution detected","suggestion":"Review shell_settings.security_checks.command_substitution"}
```

When troubleshooting shell permission issues, checking the audit log is much more efficient than searching through the main application log:

```bash
# Find all run manifests and audit logs
find .agentloom/runs -name manifest.json -o -name shell.jsonl

# Read the latest attempt's identity and audit
manifest=$(find .agentloom/runs -name manifest.json -type f -print | sort | tail -1)
run_dir=$(dirname "$manifest")
sed -n '1,160p' "$manifest"
tail -n 100 "$run_dir/audit/shell.jsonl"

# Search by event type
rg 'SECURITY_BLOCK|WHITELIST_REJECT|PATH_VIOLATION' "$run_dir/audit/shell.jsonl"
```

---

## 10. Error Recovery Mechanism

When LLM tool calls fail (format parsing errors, unknown tool names, argument errors, etc.), the system automatically performs progressive error recovery instead of terminating the task.

### 10.1 Progressive Recovery (4 Levels)

| Consecutive Failures | Level | Behavior |
|---|---|---|
| 1 | Level 1 | Standard format guidance: correct JSON example + available tools list |
| 2 | Level 2 | Enhanced diagnosis: error type + output problem + correct format + tool params |
| 3-4 | Level 3 | Approach switch suggestion: try different tool or simplify (intentionally shorter) |
| 5+ | Level 4 | Minimal format template, loops indefinitely (no task termination, `max_steps` is the safety boundary) |

### 10.2 Error Classification (4 Categories)

| Category | Trigger | Feedback Focus |
|----------|---------|----------------|
| `FORMAT_NOT_FOUND` | No recognizable tool call structure | Full format template + tool list |
| `JSON_SYNTAX_ERROR` | JSON-like structure but syntax errors | Specific syntax issue |
| `UNKNOWN_TOOL` | Tool name not in registry | List all available tools |
| `ARGUMENT_ERROR` | Tool name correct but wrong arguments | Tool parameter schema |

### 10.3 Error Message Consolidation

For consecutive errors, the system consolidates historical error messages:
- Only keeps the latest error message in full (with Level 1-4 guidance)
- Older errors compressed to one-line summaries
- Consolidation runs before the compression pipeline

### 10.4 Adaptive Strategy Memory

For Fallback text parsing path (models that don't support native tool calling):
- Records per-model last successful parsing strategy
- Subsequent requests try cached strategy first, skipping ineffective attempts

### 10.5 Compression Pipeline Exemption

Recent error recovery messages are protected from compression:
- Layer 3 and Fallback skip the latest 1 error message during compression
- Ensures the LLM always sees the latest error feedback and format guidance

### 10.6 LLM Output Tolerance Enhancements

The framework provides automatic tolerance for common non-standard LLM outputs:

| Issue | Symptom | Auto-fix |
|-------|---------|----------|
| Whitespace in file paths | `' /tmp/foo.txt'` (leading/trailing spaces) | All file tools auto-`strip()` |
| Stringified parameters | `sections: "[{...}]"` (JSON string instead of array) | Auto `json.loads()` coercion to native type |
| Python dict tool calls | `[{'id':..., 'function':{'name':..., 'arguments':{'query':'...\n...'}}}]` | Nested tool call strategy supports JSON escapes + Python booleans |

These tolerance mechanisms significantly reduce wasted retries caused by LLM output format variations, without compromising security.

---

## Appendix: Field Quick Reference Table

| Field | Required | Supervisor | Worker | Type | Default |
|------|------|-----------|--------|------|--------|
| `name` | ✅ | ✅ | ✅ | `str` | — |
| `description` | ✅ | ✅ | ✅ | `str` | — |
| `workflow` | ✅ | ✅ | ✅ | `str`/`list[str]` | — |
| `goal` | ❌ | ✅ | ❌ | `bool`/`dict` | `false` |
| `tools` | ❌ | ✅ | ✅ | `list[dict]` | `[]` |
| `model_type` | ❌ | ✅ | ✅ | `str` | `model.default_model_type` from `config/llm.yaml`; no implicit default |
| `tool_call_type` | ❌ | ✅ | ✅ | `str` | `"code_act"` |
| `execution_env` | ❌ | ✅ | ✅ | `dict` | `{type: "local"}` |
| `prompt` | ❌ | ✅ | ✅ | `str`/`dict` | Framework built-in |
| `planning_interval` | ❌ | ✅ | ✅ | `int` | Not set |
| `todo` | ❌ | ✅ | ✅ | `dict` | `{mode: "auto"}` |
| `concurrency` | ❌ | ✅ | ✅ | `int`/`str` | Not set (`auto`) |
| `skills` | ❌ | ✅ | ✅ | `list`/`dict`/`str` | Auto-loaded |
| `worker_agents` | ❌ | ✅ | ❌ | `list[dict]` | `[]` |
| `max_steps` | ❌ | ✅ | ✅ | `int` | `80` |
| `agent_function_schema` | ❌ | ❌ | ✅ | `dict` | Not set |
