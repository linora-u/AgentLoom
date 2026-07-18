# AgentLoom TUI

AgentLoom TUI is the terminal product for creating Agent YAML and inspecting
every Agent System and Run in the current project. The user-facing command is
`agentloom`; the source directory and package make the TUI boundary explicit,
following OpenCode's product-command/internal-TUI-package split.

## Install and run

```bash
cd /path/to/AgentLoom
./install
# Open a new shell once, then run from the project you want to inspect:
agentloom
```

`./install` creates a current-platform standalone OpenTUI binary in
`~/.agentloom/bin` and a non-editable, `uv.lock`-pinned Python environment in
`~/.agentloom/venv`. The generated `agentloom` wrapper selects that Python
directly, so users do not activate a venv or type `uv run`. Missing `uv` or Bun
is installed with the respective official installer. Options:

```bash
./install --no-modify-path
AGENTLOOM_INSTALL_DIR=/custom/location ./install
```

The first option leaves shell configuration unchanged. The environment
variable relocates the binary, wrapper, and Python environment together.

For a non-interactive health check:

```bash
agentloom --snapshot
```

## What it does

- Chat: real model-backed general conversation plus bounded ReAct tools to
  inspect the project, stage Agent YAML in memory, and validate it. It cannot
  run Shell, Git, or an Agent. `/apply` is the only Agent-definition write path
  and requires the current draft revision.
- Workspace: `Ctrl+P` searches Applications, supervisor/worker Agents, Skills,
  Schedules, Runs, and commands. Entries remain observable even before their
  first run, and opening one keeps the conversation visible.
- Runs: merges canonical Run manifests, checkpoints, heartbeats, Workers,
  events, logs, artifacts, and durable results. Large event/log/artifact/result
  views use explicit bounded previews, so the live refresh loop cannot silently
  present partial data as complete or grow without limit. A lightweight global
  refresh updates every Agent, Run, and Schedule without reparsing Agent YAML.
- Details: click a workspace or recent-run entry. Use arrow/Page Up/Page Down/
  Home/End or the mouse wheel to scroll; `Esc` or `b` returns to chat.
  A never-run Agent shows its definition, topology, files, validation, and
  “尚未运行，无执行结果”; it never fabricates an execution result.
- Models: reads the sanitized model catalog and default from `config/llm.yaml`.
  API keys, base URLs, headers, and provider configuration never cross the
  bridge. Use `/models` for a keyboard/mouse model selector, or `/model <type>`
  for direct switching.
- Schedules: create, pause, resume, and remove durable local schedules with
  `/schedule ...`. Automatic firing is deliberately a separate foreground
  service, so it survives the TUI closing and never hides a background daemon.
  Keep it running in another terminal with:

  ```bash
  agentloom schedules --project /path/to/project serve
  ```

  The TUI reports `Scheduler: stopped` and warns after schedule creation until
  this service is running. The same store remains available to source users
  through `uv run loom schedules`.

Python entry files remain observable when they call `src.runner.run_app`, as
the existing `loom create` scaffold does. Calling a low-level Agent `.run()`
directly bypasses AgentLoom's canonical Run lifecycle and therefore cannot
provide complete status to any UI.

## Architecture

```text
OpenTUI + SolidJS (TypeScript)
  └─ long-lived NDJSON RPC
       └─ python -I -u -m src.tui_bridge
            ├─ Agent definitions + config/llm.yaml
            ├─ runtime runs + checkpoints + heartbeats
            └─ .agentloom/schedules
```

The presentation layer is a reduced adaptation of OpenCode's MIT-licensed TUI.
Exact provenance, source mapping, commit, and license are preserved in
[`upstream/`](upstream/README.md).

## Development

```bash
bun install --frozen-lockfile
bun test
bun run typecheck
bun run build
```

For an uninstalled source checkout, `./bin/agentloom --project ..` remains
available to TUI contributors.

Python bridge tests run from the AgentLoom project root:

```bash
uv run pytest tests/tui_bridge_test -q
```
