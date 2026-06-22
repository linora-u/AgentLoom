# Tool Registry Markdown Validation

Small real-LLM validation app for the non-default `markdown_report` toolset.

Run:

```bash
uv run loom run applications/tool_registry_markdown_validation/workflows/markdown_report_agent.yaml
```

The agent explicitly enables `core_file` and `markdown_report`, writes a Markdown report, then reads it back.
