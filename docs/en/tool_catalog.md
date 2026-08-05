# Built-in Tool Catalog

AgentLoom keeps built-in tool metadata separate from Python implementation
loading. This makes toolset validation, ContextEngine routing, path protection,
and documentation generation safe to run without importing Shell,
self-learning, Todo, or ContextEngine implementations.

## Runtime contract

| Module | Owns | Must not own |
|---|---|---|
| `src/tools/catalog.py` | `ToolSpec`, toolset membership, descriptions, safety and output metadata, implementation references | Imports of concrete tool packages or runtime configuration |
| `src/tools/loader.py` | Resolving one registered implementation reference to a callable | Tool metadata or toolset membership |
| `src/tools/tool_meta.py` | Effective metadata after global and Agent overrides | Catalog exports or implementation loading |
| `src/tools/<group>/` | Concrete tool implementations | Built-in catalog membership |

`src.tools` intentionally exports nothing. Importing that package is not a
registration mechanism and does not load tools. Runtime code resolves a
registered built-in through `src.tools.loader`; code that needs only metadata
imports `src.tools.catalog`. Tests that exercise one concrete implementation
may import its group directly.

These rules preserve the dependency direction:

```text
metadata consumers ──> catalog <── loader ──> selected implementation
                               ^
effective metadata ────────────┘
```

The catalog is the single source of truth. A function that exists under
`src/tools/` is not a built-in tool until it has a `ToolSpec` in the catalog.
Dynamic YAML tools and generated Worker tools do not belong in this catalog.
Goal tools are injected by Goal Mode and are also outside the default catalog.

## Toolsets

`config/system.yaml` `default_toolsets` supplies the global list. Agent YAML
`toolsets:` replaces that list; it does not append to it. `toolsets: []`
disables built-in tools for that Agent.

| Toolset | Built-in tools |
|---|---|
| `core_shell` | `shell_tool`, `check_background_task`, `kill_background_task`, `list_background_tasks` |
| `core_file` | `read_file`, `edit_file`, `write_file`, `list_directory` |
| `core_search` | `grep_search`, `glob_search` |
| `context` | `loom_retrieve_context` |
| `skills` | `load_skill`, `list_skills` |
| `self_learning` | `session_search`, `session_scroll`, `memory`, `skill_manage` |
| `planning` | `todo_write` |
| `markdown_report` | `write_markdown_file`, `write_markdown_file_raw`, `append_markdown_sections` |
| `code_nav` | `get_file_outline`, `ast_grep_search_file`, `lsp_find_definition`, `lsp_find_references`, `lsp_get_document_symbols`, `lsp_hover`, `lsp_get_workspace_symbols` |

The default set currently includes `core_shell`, `core_file`, `core_search`,
`context`, `skills`, and `self_learning`. `planning` is selected by Todo policy;
the reporting and code-navigation sets are opt-in.

## Adding a built-in tool

1. Put the implementation in the narrowest existing tool group, or create a
   group with a clear responsibility.
2. Add one `ToolSpec` to `src/tools/catalog.py`, including an explicit
   implementation reference and complete safety/output metadata.
3. Do not add a root-package export and do not import the implementation from
   the catalog.
4. Add resolution, metadata, and fresh-interpreter import tests. Metadata-only
   tests must prove that no implementation group was loaded.
5. Run real Applications that cover the affected toolset and inspect their Run
   manifests, logs, tool calls, and output artifacts.

The required focused checks are:

```bash
uv run pytest \
  tests/tools_test/test_tool_catalog_boundaries.py \
  tests/hooks_test/test_tool_resolve.py \
  tests/hooks_test/test_tool_meta.py -q
```

For a catalog or loader change, run at least these real workflows:

```bash
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-catalog-core \
  uv run loom run applications/tool_registry_core_validation/workflows/core_tools_agent.yaml
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-catalog-markdown \
  uv run loom run applications/tool_registry_markdown_validation/workflows/markdown_report_agent.yaml
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-catalog-resolve \
  uv run loom run applications/test_demo/workflows/test_tool_resolve_agent.yaml
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-catalog-context \
  uv run loom run applications/context_engine_text_retrieve_validation/workflows/context_engine_text_retrieve_validation_agent.yaml
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-catalog-self-learning \
  uv run loom run applications/self_learning_smoke/workflows/self_learning_smoke_agent.yaml
```

Use a different `AGENTLOOM_RUNTIME_ROOT` for concurrent runs. A successful
process exit is not enough: inspect each `manifest.json`, `logs/runtime.log`,
and the files or database records produced by the tools.
