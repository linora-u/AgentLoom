# Document/Code → Skill Impact Mapping Table

> This file defines how changes to `docs/en/` documentation and `src/` code affect specific files of each Skill under `tools/skills/`.
> When changes are detected, use this mapping table to locate the Skill files that need updating.
>
> **Scope Notes (Extensible)**:
> - By default, covers all Skills under `tools/skills/` (including future new Skills).
> - `tools/skills/update-skills/` serves only as a rule source and is not an update target.
> - After adding a new Skill, append the document/code mapping entries for that Skill to this table.

---

## 1. Document → Skill Mapping

### docs/en/agent_config.md (Agent YAML Configuration Complete Reference)

| Affected Skill | Affected File | Content to Sync |
|----------------|---------------|-----------------|
| `create-app` | `SKILL.md` | Information extraction checklist (required/optional fields), YAML template examples, field default values, Supervisor/Worker role definitions |
| `create-app` | `references/templates.md` | Field names, default values, and comment descriptions in Supervisor/Worker YAML templates |
| `create-app` | `references/quick-reference.md` | Checkpoint config fields (Section 6), overlay allowlist fields (Section 7), key constraint checklist (Section 8), worker_agents parsing rules (Section 9), agent_function_schema.inputs naming (Section 5) |
| `create-app` | `references/troubleshooting.md` | Worker loading failure troubleshooting, agent_function_schema related errors |
| `create-app` | `references/full-example.md` | Fields, structure, and terminology in end-to-end examples must stay consistent with the latest Agent configuration |
| `create-app` | `references/agent-yaml-schema.json` | Agent YAML field definitions, required fields, and type constraints sync |
| `create-app` | `scripts/validate_application_yaml.py` | YAML validation logic must stay consistent with the latest field rules |
| `workflow-review` | `SKILL.md` | agent_function_schema contract check in review dimensions |
| `workflow-review` | `references/review-checklist.md` | Worker contract completeness check items (Dimension 2.2) |

### docs/en/skills_config.md (Skills Configuration Complete Reference)

| Affected Skill | Affected File | Content to Sync |
|----------------|---------------|-----------------|
| `create-skill` | `SKILL.md` | Information extraction checklist, Skill type determination guide, Hook requirement determination guide, solution template |
| `create-skill` | `references/skill-template.md` | SKILL.md frontmatter field quick-reference table, Hook event quick-reference (9 events), invocation-control configuration syntax, abstract tool name mapping |
| `create-skill` | `references/hook-scripts-guide.md` | Environment variable list (5 variables), output JSON format (7 fields), decision values, exit code rules, common.py template |
| `create-app` | `SKILL.md` | Private skills configuration fields (Phase 1 checklist #14) |

### docs/en/system_config.md (System Configuration Complete Reference)

| Affected Skill | Affected File | Content to Sync |
|----------------|---------------|-----------------|
| `create-app` | `references/quick-reference.md` | Predefined tool list (tools.default), execution_env options, code_agent configuration; **Section 6 — checkpoint config fields** (enabled, cleanup_on_success, max_resume_age, heartbeat_interval) |
| `create-app` | `SKILL.md` | Application-level config/system.yaml override instructions (Information extraction checklist #12) |
| `create-app` | `references/full-example.md` | System config override fields and comment descriptions in examples |
| `create-app` | `references/agent-yaml-schema.json` | Field constraints related to system_config in the schema |
| `workflow-review` | `references/system-tools.md` | Config source discovery workflow (default_loaded_tools, tools_mapping) |
| `workflow-review` | `SKILL.md` | Config reading paths in the context scanning phase |
| `workflow-review` | `references/best-practices.md` | **Pattern 7 — AgentLoom checkpoint config fields and crash detection logic** |
| `workflow-review` | `references/review-checklist.md` | **Dimension 4.3 — checkpoint configuration audit items** |

### docs/en/llm_config.md (LLM Configuration Complete Reference)

| Affected Skill | Affected File | Content to Sync |
|----------------|---------------|-----------------|
| `create-app` | `SKILL.md` | model_type discovery and confirmation workflow, parameter inheritance chain, retry mechanism description |
| `create-app` | `references/quick-reference.md` | model_type selection rules (Section 2), dynamic discovery script |
| `create-app` | `references/troubleshooting.md` | Troubleshooting steps when model_type does not exist |
| `create-app` | `references/full-example.md` | model_type selection strategy and description in examples |

### docs/en/config-overview.md (Configuration System Overview)

| Affected Skill | Affected File | Content to Sync |
|----------------|---------------|-----------------|
| `create-app` | `SKILL.md` | 4-layer config loading order, LLM config isolation principle |
| `create-app` | `references/quick-reference.md` | Overlay allowlist fields, config override rules |
| `create-skill` | `SKILL.md` | Skills 3-layer loading order (global → auto-discover → Agent private) |

### README.md (Project Overview — Repository Root)

> **Note**: The project overview is at `README.md` (repository root), not `docs/en/README.md` (which does not exist). The Chinese version is at `docs/cn/README.md`.

| Affected Skill | Affected File | Content to Sync |
|----------------|---------------|-----------------|
| All Skills | Respective SKILL.md | Framework description, core concept terminology (e.g., "each Agent is a tool"), feature list |

---

## 2. Code → Skill Mapping

### src/lib/checkpoint/ + src/lib/heartbeat/ (Checkpoint & Heartbeat System)

| Affected Skill | Affected File | Content to Sync |
|----------------|---------------|------------------|
| `workflow-review` | `references/best-practices.md` | Pattern 7 — checkpoint config fields, heartbeat levels, crash detection threshold, resume flow |
| `workflow-review` | `references/review-checklist.md` | Dimension 4.3 — checkpoint configuration audit items |
| `create-app` | `references/quick-reference.md` | Section 6 — system-level checkpoint configuration table |

### src/lib/config/ (Config Loading Logic)

| Affected Skill | Affected File | Content to Sync |
|----------------|---------------|-----------------|
| `create-app` | `references/quick-reference.md` | Overlay allowlist fields (the actual allowlist defined in code may change) |
| `create-app` | `references/troubleshooting.md` | Error message text for config loading failures |
| `create-app` | `SKILL.md` | LLM isolation behavior (logic that filters model/llm/langfuse from Agent YAML) |
| `create-app` | `scripts/validate_application_yaml.py` | Config validation rules and error messages aligned with the latest implementation |

### src/lib/smolagents/skills/ (Skills Parsing and Loading)

| Affected Skill | Affected File | Content to Sync |
|----------------|---------------|-----------------|
| `create-skill` | `references/skill-template.md` | Supported fields in SKILL.md frontmatter (aligned with parser.py) |
| `create-skill` | `references/hook-scripts-guide.md` | Hook execution workflow, environment variable passing logic |
| `create-skill` | `SKILL.md` | Skill registration and loading mechanism description |

### src/lib/smolagents/agent/ (Agent Factory and Base Classes)

| Affected Skill | Affected File | Content to Sync |
|----------------|---------------|-----------------|
| `create-app` | `SKILL.md` | Agent creation workflow, YAML field parsing logic |
| `create-app` | `references/templates.md` | Validity of fields in YAML templates |
| `create-app` | `references/full-example.md` | Example directory structure aligned with Agent parsing behavior |
| `create-app` | `references/agent-yaml-schema.json` | Schema consistency with Agent parsing behavior |

### src/tools/ (Tool System)

| Affected Skill | Affected File | Content to Sync |
|----------------|---------------|-----------------|
| `create-app` | `references/quick-reference.md` | Actual function names and capability descriptions of predefined tools |
| `workflow-review` | `references/system-tools.md` | Tool discovery strategy, default tool loading mechanism |
| `workflow-review` | `SKILL.md` | How scan_tools.py is invoked |
### src/lib/smolagents/hooks/ (Hook Type Definitions and Manager)

| Affected Skill | Affected File | Content to Sync |
|----------------|---------------|------------------|
| `create-skill` | `references/hook-scripts-guide.md` | HookResult fields (decision/modified_input/modified_response/agent_context/user_message/reason/telemetry), HookContext fields passed via HOOK_CONTEXT_JSON, decision allowed values |
| `create-skill` | `references/skill-template.md` | Hook event names from HookEvent enum, matcher behavior |
| `create-skill` | `SKILL.md` | Hook execution workflow, HookResult processing logic |
### src/lib/smolagents/models/ (Model Type System)

| Affected Skill | Affected File | Content to Sync |
|----------------|---------------|-----------------|
| `create-app` | `references/quick-reference.md` | model_type dynamic discovery script |
| `create-app` | `SKILL.md` | model_type discovery and confirmation workflow |
| `create-app` | `references/full-example.md` | model_type demonstration in examples aligned with the actual available type mechanism |

---

## 3. Quick Lookup Guide

When you know which file has changed, use this quick-reference table to locate affected Skills:

| Changed File | Affected Skills (Priority Order) |
|--------------|----------------------------------|
| `docs/en/agent_config.md` | create-app (High) > workflow-review (Medium) |
| `docs/en/skills_config.md` | create-skill (High) > create-app (Low) |
| `docs/en/system_config.md` | create-app (High) > workflow-review (Medium) |
| `docs/en/llm_config.md` | create-app (High) |
| `docs/en/config-overview.md` | create-app (Medium) > create-skill (Low) |
| `README.md` (repo root) | All (Low) |
| `src/lib/config/` | create-app (High) |
| `src/lib/smolagents/skills/` | create-skill (High) |
| `src/lib/smolagents/hooks/` | create-skill (High) |
| `src/lib/smolagents/agent/` | create-app (Medium) |
| `src/tools/` | create-app (Medium) > workflow-review (Medium) |
| `src/lib/smolagents/models/` | create-app (Medium) |
| `config/system.yaml` | create-app (Medium) > workflow-review (Low) |
| `config/llm.yaml` | create-app (Medium) |
| `docs/en/system_config.md` (checkpoint section) | create-app (Medium) > workflow-review (Medium) |
| `src/lib/checkpoint/` | workflow-review (High) |
| `src/lib/heartbeat/` | workflow-review (High) |
