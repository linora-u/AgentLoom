---
name: workflow-review
description: "Use when reviewing a user-provided AgentLoom-style application path for workflow architecture quality, especially agent/tool boundaries, orchestration contracts, and resilience risks that require evidence-based recommendations."
---

# Workflow Architecture Review

Used to review the workflow design quality of AgentLoom-style Applications.
This Skill preserves AgentLoom framework semantics but does not depend on specific examples, fixed directories, or fixed tool lists in the current repository.

> Companion documents (relative to Skill root directory):
> - [references/review-checklist.md](./references/review-checklist.md)
> - [references/best-practices.md](./references/best-practices.md)
> - [references/system-tools.md](./references/system-tools.md)
> - [scripts/scan_tools.py](./scripts/scan_tools.py)

## Applicable Scenarios

- User provides an `application path` and needs to determine whether the workflow architecture is sound
- Need to review Supervisor/Worker coordination, Agent/Tool boundaries, and resilience design
- Need to provide actionable improvement recommendations rather than generic advice

## Non-Applicable Scenarios

- Creating a new Application (use `create-app` instead)
- Non-Agent architecture reviews (pure algorithm/pure style optimization)
- "Subjective scoring" without verifiable evidence

---

## Phase 1: Input Confirmation

Must confirm first:

1. **Application path (required)**
2. **Root directory prerequisite (required)**: Navigate to the AgentLoom root directory before running detection/updates
3. Review scope (defaults to all four dimensions)
4. Business context (optional, affects priority)

Root directory identification criteria: The `config/llm.yaml` file exists.
  - ⚠️ Do not use `config/system.yaml` for identification, as application-level directories may also contain this file (e.g., `applications/ai_quality_analysis/config/system.yaml`), which cannot uniquely identify the project root.
  - `config/llm.yaml` is globally unique and only exists in the AgentLoom root directory.

If the root directory prerequisite is not met, return missing items directly without proceeding to subsequent phases.

In autonomous execution mode: extract the path from context; if extraction fails, return missing items directly without proceeding to subsequent phases.

---

## Phase 2: Context Scanning (Capability Discovery First)

Perform "structure scanning + capability discovery" before analysis and judgment.

### 2.1 Structure Scanning

Prioritize calling `scripts/scan_tools.py`:

```bash
# Navigate to AgentLoom root directory first (config/llm.yaml must exist)
cd /path/to/AgentLoom
pwd
ls config/llm.yaml

.venv/bin/python -c "
import sys
sys.path.insert(0, '/abs/path/to/workflow-review')
from scripts.scan_tools import scan_app_structure
print(scan_app_structure('applications/<app_name>'))
"
```

Then extract the `workflow` raw text from individual YAMLs as needed:

```bash
.venv/bin/python -c "
import sys
sys.path.insert(0, '/abs/path/to/workflow-review')
from scripts.scan_tools import extract_workflow_text
print(extract_workflow_text('.../worker_agents/<step>.yaml'))
"
```

### 2.2 Capability Discovery (Dynamic, Not Hardcoded)

Do not assume a fixed system tool list; discover based on the target project's actual configuration:

1. Read the project-level/application-level `config/system.yaml`, compute the effective configuration following the **override chain** (avoid drawing conclusions directly from sibling files)
2. Extract the effective `default_loaded_tools` (including source hierarchy), considering list replacement semantics
3. Extract `tools_mapping`; if missing, check the legacy `tools.mapping` compatibility mapping
4. Read `tools`, `worker_agents`, `execution_env`, and `model_type` from Agent configurations
5. Determine default tool availability based on `execution_env` (`docker`/`e2b` environments skip default tools entirely)
6. Read `agent_tools/*.py`, extract public functions and docstring capability summaries
7. Compare action verbs in `workflow` instructions to identify capability gaps

---

## Phase 3: Four-Dimension Review

Every finding must be actionable, with a fixed format:

- `[Severity]`: `Must Fix (blocks deployment)` / `Recommended Optimization (non-blocking)`
- `[Evidence]`: Reference specific fields, prompt text, or code snippets
- `[Assessment]`: Explain risks and impact
- `[Improvement Recommendation]`: Configuration change / architecture change / new capability suggestion
- `[Confidence]`: `High` / `Medium` / `Low`
- `[Inferred]`: `Yes` / `No`

### Dimension 1: Workflow Process Design

Focus on: phase responsibilities, explicit dependencies, branch clarity, output constraints, observability entry points.

Additional focus:

- **Workflow five-section structure**: Whether the workflow follows the recommended structure (① Background & Role → ② Core Responsibilities & Constraints → ③ Execution Flow with Mermaid → ④ Step-by-step Details → ⑤ Output Requirements). Missing sections reduce LLM execution quality.
- **`description` role boundary**: The `description` should be short and concise, clarifying the Agent's core role, and should not duplicate the lengthy `workflow`.
- **Single-agent over-engineering**: Do not forcefully introduce a Supervisor for a single Worker, avoiding single-agent over-engineering.
- **Mermaid flowchart special semantics**: The framework automatically detects ` ```mermaid ` code blocks in workflows, extracts them, and wraps them in `<workflow>` XML tags. When Mermaid blocks are present, the framework **automatically injects "must be followed strictly"** constraints. Verify that the Mermaid flowchart is consistent with textual instructions and leverages this enforcement mechanism.
- **`planning_interval` configuration**: For Agents with long task chains (many steps), check whether `planning_interval` is set to enable periodic self-reflection. Without it, Agents may lose track of progress in complex workflows.
- **`max_steps` budget**: Check whether `max_steps` is set appropriately for each Agent (default: 80). Overly high values waste resources; overly low values cause premature termination.
- **`concurrency` for batch Workers**: If a Worker Agent is designed to be called on multiple inputs (e.g., analyzing N directories, processing N files), check whether `concurrency` is configured. Batch-invocable Workers should set `concurrency: auto` (or a tuned integer) so the application layer can use `tool.batch(tasks)` for parallel execution.

### Dimension 2: Supervisor-Worker Agent Coordination

Focus on:

- `agent_function_schema` contract clarity
- Whether Worker dependencies are explicitly passed
- Coordination overhead and "translation loss" (information loss caused by Supervisor repeatedly relaying instructions)
- Delegation deduplication (avoid multiple Workers performing redundant analysis)

### Dimension 3: Agent/Tool Responsibility Separation (Core)

Focus on:

- Whether purely deterministic steps are handled by an LLM (should be converted to Tools)
- Whether repeated templates and constraints in Prompts can be extracted
- Whether Tool encapsulation patterns are appropriate — verify against the **three Agent-Tool paths**:
  - **Path A**: `worker_agents` auto-registration (simple single-call scenarios)
  - **Path B**: Plain dynamic tools via `module + function` (no Agent involved)
  - **Path C**: Python-wrapped Agent tools via `YamlAgentFactory.create_agent_as_tool()` (for batch processing, checkpoint/resume, error isolation, progress persistence)
- Whether `model_type` matches task complexity, **and whether the specified `model_type` actually exists in `llm.yaml`** (non-existent types cause `ValueError` at runtime — no silent fallback)
- Whether `tool_call_type` selection is appropriate: `code_act` is recommended for Supervisors and complex Workers (supports loops, conditions, multi-step orchestration); `tool_call` suffices for simple single-tool Workers
- Whether Agent YAML contains **prohibited LLM fields** (`model`, `llm`, `langfuse`) — these are auto-filtered with warnings; LLM parameters must only be in `config/llm.yaml`
- **`code_agent` security (Recommended Optimization)**: It is recommended to avoid using `"*"` for `additional_authorized_imports` or `additional_functions`; suggest specific whitelists instead to prevent code execution risks.
- **`execution_env` mismatch (Recommended Optimization)**: For complex tasks involving code generation, it is recommended to configure `execution_env` to use `docker` or `e2b` for secure sandbox isolation, rather than the default `local` environment.

**Skills Integration** (check when the application uses Skills):

- Whether Skills are correctly referenced (path resolution is relative to `AGENT_ROOT`, not the Agent YAML directory)                                           
- Whether `invocation-control` is appropriate: `force-inject` for critical capabilities (e.g., memory systems), `true` for on-demand loading, `false` for silent Hook-only Skills                                                               
- Whether the three-layer loading mechanism is understood: Global Skills (`config/system.yaml`) → Auto-discovered (`AGENT_ROOT/skills/`) → Agent-private (Agent YAML `skills` field). Same-name Skills in later layers override earlier ones with warnings.                                                                     
- Whether `platform` is set correctly when tool name mapping is needed
- **Hook validation**: Check if declared hooks have corresponding physical scripts existing (e.g., if `TaskCreated` is declared, `scripts/on_task_start.py` physical file must exist).
### Dimension 4: Error Handling and Resilience

Focus on:

- Gating, error isolation, retries, and limits
- Checkpoint/progress-based resumption
- Whether parallelism/step budgets are controllable (tokens and latency) — specifically check `max_steps` (default 80) and `planning_interval` settings per Agent
- Whether failure paths are recoverable and diagnosable
- Whether `smart_summary` (context compression) is enabled for long-context tasks that risk token overflow — this controls whether the framework uses LLM summarization vs simple truncation
- Whether `prompt` custom overrides (if present) conflict with `workflow` content or reference non-existent template files

---

## Phase 4: Output Report

Only output dimensions with findings; dimensions without findings are covered in a single line.

```markdown
# Workflow Architecture Review Report: <app_name>

## Review Summary
- Application: <app_name>
- Pattern: <Single Agent / Supervisor + N Workers>
- Worker Count: <N>
- Custom Tool Count: <M>
- Must Fix (blocks deployment): <count>
- Recommended Optimization (non-blocking): <count>

## Finding <number>: <title>
[Severity]
- Must Fix (blocks deployment) / Recommended Optimization (non-blocking)

[Evidence]
- ...

[Assessment]
- ...

[Improvement Recommendation]
- ...

[Confidence]
- High / Medium / Low

[Inferred]
- Yes / No
```

### Improvement Recommendation Layering Rules

- Configuration-level: Provide field-level modification suggestions directly
- Architecture-level: Provide step-by-step refactoring plans (complete code not required)
- New capability-level: Describe required capability boundaries and input/output; do not presume repository implementation

---

## AgentLoom Constraint Reference (General)

**Required fields & structure:**
- Agent YAML required fields: `name`, `description`, `workflow`
- Workers should have a fully defined `agent_function_schema`
- `worker_agents` uses `path` (not `name`), supporting three forms: absolute path / relative path / shorthand name (no separators)
- `worker_agents` supports suffixes: `.md` / `.yaml` / `.yml`
- `workflow` is either a non-empty string (use `|` YAML literal block syntax) or a non-empty `list[str]` executed sequentially; supports Markdown and Mermaid

**LLM configuration isolation:**
- Fields `model`, `llm`, `langfuse` in Agent YAML or `system.yaml` are **auto-filtered** with warning logs — all LLM parameters must only be in `config/llm.yaml`
- Agent selects model via `model_type`; if the specified type does not exist in `llm.yaml`, runtime raises `ValueError` (no silent fallback)
- Fallback chain when `model_type` is omitted: uses `default_model_type` from `llm.yaml` (defaults to `"common"`)

**Configuration overlay:**
- Agent YAML overlay whitelist (7 keys only): `system`, `smart_summary`, `tool_access_control`, `execution_env`, `code_agent`, `tools`, `prompt`
- `default_loaded_tools` is affected by list replacement semantics in the override chain; determination must be based on the effective configuration
- `default_loaded_tools` loading behavior varies across execution environments (`docker`/`e2b`/`wasm` skip default tools entirely)

**Skills system:**
- Three-layer loading: Global Skills (`config/system.yaml`) → Auto-discovered (`AGENT_ROOT/skills/`) → Agent-private (Agent YAML `skills`)
- Three invocation modes: `allow-model: "force-inject"` (injected into system prompt) / `true` (on-demand via `load_skill()`) / `false` (hidden, Hook-only)
- 9 Hook events: TaskCreated, TaskCompleted, StopFailure, SubagentStart, SubagentStop, PreToolUse, PostToolUse, PostToolUseFailure, Stop

**Key optional fields:**
- `tool_call_type`: `"code_act"` (default, recommended for Supervisors) or `"tool_call"` (for simple Workers)
- `planning_interval`: positive integer, enables forced planning every N steps (useful for long task chains)
- `max_steps`: integer, default 80, Agent terminates after exceeding this
- `smart_summary`: boolean, controls context compression strategy (LLM summarization vs truncation)

## Prompt Quality Baseline (Aligned with Industry Best Practices)

- Instructions should be specific: clearly define roles, objectives, constraints, input boundaries, and output formats
- Structure should be clear: use sections/labels to distinguish "rules, context, examples, tasks"
- Output should be verifiable: use fixed fields with clear boundaries between evidence and inference
- Evaluation should be closed-loop: compare before and after refactoring using the same small sample scenarios
