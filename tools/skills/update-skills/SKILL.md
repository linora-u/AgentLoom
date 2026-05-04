---
name: update-skills
description: "When docs/en/ documentation, framework source code (src/), or core config files (config/system.yaml, config/llm.yaml) change, detect the scope of changes and synchronize updates to all Skills' SKILL.md and references/*.md under tools/skills/, ensuring Skill content stays consistent with the latest documentation and code."
---

# Update Skills

After changes to AgentLoom project documentation (`docs/en/` and repo-root `README.md`), framework source code (`src/`), or core config files (`config/system.yaml`, `config/llm.yaml`), use this Skill to automatically detect changed content, locate affected Skills, and update SKILL.md and references/*.md files under `tools/skills/` one by one.

## Prerequisites (Required)

- **Navigate to the AgentLoom root directory first** before executing any operations of this Skill.
- **Root directory identification criteria**: `<project_root>/config/llm.yaml` exists (that is, `config/llm.yaml` must be directly reachable from the current working directory).
  - ⚠️ Do not use `config/system.yaml` for identification, as application-level directories may also contain this file (e.g., `applications/ai_quality_analysis/config/system.yaml`), which cannot uniquely identify the project root.
  - `config/llm.yaml` is globally unique and only exists in the AgentLoom root directory.
- Project paths (`docs/*`, `src/*`, `config/*`) are resolved relative to the AgentLoom root directory.
- Skill-local references (`./references/*`) are resolved relative to the current Skill root directory.
- `tools/skills/update-skills/` serves only as a source of update rules and is **not a target for updates**.

> **📖 Supporting Reference Documents** (consult as needed):
> - [references/doc-skill-mapping.md](./references/doc-skill-mapping.md) — Document/Code → Skill Impact Mapping Table
> - [references/update-checklist.md](./references/update-checklist.md) — Per-Skill Update Checklist
>
> **📖 Authoritative Specification Documents** (must read when updating):
> - `docs/en/agent_config.md` — Agent YAML Configuration Complete Reference
> - `docs/en/skills_config.md` — Skills Configuration Complete Reference
> - `docs/en/system_config.md` — System Configuration Complete Reference
> - `docs/en/llm_config.md` — LLM Configuration Complete Reference
> - `docs/en/config-overview.md` — Configuration System Overview
>
> Path base rules:
> - `./references/*` is relative to the current Skill root directory.
> - `docs/*`, `src/*`, and `config/*` are relative to the AgentLoom root directory.

## Applicable Scenarios

- Documents under `docs/en/` have changed (added, modified, or deleted), and Skills under tools/skills that reference these documents need to be updated accordingly
- The repository root `README.md` has changed, and framework concept descriptions referenced by Skills may need sync
- Framework source code under `src/` has changed (config parsing, Skills loading, tool system, etc.), which may affect behaviors or constraints described in Skills
- `config/system.yaml` or `config/llm.yaml` has changed, potentially affecting tool defaults, override semantics, or model_type behavior described in Skills
- User says "update skills", "sync skills with docs", "docs changed, skills need to follow"
- A new `docs/en/` document has been added, and references need to be added to relevant Skills

## Non-Applicable Scenarios

- Creating a brand new Skill (use `create-skill` Skill instead)
- Creating a new Application (use `create-app` Skill instead)
- Reviewing Workflow quality (use `workflow-review` Skill instead)
- Projects not based on the AgentLoom framework

## Execution Strategy

| Environment | Strategy |
|-------------|----------|
| **Interactive** (VS Code Copilot Chat / Terminal dialogue) | Present change impact analysis first, update Skills one by one after confirmation, show diff summary after each Skill update |
| **Autonomous** (Copilot Codex / Claude Code / Batch processing) | Automatically complete all four phases, attach change summary |

> **Core Principles**:
> - When encountering unclear or uncertain situations, **ask the user directly**.
> - Preserve the original style and structure of the Skill when updating; only modify content related to the changes.
> - Do not fabricate information; all updates must be backed by documentation or code.

---

## Phase 1: Change Detection

**Goal**: Identify changes in sources supported by the mapping table (`docs/en/`, repo-root `README.md`, key directories under `src/`, `config/system.yaml`, `config/llm.yaml`).

> **Consistency Rule**: Phase 1 change sources = `references/doc-skill-mapping.md` supported sources. These two must stay in sync to prevent missed detections or updates.

### 1.0 Root Validation (Fail-Fast, run first)

Before any Phase 1 detection command, verify that the current working directory is the AgentLoom root:

```bash
# Run in current directory (do not cd elsewhere first)
pwd
test -f config/llm.yaml
```

- If `test -f config/llm.yaml` fails: **stop immediately**, `cd <project_root>` (the directory that contains `config/llm.yaml`), then continue.
- Only proceed when this check passes, to avoid scanning from an application subdirectory.

### 1.1 Detect Documentation Changes via Git

Execute the following commands to get the list of changed files and diff summary:

```bash
# Execute in the project root directory
cd <project_root>

# View recent change history for docs/en/ and repository-root README.md
git log --oneline -20 -- docs/en/ README.md

# View diff for docs/en/ + README.md since the last update (based on commit or tag)
# Method 1: Compare with a specific commit
git diff <base_commit>..HEAD -- docs/en/ README.md

# Method 2: Compare with the last known update point
git diff HEAD~N..HEAD -- docs/en/ README.md

# Method 3: See which files have changed
git diff --name-only <base_commit>..HEAD -- docs/en/ README.md
```

### 1.2 Detect Code Changes via Git

```bash
# View code and config changes related to Skills
git diff --name-only <base_commit>..HEAD -- \
  src/lib/config/ \
  src/lib/smolagents/skills/ \
  src/lib/smolagents/hooks/ \
  src/lib/smolagents/agent/ \
  src/lib/smolagents/models/ \
  src/tools/ \
  config/system.yaml \
  config/llm.yaml
```

### 1.3 Determine the Change Baseline

Strategy for selecting the change baseline (by priority):

1. **User-specified commit / tag**: User says "changes since xxx"
2. **Last commit that updated Skills**: Find via `git log --oneline -5 -- tools/skills/` to get the last modification point
3. **Last N commits**: When user says "recent changes", default to comparing the last 3–5 commits

### 1.4 Output: Changed Files List

```markdown
## Change Detection Results

### Documentation Changes
| File | Change Type | Summary of Key Changes |
|------|-------------|------------------------|
| docs/en/agent_config.md | Modified | Restructured as complete reference, added field quick-reference table and config override relationships |
| docs/en/skills_config.md | Added | Brand new Skills configuration complete reference document |
| README.md | Modified | Updated framework positioning/core concepts wording used by multiple Skills |
| ... | ... | ... |

### Code Changes
| File | Change Type | Affected Functionality |
|------|-------------|------------------------|
| src/lib/config/config.py | Modified | Config loading logic, overlay rules |
| ... | ... | ... |
```

---

## Phase 2: Impact Analysis

**Goal**: Locate affected Skills and specific files based on the changes.

### 2.1 Load the Mapping Table

Read `references/doc-skill-mapping.md` to obtain the complete "Document/Code → Skill File" mapping.

### 2.2 Match Affected Skills

For each changed file detected in Phase 1, look up the affected Skills and specific files in the mapping table:

- Default scope: All Skills under `tools/skills/` (including future new Skills)
- Exception: `tools/skills/update-skills/` is not an update target

```markdown
## Impact Analysis Results

### Affected Skills

| Skill | Affected File | Impact Source | Estimated Impact Scope |
|-------|---------------|---------------|------------------------|
| create-app | SKILL.md | docs/en/agent_config.md restructured | Templates and field descriptions need updating |
| create-app | references/quick-reference.md | docs/en/system_config.md changed | Tool list/constraints need syncing |
| create-skill | SKILL.md | docs/en/skills_config.md added | Need to verify referenced section numbers |
| ... | ... | ... | ... |
```

### 2.3 Interactive Confirmation (Interactive Mode)

Present the impact analysis results to the user and confirm the update scope:

```
The following Skills need updating. Confirm?
1. create-app — SKILL.md, references/quick-reference.md, references/templates.md
2. create-skill — SKILL.md, references/skill-template.md
3. workflow-review — references/system-tools.md

[Update All / Selective Update / Cancel]
```

---

## Phase 3: Per-Skill Update

**Goal**: Execute updates for each affected Skill file.

### 3.1 Update Workflow (Per File)

```
For each affected file:
  0. If the file is under tools/skills/update-skills/, skip it (this directory is not written back to)
  1. Read the current content of the Skill file
  2. Read the corresponding latest docs/en/ document content (post-change version)
  3. Identify the specific sections in the Skill file that need updating
  4. Execute the update:
     - Only modify content related to the changes
     - Preserve the original file structure and style
     - Do not alter unaffected sections
  5. Record a summary of the updates
```

### 3.2 SKILL.md Update Strategy

| Update Item | What to Check | Action |
|-------------|---------------|--------|
| frontmatter.description | Whether the feature description in the documentation has changed | Sync update the description text |
| Applicable / Non-Applicable Scenarios | Whether features were added/removed in the documentation | Add/remove corresponding entries |
| Information Extraction Checklist | Whether config fields were added/modified in the documentation | Sync update checklist items |
| Solution Template | Whether template formats/fields changed in the documentation | Sync update YAML templates |
| Document Reference Paths | Whether document filenames/locations changed | Fix reference links |
| Section Number References | Whether document sections were renumbered | Fix section numbers |

### 3.3 references/*.md Update Strategy

| File Type | Update Focus |
|-----------|--------------|
| quick-reference.md | Tool list, model_type rules, execution_env options, constraint checklist, overlay allowlist |
| templates.md | Field names, default values, and comment descriptions in YAML templates |
| troubleshooting.md | Error message text, troubleshooting steps, validation scripts |
| skill-template.md | SKILL.md frontmatter fields, Hook event list, invocation-control configuration |
| hook-scripts-guide.md | Environment variable list, output JSON format, decision values, exit code rules |
| system-tools.md | Tool discovery workflow, config field paths |
| review-checklist.md | Review dimensions and checklist items |
| best-practices.md | Design patterns and best practices |

### 3.4 Update Principles

1. **Minimal Changes**: Only modify content directly related to documentation/code changes; do not perform unrelated refactoring
2. **Maintain Consistency**: Use the same terminology, formatting style, and heading levels as the original file
3. **Evidence-Based**: Every modification must be traceable to a specific documentation/code change
4. **No Missed References**: If a concept is referenced in multiple places within a Skill, all references must be updated
5. **Preserve Context**: When updating tables, code blocks, and other structures, maintain contextual integrity

---

## Phase 4: Consistency Verification & Summary

### 4.1 Post-Update Verification

Perform checks on each updated file:

```markdown
### Verification Checklist
- [ ] Update targets include only business Skills under tools/skills/ (excluding tools/skills/update-skills/)
- [ ] Section numbers referenced in SKILL.md for docs/en/ are correct
- [ ] Config field names in references/*.md match the latest documentation
- [ ] Fields and default values in YAML templates match the latest specifications
- [ ] Tool list reflects the latest config/system.yaml
- [ ] Hook event names/count match the latest skills_config.md
- [ ] Error messages and troubleshooting steps reflect the latest code behavior
- [ ] All internal cross-references (between Skills, between references) are valid links
- [ ] Default verification command `./run_tests.sh tests/skills_test` has been executed and passed
```

### 4.2 Test Validation Scope (Default)

- Default scope: run Skills-only tests via `./run_tests.sh tests/skills_test`
- Run full test suite (`./run_tests.sh`) only when the user explicitly requests it

### 4.3 Output Update Report

```markdown
# Skills Update Report

## Change Baseline
- Base commit: <base_commit_hash>
- Current commit: <head_commit_hash>
- Documentation changes detected: N files
- Code changes detected: M files

## Update Summary

### create-app
| File | Updated Content | Status |
|------|-----------------|--------|
| SKILL.md | Updated XXX section | ✅ Done |
| references/quick-reference.md | Synced tool list | ✅ Done |

### create-skill
| File | Updated Content | Status |
|------|-----------------|--------|
| SKILL.md | Updated information extraction checklist | ✅ Done |

### workflow-review
| File | Updated Content | Status |
|------|-----------------|--------|
| references/system-tools.md | Updated config paths | ✅ Done |

## Consistency Verification
- All passed / X items require manual confirmation

## Notes
- <Any special situations requiring user attention>
```

---

## Appendix: Quick Usage Examples

### Example 1: Update After Documentation Changes

```
User: The docs/en docs have changed, help me update all skills under tools/skills
AI: (load update-skills) → Detect changes → Analyze impact → Update one by one → Output report
```

### Example 2: Scoped Update

```
User: agent_config.md was rewritten, only update the create-app skill
AI: (load update-skills) → Only detect agent_config.md → Only update create-app → Output report
```

### Example 3: Update After Code Changes

```
User: The config logic in src/lib/config/ changed, skills need to be synced
AI: (load update-skills) → Detect code changes → Analyze impact (overlay rules, etc.) → Update relevant references → Output report
```
