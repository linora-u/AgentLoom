# Architecture ownership and migration inventory

Research baseline: `ca27966d`. The implementation uses the user-selected layout:
responsibility modules live directly under `src/`, and standard setuptools
`package-dir` configuration maps that directory to the installed `agentloom`
package. There is no nested `src/agentloom/` or repository-root `agentloom/` tree.
Source paths and Python import names are distinct.

The migration is intentionally breaking for old Python imports and module
commands. Repository-owned callers, Applications, tests, templates and current
documentation use canonical names. The old `src.*` Python identity, alias finder,
synthetic namespaces and old module entry points are removed; the source package
initializer rejects loading under the name `src`. This inventory describes code
ownership, not completion of E1 acceptance or E2 review.

## Implemented owners

The first column records historical baseline imports only. It is not a set of
supported aliases.

| Baseline owner at ca27966d | Physical source owner | Canonical Python owner and responsibility |
| --- | --- | --- |
| `src.runner`, `src.application_run*`, `src.application_revision`, scattered definition interpretation | `src/application/` | `agentloom.application`: shared definition, paths, validation, presentation, Run identity, revision, lifecycle and execution |
| `src.lib.config` | `src/configuration/` | `agentloom.configuration`: project/model configuration, normalization, provenance and invocation binding |
| `src.lib.smolagents.agent` orchestration and assembly | `src/runtime/{agent,factory,invocation,loom_mixin}.py` | `agentloom.runtime`: Supervisor/Worker construction, delegation and runtime ownership |
| Hook policy, Skills, prompts, checkpoint, Goal, Todo, ContextEngine, storage/context | `src/runtime/` responsibility modules | `agentloom.runtime`: invocation state, authorization, recovery, usage, context and persistence |
| `src.lib.smolagents` upstream Agent subclasses, model adapters, patches and Tool conversion | `src/adapters/smolagents/` | `agentloom.adapters.smolagents`: actual coupling to fixed smolagents 1.26.0 |
| `src.mcp`, `src.services.lsp` | `src/adapters/{mcp,lsp}/` | `agentloom.adapters.mcp`, `agentloom.adapters.lsp`: external protocol connections |
| `src.tools` | `src/tools/` | `agentloom.tools`: lightweight catalog and selective implementation loading |
| `src.extensions.self_learning` | `src/self_learning/` | `agentloom.self_learning`: existing persistence, recording and review responsibilities |
| `src.__main__`, scaffold, TUI bridge, schedules | `src/__main__.py`, `src/scaffold.py`, `src/tui_bridge/`, `src/schedules/` | Canonical CLI, Studio and scheduling adapters consume Application and Run owners |

`agentloom.runtime.agent` owns orchestration; upstream CodeAgent/ToolCallingAgent
subclasses live in `agentloom.adapters.smolagents.agents`.
`agentloom.runtime.tool_protocol` owns terminal ToolCallRecord values without
importing smolagents. Tool execution and provider-message conversion stay in the
smolagents adapter and use those same values. Hook configuration and Run policy
can describe an outcome without importing upstream Tool execution. Runtime
patches install when a concrete Agent implementation is imported.

Configuration, registries and ContextVars have one implementation each. Canonical
package exports lazily reference their owners; they do not recreate old module
paths. The temporary C1/D1 alias implementation visible in earlier commits was
removed when the user rejected compatibility. It is not part of the final layout.

## Current public interfaces and preserved behavior

- Python callers use `agentloom`, including its lazy `C`, configuration accessors,
  `execute_app`, `run_app` and Run result/error/event exports. Direct imports use
  the canonical responsibility owners above.
- Supported commands are `loom` and `python -m agentloom`. CLI parameters, exit
  states, JSONL output and resume/task semantics are preserved. Old
  `python -m src` and `src.tui_bridge` commands are unsupported.
- TUI uses `python -I -u -m agentloom.tui_bridge`; installed-interpreter and
  project/uv selection must both resolve the installed package. The domain CLI
  uses `agentloom.tui_bridge.domain_cli` with explicit project context.
- Framework Tool strings and generated scaffolds use `agentloom.*`. Tools owned
  by Applications retain `applications.*` imports resolved from explicit project
  context. External integrations must migrate old framework import strings.
- YAML/Markdown definition format, typed Worker schemas, Worker reference rules,
  nested Application identity, configuration layering, Skill discovery and prompt
  semantics remain supported. Repository-owned definitions migrate with code.
- Editable and wheel installations expose `agentloom` outside the checkout,
  including isolated Python. YAML/Jinja/Markdown prompt resources and SCM syntax
  queries are bundled under the installed package. Wheels exclude the old `src`
  package identity and sibling repository assets.
- Checkpoint serialization, task/run manifests, cumulative usage, runtime data
  locations, historic task identity and committed effects retain their contracts.
  Historical source capsules bind files from their own revision; current source
  manifests bind the mapped `src/` tree. Reading old persisted data does not
  require an old Python import compatibility layer.
- Read-only catalog, definition and Studio entry points remain lazy and must
  cause no model requests, MCP processes, Shell Hooks or Run allocation.
- Shared topology inspection parses each Supervisor and Worker's Skill sources
  before Run allocation. Studio projects the same per-definition catalog;
  prepared runtime definitions retain the parsed instructions in their existing
  private snapshot seam. New inspections observe edits, while existing calls
  retain their Skill instructions. Resource-file sampling remains activation-time.

## Evidence and delivery gate

The required Python suite includes the full CI command and its two explicit
memory-campaign contract files, plus affected Application tests. Record collection
and pass/fail/error/skip totals. TUI requires tests, typecheck and build. Python
3.12 follows CI; the baseline Python 3.14 attempt failed in pinned `sqlean-py`,
so the migration does not change the dependency lock to use that interpreter.

Real F1–F9 acceptance uses fresh controlled workspaces, explicit time and execution
limits, configured models, Run/Worker/Tool evidence and independent artifact
assertions. Baseline and failed candidate attempts remain retained. Historical
checkpoint recovery runs against saved baseline state without changing committed
effects. Installation and source-origin checks cover the mapped source layout.

Local evidence lives at
`/Users/bytedance/code/data_clear/agentloom-architecture-notes/`. Each worktree has
its own ignored `config/llm.yaml`; secret values, earlier runtime data and reference
checkouts are excluded from commits. Required E1 checks and E2 review must finish
before the final GitHub push. No result in this inventory waives that gate.
