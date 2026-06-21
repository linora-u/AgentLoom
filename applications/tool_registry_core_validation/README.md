# Tool Registry Core Validation

Small real-LLM validation app for the AgentLoom core toolsets.

Run:

```bash
uv run loom run applications/tool_registry_core_validation/workflows/core_tools_agent.yaml
```

The agent must use the default global toolsets to create, edit, read, glob, and grep a temporary file under `/tmp/agentloom_tool_registry_core_validation`.
