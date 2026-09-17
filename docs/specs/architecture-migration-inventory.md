# Architecture ownership and compatibility inventory

Baseline: `ca27966d`. This inventory precedes implementation; the final evidence
report records actual moves and validation, rather than treating this plan as a
passing result.

## Responsibilities

| Current owner | Target owner | Reason |
| --- | --- | --- |
| `src.runner`, `application_run*`, `application_revision`, scattered definition interpretation | `agentloom.application` | One Application definition, identity, revision and execution contract |
| `src.lib.config` | `agentloom.configuration` | Shared project/model configuration, normalization and provenance |
| `src.lib.smolagents.agent` orchestration and definition assembly | `agentloom.runtime` / `agentloom.application` | Supervisor/Worker ownership and definition semantics are AgentLoom behavior |
| `src.lib.smolagents` upstream Agent subclasses, model adapters, patches, tool conversion | `agentloom.adapters.smolagents` | Keep actual coupling to fixed smolagents 1.26.0 visible |
| Hook policy/dispatch, skills, checkpoint, Goal, Todo, ContextEngine, runtime storage/context | `agentloom.runtime` | Invocation-specific state and policy stay together |
| `src.mcp`, `src.services.lsp` | `agentloom.adapters.mcp`, `agentloom.adapters.lsp` | External protocol connections |
| `src.tools` | `agentloom.tools` | Preserve light catalog and selective implementation loader |
| `src.extensions.self_learning` | `agentloom.self_learning` | Preserve the existing cohesive persistence/review scope |
| `src.__main__`, scaffold, TUI bridge, schedules | CLI / Studio / scheduling adapters | Consume shared Application and Run truth |

The final layout must contain implementations with these responsibilities, not
parallel copies or a new forwarding hierarchy. Behavior changes and mechanical
moves are separate commits.

## External interfaces to preserve

- Python package exports in `src.__all__`, including `C`, `get_config`, model and
  toolset accessors, `execute_app`, `run_app` and all Run result/error/event types.
- Existing direct `src.*` imports: factory, Agent subclasses, Hook types,
  ToolCallRecord, tools, model/config access, checkpoint serializers, tracing
  ContextVars and registries. New and old paths must resolve to identical modules
  or the same objects; patching a legacy module must affect the implementation.
- `loom` console script, `python -m src`, new `python -m agentloom`, existing CLI
  parameters, exit states, JSONL output, resume/task identity.
- TUI isolated Python bridge (`python -I -u -m src.tui_bridge` at baseline), both
  installed-interpreter and project/uv fallback selection.
- String imports in Application `tools` definitions and generated scaffolds;
  application-owned `applications.*` tools; dynamic function lookup.
- YAML/Markdown workflows, Worker typed schemas and relative/absolute Worker
  references; Application config layering, nested Application identity, Skill and
  prompt paths. Existing valid Applications require no manual rewrites.
- Bundled YAML/Jinja/Markdown prompt and workflow resources, CLI/TUI resource
  discovery, editable and wheel installs from outside the checkout.
- Checkpoint serializer class lookup, task/run manifests, cumulative usage,
  filesystem/runtime home defaults, historic task identity and committed effects.
- Read-only catalog, definition and Studio entry points must remain lazy and
  cause no model requests, MCP processes, Shell Hooks or Run allocation.

## Baseline and evidence protocol

The full Python suite is the CI command including the two explicit memory campaign
contract files. Record test collection and pass/fail/error/skip totals. TUI requires
tests, typecheck and build. Python 3.12 follows CI; automatic Python 3.14 selection
fails in the existing pinned `sqlean-py` source dependency, so it is not used to
change the dependency lock for this migration.

Real acceptance is F1–F9 in the specification. Each runner uses a fresh controlled
workspace, an explicit timeout and execution limits, genuine configured models,
Run/Worker/tool evidence and independent artifact assertions. Baseline failures
are retained; neither model success text nor two equally wrong read paths count
as verification. Baseline recovery material is retained outside the checkout for
candidate-version resume.

Local evidence: `/Users/bytedance/code/data_clear/agentloom-architecture-notes/`.
Ignored `config/llm.yaml` is copied into each worktree. No secret values, prior
runtime data, `codex/`, or `temp/` reference checkout is added to the PR.
