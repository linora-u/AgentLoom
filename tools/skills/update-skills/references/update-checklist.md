# Per-Skill Update Checklist

> Follow this checklist to inspect each Skill's files item by item, ensuring consistency with the latest `docs/en/` and `src/`.
> Only list items where findings exist (updates needed); items with no changes can be skipped.
>
> **Scope Notes (Extensible)**:
> - By default, applies to all target Skills under `tools/skills/` (including future new Skills).
> - `tools/skills/update-skills/` serves only as a rule source and is not an update target in this checklist.
> - For newly added Skills: first run the "General Check Framework", then append skill-specific check items.

---

## General Check Framework (All Target Skills)

- [ ] **Prerequisites**: Have checks been performed in the AgentLoom root directory (containing `config/llm.yaml`)
- [ ] **Source Consistency**: Are Phase 1 change sources consistent with `references/doc-skill-mapping.md` supported sources (to prevent missed detections or updates)
- [ ] **Document Reference Validity**: Are `docs/en/` and repository-root `README.md` paths, section numbers, and terminology still valid
- [ ] **Code Evidence Validity**: Are referenced source code paths, fields, and behavior descriptions still verifiable
- [ ] **Cross-Reference Validity**: Are links between Skills and between references still valid
- [ ] **Default Test Scope**: Has `./run_tests.sh tests/skills_test` been executed and passed (run full `./run_tests.sh` only when user explicitly requests)

---

## create-app

### SKILL.md

- [ ] **Required Fields Checklist**: Aligned with `docs/en/agent_config.md` Chapter 2 (YAML Template) and Chapter 3 (Field Reference Manual)
  - Is the number of required fields correct (name, description, workflow -- 3 required)
  - Is the optional field list complete (tools, model_type, tool_call_type, worker_agents, execution_env, prompt, skills, planning_interval, max_steps)
- [ ] **Information Extraction Checklist (#1-#14)**: Are default values and descriptions consistent with the latest documentation
  - `model_type` default value description: uses `config/llm.yaml`'s `model.default_model_type` only when configured; otherwise explicit `model_type` is required
  - `tool_call_type` default value: `code_act`
  - `execution_env` available values: `local`, `docker`, `e2b`, `wasm`
- [ ] **model_type Discovery and Confirmation Workflow**: Aligned with `docs/en/llm_config.md`
  - Parameter lookup chain: `models[model_type].param` -> code defaults
  - Available model_types should be dynamically discovered from `config/llm.yaml` (excluding `default_model_type`), not hardcoded to a fixed set
- [ ] **LLM Config Isolation Description**: Aligned with `docs/en/config-overview.md` and `docs/en/agent_config.md`
  - Agent YAML must not contain `model`/`llm`/`langfuse`; these will be automatically filtered with a warning output
- [ ] **Two Mode Description**: Supervisor + N Workers vs Single Agent
- [ ] **Path Strategy**: Project root directory location rules
- [ ] **Smart Recommendation Rules Table**: Do tool recommendations reflect the latest available tools

### references/quick-reference.md

- [ ] **Predefined Tools Full Table (Section 1)**: Aligned with `config/system.yaml`'s `default_loaded_tools` and tools actually registered in `src/tools/`
  - Have newly added tools been included
  - Have deleted/renamed tools been removed/updated
- [ ] **model_type Selection Rules (Section 2)**: Aligned with `docs/en/llm_config.md`
  - Is the dynamic discovery script still valid
- [ ] **execution_env.type Available Values (Section 3)**: Aligned with `docs/en/agent_config.md` Section 3.5
- [ ] **tool_call_type Comparison (Section 4)**: Are Agent type names (CodeAgentV2, ToolCallingAgentV2) still correct
- [ ] **agent_function_schema.inputs Common Naming (Section 5)**: Consistent with actual usage
- [ ] **Overlay Allowlist Fields (Section 6)**: Aligned with the actual overlay allowlist in `src/lib/config/config.py`
  - Current 7 fields: system, smart_summary, tool_access_control, execution_env, code_agent, tools, prompt
- [ ] **Key Constraint Checklist (Section 7)**: Consistent with the latest documentation and code behavior
  - Have the number or content of constraints changed
- [ ] **worker_agents.path Parsing Rules (Section 8)**: Is the parsing logic for the three notation styles still correct

### references/templates.md

- [ ] **Supervisor YAML Template (3.1)**: Field names, comments, and default values consistent with `docs/en/agent_config.md` Section 2.1
  - Skills configuration syntax (invocation-control format)
- [ ] **Worker YAML Template (3.2)**: Field names and comments consistent with `docs/en/agent_config.md` Section 2.2
- [ ] **Entry Script Template (3.3)**: Is the import path (`src.runner`) still correct
- [ ] **Custom Tool Template (3.4)**: Is the no-decorator, docstring extraction description still correct
- [ ] **Application-Level system.yaml Template (3.5)**: Are overridable fields consistent with the overlay allowlist

### references/troubleshooting.md

- [ ] **Error Message Text**: Consistent with actual error messages thrown in the code
- [ ] **Troubleshooting Steps**: Match the latest code behavior
- [ ] **Validation Scripts**: Python code snippets can run correctly on the current version

### references/full-example.md

- [ ] **End-to-End Example Flow**: Directory structure and fields consistent with the latest documentation and code behavior
- [ ] **model_type Strategy in Example**: Reflects dynamic discovery and the requirement to use a configured default or explicit type
- [ ] **skills/worker Configuration in Example**: Consistent with current `docs/en/agent_config.md` rules

### references/agent-yaml-schema.json

- [ ] **Field Definitions**: Required fields, optional fields, and type constraints consistent with current Agent YAML rules
- [ ] **Structural Completeness**: Supervisor/Worker related structures have not missed key fields
- [ ] **Constraint Consistency**: Enum values and default value descriptions consistent with documentation and implementation

### scripts/validate_application_yaml.py

- [ ] **Validation Rules**: Consistent with current documentation definitions and implementation logic
- [ ] **Error Messages**: Validation failure messages consistent with actual rules, avoiding misleading information
- [ ] **Script Runnability**: Executable in the `.venv` environment

---

## create-skill

### SKILL.md

- [ ] **Information Extraction Checklist (#1-#11)**: Aligned with `docs/en/skills_config.md`
  - Skill types: Force-inject / On-demand / Hidden
  - `invocation-control.allow-model` three values: `"force-inject"` / `true` / `false`
- [ ] **Skill Type Determination Guide Table**: `allow-model` values consistent with `docs/en/skills_config.md`
- [ ] **Hook Requirement Determination Guide Table**: Hook event count and names consistent with the latest specification
  - Current 9 events: TaskStart, TaskComplete, TaskFail, SubtaskStart, SubtaskFinish, PreToolUse, PostToolUse, PostToolError, Stop
- [ ] **Solution Template**: Displayed frontmatter fields and configuration examples
- [ ] **Document References**: Are section numbers for `docs/en/skills_config.md` correct
- [ ] **3-Layer Loading Order Description**: Global -> skills/ auto-discover -> Agent private

### references/skill-template.md

- [ ] **SKILL.md Complete Template**: Frontmatter fields consistent with fields actually supported in `src/lib/smolagents/skills/parser.py`
  - Supported frontmatter field list
  - Hook event name spelling
- [ ] **YAML Frontmatter Field Quick-Reference Table**: Field names, types, default values
  - Note: `platform` and `invocation-control` are not defined in SKILL.md (configured on the reference side)
- [ ] **Abstract Tool Name Mapping Table**: Read/Write/Edit/Bash/Glob/Grep -> actual tool names
- [ ] **9 Hook Events Quick-Reference Table**: Event names, matcher requirements, tool_name values, typical use cases
- [ ] **Reference-Side Configuration Quick-Reference (invocation-control)**: Syntax format
  - `allow-model`: `true` / `false` / `"force-inject"`
  - `allow-hook`: `true` / `false`
- [ ] **Three Skill Type Configuration Examples**: YAML example code

### references/hook-scripts-guide.md

- [ ] **5 Environment Variables**: Variable names and default values consistent with `src/lib/smolagents/skills/skills.py`
  - AGENT_NAME, TASK_ID, TOOL_NAME, HOOK_EVENT, HOOK_CONTEXT_JSON
- [ ] **HOOK_CONTEXT_JSON Structure**: Field list consistent with what is actually passed in the code
- [ ] **Output JSON Format -- 7 Fields**: decision, modified_input, modified_response, agent_context, user_message, reason, telemetry
- [ ] **decision Three Values**: allow / block / modify
- [ ] **Exit Code Rules Table**: Behavior for 6 combinations
- [ ] **common.py Template**: Function signatures and logic compatible with the framework

---

## workflow-review

### SKILL.md

- [ ] **Context Scanning Phase**: How scan_tools.py is invoked, path parameters
- [ ] **Capability Discovery Workflow**: Config field paths (default_loaded_tools, tools_mapping) consistent with `docs/en/system_config.md`
- [ ] **Four-Dimension Review Content**: Do review focus areas reflect the latest framework capabilities

### references/review-checklist.md

- [ ] **Dimension 1 -- Workflow Design**: Checklist item content
- [ ] **Dimension 2 -- Supervisor-Worker Coordination**:
  - Worker contract completeness (agent_function_schema fields)
  - Coordination overhead and translation loss checks
  - Delegation deduplication checks
- [ ] **Dimension 3 -- Agent/Tool Responsibility Separation**:
  - Tool capability discovery requirements (dynamic vs hardcoded)
  - Encapsulation pattern selection
- [ ] **Dimension 4 -- Error Handling & Resilience**:
  - Observability requirements
  - Progress recovery mechanisms

### references/best-practices.md

- [ ] **8 Pattern Contents**: Do new framework features require adding new patterns
- [ ] **Anti-Pattern Quick-Reference**: Do new anti-patterns need to be added
- [ ] **Pattern 7 — Checkpoint Config Fields**: Are `checkpoint.*` fields (enabled, cleanup_on_success, max_resume_age, heartbeat_interval) and their defaults current?
- [ ] **Pattern 7 — Crash Detection Threshold**: Is `HEARTBEAT_STALE_THRESHOLD` (30s) still accurate?
- [ ] **Pattern 7 — Two-Level Heartbeat**: Does Supervisor/Worker heartbeat description match current implementation?

### references/review-checklist.md

- [ ] **Dimension 4.3 — Checkpoint Audit Items**: Are all 8 checkpoint configuration checklist items (`enabled`, `cleanup_on_success`, `max_resume_age`, `heartbeat_interval`, crash recovery) current?

### references/system-tools.md

- [ ] **Step 1 -- Config Source Discovery**: Config paths and field names (`default_loaded_tools`, `tools_mapping`) consistent with `docs/en/system_config.md`
- [ ] **Step 2 -- Agent Actual Available Tools**: Discovery workflow description
- [ ] **Step 3 -- Capability Alignment**: Capability type classification
- [ ] **Step 4 -- When to Suggest Creating a New Tool**: Decision criteria
- [ ] **False Positive Warnings**: Do new scenarios need to be added

---

## Cross-Cutting Check Items (Common to All Skills)

- [ ] Do all referenced `docs/en/` paths and repository-root `README.md` still exist
- [ ] Are all `docs/en/` section numbers referenced in Skills still correct
- [ ] Are all framework terms used in Skills consistent with the latest documentation (e.g., Agent role names, config field names)
- [ ] Are Phase 1 change detection sources consistent with mapping table supported sources
- [ ] Do update results exclude `tools/skills/update-skills/`
- [ ] Can all YAML example code in references pass YAML syntax validation
- [ ] Can all Python code snippets in references run in the current `.venv` environment
- [ ] Are cross-references between Skills valid (e.g., create-app referencing create-skill, workflow-review referencing create-app)
