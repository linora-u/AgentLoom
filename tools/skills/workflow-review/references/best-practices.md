# Workflow Architecture Review — Best Practices Pattern Library (Decoupled from Repository)

This document provides transferable workflow review patterns, not tied to any specific repository examples.

---

## Pattern 1: Complexity-Driven Architecture Selection (Single First)

**Principle**: First verify whether a single Agent is sufficient before upgrading to multi-Agent.

### When to Prefer Single Agent

- Short task chain with few branches
- Clear tool set with no obvious conflicts
- No strong parallelism requirements

### When to Upgrade to Multi-Agent

- Too many conditional branches in the prompt, reducing single Agent stability
- Severe tool overlap causing frequent routing errors
- Tasks are naturally parallelizable with clear sub-task boundaries

### Review Signals

- Does adding Workers bring verifiable benefits?
- Is there "splitting for the sake of splitting"?
- Has an upgrade/rollback strategy been defined?

---

## Pattern 2: Manager-Worker Contractual Collaboration

**Principle**: Supervisor handles orchestration and boundary control; Workers handle specialized execution.

### Recommended Practices

- Workers must have callable contracts (inputs, outputs, purpose)
- Make data flow explicit: how does the output of one step enter the next?
- Supervisor should avoid rewriting Worker results, causing "translation loss"

### Typical Risks

- Workers sharing implicit files or implicit global state
- Worker contracts too vague (e.g., only `query`)
- Supervisor repeatedly paraphrasing, leading to semantic loss or redundant work

---

## Pattern 3: Push Deterministic Logic Down to Tools

**Principle**: LLMs handle understanding, reasoning, and generation; deterministic logic goes to Tools.

### Decision Tree

1. Does the operation require semantic understanding?
   - Yes: Keep in Agent
   - No: Prefer converting to Tool

2. Does it need pre/post-processing workflows (batch processing, validation, write-back)?
   - Yes: Use Agent-as-Tool pattern (outer Python control)
   - No: Can be directly injected as a regular Tool

### High-Frequency Tool-Convertible Signals

- File traversal, sorting, filtering, counting
- Reference lookup, rule matching, format rendering
- Fixed-flow loops (validate -> retry)

---

## Pattern 4: Task Decomposition Boundaries and Deduplication

**Principle**: Each Worker's goal, input, and output must be unique; avoid redundant delegation.

### Recommended Practices

- Use "task declaration statements" to define Worker boundaries (do one thing only)
- Explicitly prohibit overlapping analysis scopes
- Add deduplication checks: is the same input being consumed by multiple Workers?

### Review Checks

- Are two Workers producing the same type of conclusions?
- Are tool calls being duplicated due to unclear boundaries?
- Are there unnecessary "relay Workers"?

---

## Pattern 5: Evaluator-Optimizer Loop (Small Sample First)

**Principle**: Establish reproducible evaluation first, then perform architecture changes.

### Recommended Flow

1. Start with a small sample task set to establish a baseline (don't aim for comprehensive coverage)
2. Define pass criteria (functional correctness, tool call accuracy, output quality)
3. Include edge cases (long context, ambiguous input, multiple handoffs)
4. Compare against baseline after changes to confirm benefits

### Review Focus

- Are there explicit pass thresholds?
- Are critical boundary conditions covered?
- Is subjective "looks better" being avoided as the sole criterion?

---

## Pattern 6: Parallelization and Effort Budgeting

**Principle**: Parallelism is only for independent sub-tasks and must be budget-constrained.

### Parallelism Prerequisites

- Sub-task inputs are independent
- No shared mutable state
- Failures are isolatable and recoverable

### Budget Controls

- Set maximum concurrency
- Set maximum steps / maximum rounds / timeout
- Evaluate whether token and latency savings justify coordination costs

### Common Pitfalls

- Blind parallelism causing rate limiting or resource contention
- Complex result merging logic that actually reduces quality

---

## Pattern 7: Observability and Recovery (Trace/Checkpoint/Retry)

**Principle**: Long-chain workflows must be traceable, recoverable, and retryable.

### Minimum Requirements

- Key nodes have structured logs (input summary, output summary, errors)
- Batch processing has checkpoint/progress state
- Retries have limits and backoff strategies
- Failed items can be re-run individually without dragging down the entire process

### Review Focus

- Are error messages actionable?
- Can execution resume from the last progress point after interruption?
- Is there a distinction between retryable and non-retryable errors?

### AgentLoom Built-in Checkpoint Configuration

AgentLoom provides a **built-in checkpoint/resume/heartbeat system** requiring no application-level code. All configuration lives under `checkpoint.*` in `config/system.yaml`:

| Field | Default | Purpose |
|-------|---------|--------|
| `checkpoint.enabled` | `true` | Global switch. Disable only for short throwaway scripts |
| `checkpoint.cleanup_on_success` | `true` | `true` = production (clean up after success); `false` = debug (retain artifacts) |
| `checkpoint.max_resume_age` | `604800` (7 days) | How long a crashed/interrupted task remains resumable |
| `checkpoint.heartbeat_interval` | `5` (seconds) | How frequently the heartbeat file is refreshed; determines crash detection latency |

**Crash detection threshold**: A task is declared `crashed` if its heartbeat file is >30 seconds stale, the PID is dead, or the status field is `stopped`/`exited`.

**Two-level heartbeat**:
- **Supervisor heartbeat** (`{task_id}/heartbeat.json`): tracks overall step count and process liveness.
- **Worker heartbeat** (`{task_id}/workers/{name}/heartbeat.json`): aggregates status/step for all concurrent worker calls.

**Resume flow**: On re-run with the same `task_id`, the framework restores Supervisor memory steps and skips already-completed Worker calls (matched by `input_hash`), then continues from the interruption point.

### Configuration Best Practices

| Scenario | Recommended Settings |
|----------|---------------------|
| **Production** | `cleanup_on_success: true`, `max_resume_age: 604800` (defaults — omit the block entirely) |
| **Debugging / inspection** | `cleanup_on_success: false`, `max_resume_age: 86400` |
| **Short throwaway scripts** | `enabled: false` (disables all overhead) |
| **Slow/large batch tasks** | `heartbeat_interval: 5` (default); increase only if disk I/O is a bottleneck |

---

## Pattern 8: Prompt Design and Output Contracts

**Principle**: Prompts handle "decision-making"; templated content and fixed formats are handled by Tools.

### Recommended Practices

- Avoid repeating the same constraint section across multiple Workers
- Use structured output contracts (JSON/table schemas)
- Clearly distinguish `[Evidence]` from `[Inference]` in review reports

### Review Focus

- Are large repeated templates consuming context?
- Are programmable formats being left to LLM memory?
- Are inferences being stated as facts?

---

## Pattern 9: Prompt Quality Baseline (Industry Standard)

**Principle**: Ensure instruction clarity and verifiability first, then pursue complex capabilities.

### Recommended Practices

- Clear role and goal: Define role, task objective, and success criteria in one sentence
- Executable constraints: Specify input boundaries, prohibitions, failure handling, escalation conditions
- Verifiable output: Fixed structure + required fields + evidence sources
- Small-sample regression: Compare results before and after changes using a fixed test set

### Review Focus

- Are there prompts with "clear goals but unclear acceptance criteria"?
- Are critical conditions written as "suggestions" rather than "requirements"?
- Is there a missing strategy for handling insufficient information scenarios?

---

## Anti-Pattern Quick Reference

- Having LLMs handle loop control and state persistence
- Workers simultaneously responsible for IO, orchestration, and analysis
- Expanding Worker count and parallelism without budget constraints
- Restructuring without reproducible evaluation
- Report recommendations that are unverifiable and non-actionable

---

## Pattern 10: Skills Configuration and Integration

**Principle**: Skills extend Agent capabilities without modifying framework code; choose the right invocation mode and avoid loading conflicts.

### Three Invocation Modes

| Mode | `allow-model` | Behavior | Use When |
|------|--------------|----------|----------|
| **Force-inject** | `"force-inject"` | Full instructions embedded in system prompt at initialization; LLM always follows | Critical capabilities (memory, safety rules) that must always be active |
| **On-demand** | `true` (default) | Listed in skill catalogue; LLM decides when to call `load_skill()` | Domain guides, workflow references, optional knowledge |
| **Hidden** | `false` | LLM completely unaware; runs silently via Hooks only | Event collection, visualization, transparent monitoring |

### Recommended Practices

- Use `force-inject` sparingly — each injected Skill consumes system prompt token budget
- Verify path resolution: Skill paths are relative to `AGENT_ROOT` (directory containing `config/system.yaml`), not the Agent YAML file location
- Understand the three-layer loading: Global (`config/system.yaml`) → Auto-discovered (`AGENT_ROOT/skills/`) → Agent-private (`skills` field). Same-name Skills in later layers override earlier ones with warnings
- Set `platform` when the Skill needs tool name mapping (e.g., `"Claude"` to use `tools_mapping.Claude`)

### Typical Risks

- Missing `force-inject` for critical Skills (e.g., not injecting `agent-recall-with-files` causes cross-session memory loss)
- Skill path resolution errors due to confusion between `AGENT_ROOT` and Agent YAML directory
- Duplicate Skill loading from multiple layers without awareness of override behavior
- Forgetting that Skills with `allow-model: false` cannot be loaded by LLM even via `load_skill()`

---

## Pattern 11: Agent Optional Parameter Best Practices

**Principle**: Correctly configure optional parameters to avoid silent failures and resource waste.

### Key Parameters

| Parameter | Default | Common Pitfalls | Recommended Practice |
|-----------|---------|----------------|---------------------|
| `model_type` | `default_model_type` from `llm.yaml` (usually `"common"`) | Specifying a non-existent type causes `ValueError` at runtime (no silent fallback) | Verify the type exists in `llm.yaml`; use `powerful` for complex reasoning, `fast` for simple tasks |
| `tool_call_type` | `"code_act"` | Using `tool_call` for Supervisors limits orchestration capability | Use `code_act` for Supervisors and complex Workers; `tool_call` only for simple single-tool Workers |
| `max_steps` | `80` | Default too high for simple Workers (wastes resources); too low for complex Supervisors | Set proportional to task complexity; simple Workers: 20-40; complex Supervisors: 60-120 |
| `planning_interval` | Not set | Long task chains without periodic planning lose track of progress | Set to 3-5 for Agents with 20+ expected steps |
| `smart_summary` | `false` | Long-context tasks overflow without compression | Enable for tasks processing large files or many steps |
| `prompt` | Framework default | Custom templates with wrong paths cause silent fallback to defaults | Verify template path exists; ensure content complements (not duplicates) `workflow` |

### LLM Configuration Isolation Rules

- **Never write** `model`, `llm`, or `langfuse` fields in Agent YAML or `system.yaml` — they are auto-filtered with warning logs
- Agent selects model via `model_type` only; all LLM parameters live exclusively in `config/llm.yaml`
- When `model_type` is unspecified, fallback chain: `default_model_type` → `"common"` type → built-in defaults

### Configuration Overlay Whitelist

Agent YAML can only override these 7 system-level fields: `system`, `smart_summary`, `tool_access_control`, `execution_env`, `code_agent`, `tools`, `prompt`. All other fields in Agent YAML are silently ignored for overlay purposes.

### Workflow Writing Quality

- Follow the **five-section structure**: ① Background & Role → ② Core Responsibilities & Constraints → ③ Execution Flow (Mermaid) → ④ Step-by-step Details → ⑤ Output Requirements
- Mermaid blocks receive **automatic framework enforcement** ("must be followed strictly") — leverage this by putting core logic in Mermaid
- Use `|` YAML literal block syntax for `workflow` (preserves newlines and indentation)
- Avoid hardcoded paths in `workflow` — use tools for dynamic discovery
