<div align="center"><sub>
English | <a href="docs/cn/README.md">简体中文</a>
</sub></div>

<h1 align="center">AgentLoom</h1>

<p align="center">
  <strong>Application-level framework for building multi-agent systems from YAML.</strong>
</p>

<p align="center">
  Build multi-agent apps by writing YAML, route each sub-agent to the right model, load Skills / MCP / tools, run repeated work in parallel, and resume long runs from saved state.
</p>

<p align="center">
  <a href="https://github.com/linora-u/AgentLoom/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/linora-u/AgentLoom/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://www.python.org/downloads/"><img alt="python >=3.12" src="https://img.shields.io/badge/python-%3E%3D3.12-3776AB?logo=python&logoColor=white"></a>
  <a href="https://github.com/linora-u/AgentLoom/releases/tag/v1.0.1"><img alt="release v1.0.1" src="https://img.shields.io/badge/release-v1.0.1-007EC6"></a>
</p>

<p align="center">
  <img alt="AgentLoom application flow" src="docs/assets/agentloom-application-flow.svg">
</p>

---

## 3-Minute Quick Start

AgentLoom is for developers who want ready-to-run agent apps: YAML-defined agents, explicit Worker contracts, model routing, runtime logs, checkpoint state, and optional UI monitoring.

```bash
git clone <repo-url> AgentLoom
cd AgentLoom

uv sync
# If PyPI is slow or unavailable on your network:
# UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple uv sync

cp config/llm.example.yaml config/llm.yaml
# Edit config/llm.yaml and add your model credentials.

uv run loom run applications/ai_quality_analysis/workflows/code_review_agent.yaml
```

After the first run, you should see:

- structured terminal logs with agent names, task IDs, step duration, and token usage;
- one execution attempt under `.agentloom/runs/<application_id>/<run_id>/`, with a manifest, bounded runtime log, Shell audit, and raw artifacts;
- resumable task state under `.agentloom/checkpoints/<application_id>/<task_id>/` when checkpointing is enabled;
- optional visualization through `uv run loom ui`;
- optional terminal monitoring through `uv run loom dashboard`.

`run_id` identifies one execution attempt and changes on resume. `task_id` identifies the logical task and remains stable, so a resumed attempt writes a new run directory while continuing the same checkpoint. The agent-visible `.runtime/` workspace is intentionally separate from framework runtime storage.

## Create a Multi-Agent App with Codex

The fastest path is to let Codex use the framework skill that ships with this repository. Codex can create the YAML files, run the AgentLoom app, inspect `.agentloom/` run and checkpoint evidence, and revise the app when a Worker gets stuck or a config is wrong.

Use a prompt like this:

```text
Read agentloom-framework-skill/SKILL.md first.

Create an AgentLoom application named <app_name> for this goal:
<describe the user-facing task, input, output, and acceptance criteria>

Requirements:
- Create files under applications/<app_name>/.
- Use a Supervisor YAML plus at least two Worker YAML files.
- Each Worker must define agent_function_schema with clear inputs and output.
- Choose model_type values only from config/llm.yaml.
- Add Skills or MCP config only if they are useful for this app.
- Write an application README with run commands, Worker responsibilities, validation records, and known limits.
- Run the app, watch the logs/checkpoints, and fix any YAML or tool issues you find.
```

You can let Codex run and monitor the app for you, or run it yourself:

```bash
uv run loom run applications/<app_name>/workflows/<app_name>_agent.yaml
```

Useful runtime checks while the app is running:

```bash
uv run loom list-tasks
uv run loom dashboard

manifest=$(find .agentloom/runs -name manifest.json -type f -print | sort | tail -1)
run_dir=$(dirname "$manifest")
sed -n '1,160p' "$manifest"
tail -n 80 "$run_dir/logs/runtime.log"
tail -n 80 "$run_dir/audit/shell.jsonl"
```

If you prefer Claude Code, give it the same request: read `agentloom-framework-skill/SKILL.md`, create the app under `applications/<app_name>/`, run it, and summarize what worked or failed.

The main config ideas are simple:

- `model_type`: which configured model category this Agent uses.
- `worker_agents`: which Worker YAML files the Supervisor can call.
- `agent_function_schema`: the input and output contract for a Worker.
- `skills` / `mcp_servers`: optional extra knowledge and external tools.

## Why AgentLoom

Many agent frameworks expose components. AgentLoom gives you a complete app structure.

Complex agent applications often repeat the same setup code: Worker registration, parameter adapters, runtime entrypoints, batch execution, logging, checkpointing, and safety controls. AgentLoom moves those reusable parts into YAML and the runtime. Your application code stays focused on domain work: preprocessing, validation, artifact writing, and the actual Agent roles.

The practical result:

| Need | AgentLoom approach |
|---|---|
| Build a full multi-agent app | Define a Supervisor, Workers, tools, Skills, and runtime behavior in YAML. |
| Route roles to different models | Set `model_type` per Agent and keep credentials isolated in `config/llm.yaml`. |
| Turn sub-agents into tools | Give Workers `agent_function_schema`; the Supervisor calls them like normal tools. |
| Reuse coding-assistant knowledge | Load Claude-style `SKILL.md` packages on demand or eagerly. |
| Connect external tools | Register local Python tools, local `codex exec`, and MCP servers through `mcp_servers`. |
| Handle repeated work | Use Worker `concurrency` and `tool.batch(tasks)` for independent repeated inputs. |
| Understand long runs | Read the run manifest, bounded runtime log, Shell audit, task checkpoint, `loom ui`, and `loom dashboard`. |

## Architecture

<p align="center">
  <img alt="AgentLoom runtime architecture" src="docs/assets/agentloom-runtime-architecture.svg">
</p>

The important boundary is:

- Worker Agents define their role, tools, inputs, and output contract.
- AgentLoom turns Workers into callable tools with generated function signatures.
- The Supervisor coordinates Workers and normal tools to complete the application task.
- Python remains available for deterministic preprocessing, validation, caching, and artifact writing.

## Capabilities

| Capability | What is already implemented |
|---|---|
| Application-first YAML | `loom run` executes an Agent YAML directly; `loom create` generates a Python entry script; `run_app()` embeds the app in Python. |
| Per-Agent model routing | Each Agent selects a `model_type`; the actual provider, key, endpoint, retries, and parameters live in `config/llm.yaml`. |
| Agent-as-Tool coordination | Worker Agents export as callable tools through `agent_function_schema`, including required-input validation and string outputs. |
| Skills, MCP, and tools | Agents can load `SKILL.md` packages, local Python functions, built-in file/shell/search/git tools, local Codex tools, and MCP client tools through `mcp_servers`. |
| Concurrent repeated work | Worker `concurrency: auto` or fixed concurrency works with `.batch(tasks)` for many independent inputs. |
| State and observability | Rich terminal logs, bounded per-run file logs, per-step timing, token usage, run manifests, checkpoint resume, Web UI, and TUI dashboard. |

## Example Applications

The `applications/` directory contains working apps that show the intended usage patterns.

| Application | What it builds | What it demonstrates |
|---|---|---|
| `ai_quality_analysis` | Multi-dimensional code review application. | 12 Worker Agents, staged review, direct `loom run`, long-running codebase analysis. |
| `unit_test_studio` | Python pytest generation workflow. | Strict multi-step pipeline, custom entry script, function intake, scenario planning, test generation, refinement, delivery report. |
| `repo_map` | Repository architecture map generator. | Deterministic Python preprocessing plus Agent analysis, bottom-up directory processing, `tool.batch(tasks)`, progress persistence. |
| `codex_exec_demo` | Local Codex Exec tool-call example. | Direct `loom run`, normal function tool registration, `fixed_args` for Codex parameters, structured `tool_call` sequencing. |

Run the default code review example:

```bash
uv run loom run applications/ai_quality_analysis/workflows/code_review_agent.yaml
```

Run Repo Map with a custom Python entrypoint:

```bash
uv run python applications/repo_map/repo_map_app.py /path/to/project \
  --output_dir /tmp/repo-map-output \
  --exclude_dirs vendor \
  --exclude_dirs build
```

Generate pytest tests for selected functions:

```bash
uv run python applications/unit_test_studio/studio_runner.py \
  /path/to/your/project \
  "src/utils.py:parse_config,src/core.py:run_pipeline" \
  --output_dir tests/generated
```

Run the local Codex Exec tool example:

```bash
uv run loom run applications/codex_exec_demo/workflows/use_codex_exec_demo.yaml
```

## Core Concepts

### Supervisor / Worker

A Supervisor Agent coordinates the task. Worker Agents specialize in one part of the job. In multi-agent apps, the Supervisor references Workers through `worker_agents`, and Workers expose callable contracts through `agent_function_schema`.

### Agent as Tool

Worker Agents can be exported as callable tools. AgentLoom generates function signatures, validates required inputs, builds the task payload, and returns a string result. The same Worker can expose `.batch(tasks)` for parallel execution.

### Skills and Hooks

Skills provide reusable knowledge or behavior through Claude-style `SKILL.md` packages. Hooks attach logic around task lifecycle, sub-agent lifecycle, tool calls, sessions, compaction, setup, and config changes.

### Local Codex Tool

AgentLoom includes `src.tools.codex.codex_tool.codex`, which exposes local `codex exec` as a normal function tool. Register it in Agent YAML with `module/function`. Use `fixed_args` to lock inputs such as `prompt`, `cwd`, `sandbox`, and `search`; fixed inputs are removed from the LLM-visible tool schema.

Before running the tool, make sure `codex` is on `PATH` and `codex login status` succeeds.

## CLI Reference

| Command | Purpose |
|---|---|
| `uv run loom run <workflow>` | Run an AgentLoom application. |
| `uv run loom create <workflow>` | Generate a Python entry script for an application. |
| `uv run loom ui` | Open the Web visualization panel. |
| `uv run loom dashboard` | Open the terminal task dashboard. |
| `uv run loom list-tasks` | List resumable checkpoint tasks. |
| `uv run loom clean-tasks` | Clean old checkpoint data. |
| `uv run loom clean-runtime` | Apply configured retention to completed run directories and raw artifacts. |
| `uv run loom migrate-runtime --dry-run` | Preview valid legacy checkpoint candidates without changing disk state. |
| `uv run loom migrate-runtime --apply` | Migrate valid legacy checkpoints and archive the old `.logs` tree. |

## Documentation

| Document | Description |
|---|---|
| [Configuration Overview](docs/en/config-overview.md) | Configuration layers, merge rules, and model configuration isolation. |
| [Agent Configuration](docs/en/agent_config.md) | Supervisor/Worker fields, tools, model selection, workflows, and skills. |
| [LLM Configuration](docs/en/llm_config.md) | Model types, provider settings, inheritance, retries, and prompt cache settings. |
| [System Configuration](docs/en/system_config.md) | Runtime settings, permissions, logging, execution environments, and tools. |
| [Skills Configuration](docs/en/skills_config.md) | Skill package format, loading, runtime policy, and built-in skills. |
| [Hooks Reference](docs/en/hooks.md) | Lifecycle events, hook types, matching, and execution behavior. |
| [Checkpoint Resume](docs/en/checkpoint.md) | Checkpoint layout, resume behavior, and long-task recovery. |

## AgentLoom Framework Skill

The repository includes one root-level Agent Skill for AI coding assistants: `agentloom-framework-skill/`.

Use it when you want Codex, Claude Code, or another coding assistant to develop with AgentLoom instead of guessing the required files and fields from scratch. Ask the assistant to read `agentloom-framework-skill/SKILL.md`, then describe the application capability you want to build.

This Skill is a development aid for coding assistants. It is not stored under `skills/`, so AgentLoom runtime applications do not auto-load it as a normal runtime Skill.

## Support and Contributing

AgentLoom is built as a practical framework for complex agent automation. Issues, feature ideas, documentation improvements, and example applications are welcome.

- Open an issue: [GitHub Issues](https://github.com/linora-u/AgentLoom/issues)
- Contact: [raine_walker@163.com](mailto:raine_walker@163.com?subject=AgentLoom%20Collaboration)
- If the project helps you, a GitHub star makes it easier for others to find.
