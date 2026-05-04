# Workflow Architecture Review — Four-Dimension Checklist (Enhanced)

This checklist is used for item-by-item review. You may only output items with findings, but the complete check must be performed.

---

## Pre-Gate Checks (Mandatory)

- [ ] Have you entered the AgentLoom root directory (criterion: `config/llm.yaml` exists)?
- [ ] If the root directory prerequisite fails during relative path review, was the process immediately aborted?

---

## Dimension 1: Workflow Process Design

### 1.1 Phase Division & Complexity

- [ ] Does each phase have a single responsibility?
- [ ] When there is only 1 phase, can a single Agent be used directly (no additional orchestration needed)?
- [ ] When there are 2-7 phases, is the division of labor clear?
- [ ] When there are 8+ phases, has merging or hierarchical orchestration been evaluated?

### 1.2 Workflow Instruction Quality (Five-Section Structure)

- [ ] Does the workflow follow the recommended five-section structure? (① Background & Role → ② Core Responsibilities & Constraints → ③ Execution Flow with Mermaid → ④ Step-by-step Details → ⑤ Output Requirements)
- [ ] Does each step have explicit **success criteria** and **failure handling**?
- [ ] Are critical rules emphasized with **bold** formatting? Are numbered lists used for ordered steps?
- [ ] Is the `workflow` field either `|` YAML literal block syntax, or a non-empty list whose string items are written as literal blocks?
- [ ] Are hardcoded file paths avoided in `workflow`? (should use tools to dynamically obtain paths)

### 1.3 Flowchart & Execution Instruction Consistency

- [ ] Are flowchart nodes consistent with registered capabilities (Worker/Tool)?
- [ ] Do conditional branches have explicit trigger conditions and termination conditions?
- [ ] Are there any contradictions between textual steps and the flowchart?
- [ ] Does the workflow contain a Mermaid code block? (Framework auto-injects "must be followed strictly" constraint when present — verify this enforcement is leveraged)
- [ ] Is the Mermaid syntax valid? (Framework validates via `mermaid-syntax-parser` and outputs warnings on errors)

### 1.4 Output & Completion Criteria

- [ ] Does each phase define verifiable completion criteria?
- [ ] Is the final output format reproducible and stable?
- [ ] Is a failure exit strategy defined?

### 1.5 Effort Budget & Planning

- [ ] Is `max_steps` set appropriately for each Agent? (default: 80; overly high wastes resources, overly low causes premature termination)
- [ ] Is `planning_interval` set for Agents with long task chains? (enables periodic self-reflection every N steps)
- [ ] Are concurrency limits and timeout strategies specified?
- [ ] Does the architectural complexity match the task value (avoid over-engineering)?

---

## Dimension 2: Supervisor-Worker Agent Coordination

### 2.1 Supervisor Responsibility Boundaries

- [ ] Is the Supervisor primarily responsible for scheduling/decision-making, avoiding execution of detailed work?
- [ ] Is the Worker invocation order and conditions defined?
- [ ] Are there any redundant relay calls?

### 2.2 Worker Contract Completeness

- [ ] Does each Worker have a clear input/output contract?
- [ ] Does `inputs` cover all information required for execution?
- [ ] Can the output be directly consumed by downstream components?

### 2.3 Explicit Dependencies

- [ ] Are there implicit dependencies between Workers (files/environment/global variables)?
- [ ] Have implicit dependencies been converted to explicit parameters or documented as clear data flows?

### 2.4 Coordination Overhead & Translation Loss (New)

- [ ] Does the Supervisor repeatedly rewrite Worker results, causing information loss?
- [ ] Is there a "telephone game" style relay causing semantic drift?
- [ ] Are direct pass-throughs allowed in necessary scenarios (reducing unnecessary paraphrasing)?

### 2.5 Delegation Deduplication (New)

- [ ] Are multiple Workers performing redundant analysis on the same input?
- [ ] Are there overlapping responsibilities causing duplicate tool calls?
- [ ] Is there a clear deduplication strategy?

---

## Dimension 3: Agent/Tool Responsibility Division (Core)

### 3.1 Agent Level

- [ ] Does the Worker's primary task genuinely require LLM comprehension?
- [ ] Should purely deterministic tasks be converted to Tools?
- [ ] Is there any logic requiring LLM judgment that was mistakenly placed in a Tool?

### 3.2 Prompt Level

- [ ] Are there deterministic instructions that could be Tool-ified (traversal, sorting, filtering, formatting, rule matching)?
- [ ] Are there duplicated constraints and templates across Workers?
- [ ] For mixed tasks, has the "Tool pre/post-processing + Agent reasoning" split been applied?

### 3.3 Encapsulation Pattern & Interaction Mode Selection

- [ ] Is the correct Agent-Tool path selected?
  - **Path A** (`worker_agents` auto-registration): For simple single-call Agent invocations
  - **Path B** (plain `module + function` tool): For deterministic logic without Agent involvement
  - **Path C** (Python-wrapped Agent via `create_agent_as_tool()`): For batch processing, checkpoint/resume, error isolation, progress persistence
- [ ] Can it be directly injected as a Worker (phase-independent, simple invocation)?
- [ ] Is LLM-generated code for ad-hoc flow control implementation avoided?
- [ ] Is `tool_call_type` appropriate? (`code_act` recommended for Supervisors and complex Workers; `tool_call` for simple single-tool Workers)

### 3.4 Model & LLM Configuration

- [ ] Does the specified `model_type` exist in `config/llm.yaml`? (Non-existent types cause `ValueError` at runtime — no silent fallback)
- [ ] Is `model_type` appropriate for the task? (`powerful` for complex reasoning, `fast` for simple classification, `summary` for extraction)
- [ ] Does the Agent YAML contain prohibited LLM fields (`model`, `llm`, `langfuse`)? (These are auto-filtered with warnings — all LLM params must be in `config/llm.yaml`)
- [ ] When `model_type` is omitted, is the `default_model_type` (defaults to `"common"`) from `llm.yaml` suitable?

### 3.5 Tool Capability Discovery

- [ ] Are tool capabilities dynamically discovered based on the target project, rather than hard-coding a tool list?
- [ ] Is the effective `default_loaded_tools` calculated based on the override chain (rather than drawing conclusions directly from side-by-side files)?
- [ ] Has the compatibility between `tools_mapping` and legacy `tools.mapping` been verified?
- [ ] Has the default tool availability under `execution_env` been verified?
- [ ] Do new Tool proposals have clear input/output contracts?

### 3.6 Skills Integration

- [ ] Are Skills correctly referenced? (path resolution is relative to `AGENT_ROOT`, not the Agent YAML file directory)
- [ ] Is the three-layer loading mechanism understood? (Global `config/system.yaml` → Auto-discovered `AGENT_ROOT/skills/` → Agent-private `skills` field; same-name Skills in later layers override earlier ones)
- [ ] Is `invocation-control.allow-model` set appropriately? (`"force-inject"` for critical capabilities like memory; `true` for on-demand; `false` for silent Hook-only)
- [ ] Is `platform` set correctly when tool name mapping is needed?
- [ ] Are there essential Skills missing from the configuration? (e.g., `agent-recall-with-files` for cross-session memory)

---

## Dimension 4: Error Handling & Resilience

### 4.1 Gates & Pre-Validation

- [ ] Are necessary gates in place (context, paths, configuration)?
- [ ] Does gate failure immediately terminate downstream calls?

### 4.2 Error Isolation & Retry

- [ ] Can a single Worker failure be isolated?
- [ ] Are retryable and non-retryable errors distinguished?
- [ ] Do retries have a maximum limit and backoff strategy?

### 4.3 Progress Recovery

- [ ] Does batch processing have checkpoint/progress tracking?
- [ ] Can execution resume from the last state after interruption?
- [ ] Can already-completed items be skipped?
- [ ] Is `checkpoint.enabled` set correctly for the use case? (Default `true` is appropriate for most scenarios)
- [ ] Is `checkpoint.cleanup_on_success` appropriate? (`true` for production, `false` for debugging/inspection)
- [ ] Is `checkpoint.max_resume_age` aligned with the task's business SLA? (Default 7 days; shorten for ephemeral tasks)
- [ ] Is `checkpoint.heartbeat_interval` reasonable? (Default 5s; increase only if disk I/O is a bottleneck)
- [ ] After a crash, can interrupted tasks be detected and resumed?

### 4.4 Context & Resource Management

- [ ] Is `smart_summary` enabled for tasks with long contexts that risk token overflow? (Controls LLM summarization vs simple truncation)
- [ ] Does `prompt` custom override (if present) conflict with `workflow` content? Does the referenced template file exist?
- [ ] Is `tool_access_control` configuration (include_paths, exclude_paths) appropriate for the Agent's file access needs?

### 4.5 Observability

- [ ] Are input/output summaries recorded at key nodes?
- [ ] Can error logs be directly used for problem diagnosis?
- [ ] Can the impact scope of proposed changes be traced?

---

## Cross-Cutting Requirement: Recommendations Must Be Verifiable (New)

Each finding must satisfy:

- [ ] Has `[Severity]`: `Must Fix (blocks release)` / `Recommended Optimization (non-blocking)`
- [ ] Has `[Evidence]`: one of field reference, original text, or code snippet
- [ ] Has `[Impact Assessment]`: describes impact (accuracy/maintainability/cost/risk)
- [ ] Has `[Improvement Suggestion]`: actionable and verifiable
- [ ] Has `[Confidence]`: High / Medium / Low
- [ ] Has `[Inference]`: Yes / No
