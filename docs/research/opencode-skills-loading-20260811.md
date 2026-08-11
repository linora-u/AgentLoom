# OpenCode Skills loading and composition

Date: 2026-08-11
Reference version: OpenCode `v1.18.3`, commit `127bdb30784d508cc556c71a0f32b508a3061517`

## Conclusion

OpenCode treats on-demand loading as a runtime invariant, not a user-selectable
`eager` / `on_demand` policy. The model initially receives only the available
skill summaries. It calls the `skill` tool when a description matches the task,
and only that tool result adds the selected skill body to the conversation.

There is one important implementation nuance: in v1.18.3, discovery parses each
`SKILL.md` and caches its body in process memory. Therefore, "on demand" describes
model-context injection, not necessarily first-time disk I/O.

## Primary-source evidence

- Discovery scans conventional OpenCode, Claude, and Agents skill directories,
  plus extra configured paths and URLs: [skill discovery source](https://github.com/anomalyco/opencode/blob/127bdb30784d508cc556c71a0f32b508a3061517/packages/opencode/src/skill/index.ts#L173-L227).
- Discovery parses the Markdown and stores `md.content` in the skill state:
  [skill registration source](https://github.com/anomalyco/opencode/blob/127bdb30784d508cc556c71a0f32b508a3061517/packages/opencode/src/skill/index.ts#L105-L139).
- The system prompt exposes permitted skill summaries and tells the model to use
  the skill tool when a task matches: [system prompt assembly](https://github.com/anomalyco/opencode/blob/127bdb30784d508cc556c71a0f32b508a3061517/packages/opencode/src/session/system.ts#L98-L109).
- The `skill` tool checks permission and returns the selected body, base directory,
  and a sampled file list to the conversation: [skill tool implementation](https://github.com/anomalyco/opencode/blob/127bdb30784d508cc556c71a0f32b508a3061517/packages/opencode/src/tool/skill.ts#L21-L67).
- OpenCode v1 configuration contains only extra skill `paths` and `urls`; it has
  no load mode: [v1 skills schema](https://github.com/anomalyco/opencode/blob/127bdb30784d508cc556c71a0f32b508a3061517/packages/core/src/v1/config/skills.ts#L5-L12).
- The official documentation describes skills as loaded on demand and documents
  discovery, frontmatter, permission, and tool visibility: [OpenCode Skills](https://opencode.ai/docs/skills/), [OpenCode skill tool](https://opencode.ai/docs/tools/#skill).

## Comparison with AgentLoom

Before this refactor, AgentLoom had the essential catalogue-then-tool shape but
also exposed a second, configurable eager path. `load-mode`, per-item policy
inheritance, eager prompt injection, and TUI display state spread that choice
across the parser, runtime, prompt builder, validators, tests, and documentation.

That produced two divergent skill contracts:

- the Python runtime accepts `skills.items` with `load-mode` and execution policy;
- the embedded OpenCode 1.18.3 runtime is configured with `skills.paths` and no
  load mode.

The target contract should be:

```text
conventional roots + extra paths
  -> resolve one permitted skill catalogue
  -> put name/description summaries in the system context
  -> model calls skill(name)
  -> add that skill body and resource locations to the conversation
```

Skill activation must not grant tools, script authority, or network authority.
Those belong to the Agent's normal tool and permission model. If the `skill` tool
is disabled, its catalogue must not be shown.

## Implemented AgentLoom contract

- `SkillCatalog` owns discovery, scope precedence, summaries, and activation.
- Project and Application `skills/` directories are conventional roots;
  `skills.paths` is the only configuration extension.
- The `skills` toolset contains one model-facing tool: `skill(name)`.
- The prompt builder advertises summaries only when that tool is present.
- Agent scope overrides Application scope, which overrides project scope;
  duplicate names inside one scope are errors.
- `generated` proposal directories are pruned until proposals are promoted.
- Skill-specific resource, script, network, and loading-mode APIs were removed.

## Validation evidence

The implementation passed 3,593 Python tests (2 skipped), 199 Bun tests, and
TypeScript type checking. Real-model Application runs produced these outcomes:

| Application validation | Outcome |
|---|---|
| Skill activation and Agent override | `SKILL_RUNTIME_AGENT_OVERRIDE_V2`; log records `skill({name: precision-format})` |
| Skill tool disabled | `SKILL_TOOL_DISABLED_OK` |
| Todo-off ordinary run | `TODO_OFF_OK MOOL-4` |
| Core tool registry | `CORE_TOOL_REGISTRY_VALIDATION: PASS` |
| Markdown toolset | `MARKDOWN_TOOLSET_VALIDATION: PASS` |
| Search and LSP tools | `SEARCH_TOOLS_VALIDATION: PASS` |
| Native tool-call workflow | all four tool steps succeeded |
| Text ContextRef worker | `TEXT_CONTEXT_RETRIEVE_PASS` |
| JSON ContextRef worker | `JSON_CONTEXT_RETRIEVE_PASS` |
| Two-worker ContextRef flow | `MULTI_CONTEXT_RETRIEVE_PASS` |

Two additional code-act probes were interrupted after the external model stalled
before its first response; their manifests are marked `interrupted`, not passed.
