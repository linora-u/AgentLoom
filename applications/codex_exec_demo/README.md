# Codex Exec Demo

This application demonstrates that AgentLoom can call local `codex exec`
directly as normal function tools. The framework LLM does not need a custom
Python entrypoint or special system config; it only sees registered tools.

## Framework Support

AgentLoom provides `src.tools.codex.codex_tool.codex`, a built-in wrapper around
local `codex exec`. It can be registered like any other YAML tool through
`module/function`.

The tool signature is:

```python
codex(
    prompt: str,
    cwd: str = ".",
    model: str = "",
    timeout: str = "",
    sandbox: str = "",
    search: str = "",
) -> str
```

The return value is a JSON string with `success`, `output`, `logs`, `error`, and
`metadata`. Workflows should use `output` as the user-facing Codex result and
reserve `error` / `metadata` for failure diagnostics.

## Tool Registration

The Supervisor YAML registers the real `codex` function twice through
`module/function`. Each entry fixes its own prompt and runtime inputs. Because
all parameters are under `fixed_args`, the LLM-visible tools are zero-argument
tools named `codex1` and `codex2`; the LLM cannot override those fixed values.

```yaml
tools:
  - name: "codex1"
    module: "src.tools.codex.codex_tool"
    function: "codex"
    fixed_args:
      prompt: "Read pyproject.toml and return the project name and version."
      cwd: "."
      model: ""
      timeout: "600"
      sandbox: ""
      search: "false"
  - name: "codex2"
    module: "src.tools.codex.codex_tool"
    function: "codex"
    fixed_args:
      prompt: "Inspect the repository root and summarize top-level directories."
      cwd: "."
      model: ""
      timeout: "600"
      sandbox: ""
      search: "false"
```

At runtime, `YamlAgentFactory.get_tools_from_config()` loads
`src.tools.codex.codex_tool.codex` for both entries and exposes them as `codex1`
and `codex2`. The demo uses `tool_call` mode, so the LLM invokes `codex1` first
and `codex2` second through structured tool calls. Both tools take no
LLM-provided arguments because all `codex` inputs are fixed in YAML.

## Notes

- `codex` CLI must be installed and available on `PATH`.
- `codex login status` must succeed before this tool can run.
- AgentLoom does not receive, print, or persist Codex credentials.
- Empty `sandbox` does not pass `--sandbox`; Codex uses local CLI config and its
  own defaults. Valid explicit values are `read-only`, `workspace-write`, and
  `danger-full-access`.
- Empty `search` or `"false"` does not pass `--search`; `"true"` enables Codex
  native web search by passing `--search`.
- AgentLoom does not pass `--ask-for-approval`; approval behavior follows the
  local Codex CLI configuration.
- Use `fixed_args` when a workflow must lock Codex inputs. If a parameter is not
  fixed, it remains part of the tool schema and may be provided by the LLM.

## Run

The demo does not need a Python entrypoint. Run the workflow YAML directly:

```bash
uv run loom run applications/codex_exec_demo/workflows/use_codex_exec_demo.yaml
```

`loom run` loads the Supervisor YAML, registers `codex1` and `codex2`, and lets
the workflow call them through the normal tool-call path.
