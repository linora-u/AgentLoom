# Built-in Tool Catalog Core Validation

Real-LLM validation for catalog lookup, lazy implementation loading, and the
default `core_shell`, `core_file`, and `core_search` toolsets.
The Application-level config disables unrelated global Skills while preserving
the default toolsets under test.

Run:

```bash
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-tool-catalog-core \
  uv run loom run applications/tool_registry_core_validation/workflows/core_tools_agent.yaml
```

The Agent must make real calls to `shell_tool`, `write_file`, `edit_file`,
`read_file`, `glob_search`, `grep_search`, and `list_directory`. It writes only
under `/tmp/agentloom_tool_registry_core_validation`.

Acceptance evidence:

- final answer contains `CORE_TOOL_REGISTRY_VALIDATION: PASS` and one PASS row
  per tool;
- the final file contains `ALPHA one`, `beta two`, and `GAMMA three`;
- the Run `manifest.json` reports a successful terminal state;
- `logs/runtime.log` contains the actual tool calls and no unexpected
  `Traceback`, `ERROR`, or implementation-resolution failure.

This Application validates runtime behavior. Fresh-interpreter dependency
direction is covered separately by
`tests/tools_test/test_tool_catalog_boundaries.py`.
