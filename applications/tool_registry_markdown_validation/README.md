# Built-in Tool Catalog Markdown Validation

Real-LLM CodeAct validation for explicit toolset replacement and lazy loading
of the non-default `markdown_report` implementation group. The write and
read-back run in one model step so Todo bookkeeping cannot interfere with the
terminal answer.
The Application-level config disables unrelated global Skills; Agent
`toolsets:` is therefore the complete built-in capability list for this run.

Run:

```bash
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-tool-catalog-markdown \
  uv run loom run applications/tool_registry_markdown_validation/workflows/markdown_report_agent.yaml
```

The Agent explicitly replaces global defaults with `core_file` and
`markdown_report`, writes a Markdown report, and reads it back.

Acceptance evidence:

- final answer contains `MARKDOWN_TOOLSET_VALIDATION: PASS`;
- `/tmp/agentloom_tool_registry_markdown_validation/report.md` contains the
  requested title and result marker;
- the Run `manifest.json` reports success;
- `logs/runtime.log` shows real `write_markdown_file` and `read_file` calls and
  no unexpected resolution failure.
