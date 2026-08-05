# Self-Learning Toolset Smoke Validation

Real-LLM validation for the built-in `self_learning` toolset plus Shell and
file-read implementations. The workflow searches prior Run records, creates a
memory proposal, creates a generated Skill proposal, and verifies a temporary
file through actual tool calls.
Its Application config disables default toolsets and global Skills, so only the
six tools explicitly named in the Agent YAML are resolved.

Run it with an isolated runtime because it intentionally writes learning
records:

```bash
AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-tool-catalog-self-learning \
  uv run loom run applications/self_learning_smoke/workflows/self_learning_smoke_agent.yaml
```

Acceptance evidence:

- the final JSON has `"ok": true`;
- `memory_proposal`, `reference_file_written`, and `tmp_file_verified` are true;
- `skill_proposal_path` exists under the project's
  `skills/generated/proposals/`; move or remove that generated validation
  proposal after evidence inspection;
- the Run manifest is successful and its log records calls to
  `session_search`, `memory`, `skill_manage`, `shell_tool`, and `read_file`;
- no unexpected `Traceback`, `ERROR`, or implementation-resolution failure is
  present.
