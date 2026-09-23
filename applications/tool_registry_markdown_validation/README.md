# Core File Markdown Validation

Real-LLM validation that Markdown artifacts use the canonical `core_file`
toolset after removal of the legacy Markdown-specific tools.

Run:

```bash
rm -f /tmp/agentloom_tool_registry_markdown_validation/report.md
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-tool-catalog-markdown \
  uv run loom run applications/tool_registry_markdown_validation/workflows/markdown_report_agent.yaml
```

The Agent writes the complete Markdown document with `write_file`, reads it back
with `read_file`, and reports success only after checking the actual content.

Acceptance evidence:

- final answer contains `MARKDOWN_WRITE_MIGRATION_VALIDATION: PASS`;
- `/tmp/agentloom_tool_registry_markdown_validation/report.md` contains the
  requested title and result marker;
- the Run `manifest.json` reports success;
- `logs/runtime.log` shows real `write_file` and `read_file` calls and no
  unexpected resolution failure.
