<div align="center"><sub>
English | <a href="docs/cn/README.md">简体中文</a>
</sub></div>

<h1 align="center">AgentLoom</h1>

<p align="center">
  <strong>Create, run, and inspect YAML-defined multi-agent applications.</strong>
</p>

<p align="center">
  Use the terminal Builder to design Agent systems through conversation, then run them with AgentLoom's Python runtime and inspect every Agent, Worker, and Run from one project view.
</p>

<p align="center">
  <a href="https://github.com/linora-u/AgentLoom/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/linora-u/AgentLoom/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://www.python.org/downloads/"><img alt="python >=3.12" src="https://img.shields.io/badge/python-%3E%3D3.12-3776AB?logo=python&logoColor=white"></a>
  <a href="https://github.com/linora-u/AgentLoom/releases/tag/v1.0.1"><img alt="release v1.0.1" src="https://img.shields.io/badge/release-v1.0.1-007EC6"></a>
</p>

<p align="center">
  <img alt="AgentLoom application flow" src="docs/assets/agentloom-application-flow.svg">
</p>

## Install

The source installer is the recommended starting point. It prepares the TUI, the Python runtime, and their dependencies together, so you do not need to build environments by hand.

```bash
git clone https://github.com/linora-u/AgentLoom.git
cd AgentLoom
./install
```

Open a new terminal after installation, then verify the command from the repository root:

```bash
agentloom --version
agentloom --snapshot
```

The installer:

- installs missing `uv` and Bun with their official installers;
- creates a locked Python environment under `~/.agentloom/venv`;
- builds the current-platform TypeScript/OpenTUI binary;
- installs `agentloom` and `agentloom-tui` under `~/.agentloom/bin`;
- adds that directory to your shell `PATH` when a writable shell config exists.

The source installer currently targets macOS and Linux shells. It requires Git and Bash; `curl` is required only when `uv` or Bun must be installed.

Use a custom install location or leave `PATH` unchanged when needed:

```bash
AGENTLOOM_INSTALL_DIR="$HOME/tools/agentloom" ./install
./install --no-modify-path
```

### Configure a model

Copy the model template before using TUI chat or running an Agent:

```bash
cp config/llm.example.yaml config/llm.yaml
```

Edit `config/llm.yaml` and replace the placeholders. Do not commit this file; it is ignored because it contains credentials.

```yaml
model:
  default_model_type: powerful
  powerful:
    model: "openai/<model-id>"
    api_key: "<api-key>"
    base_url: "https://<openai-compatible-endpoint>"  # optional for OpenAI
    tool_choice: "auto"
```

Agent workloads use AgentLoom's normal model runtime. TUI chat currently requires an OpenAI-compatible Chat Completions endpoint; YAML creation also requires streaming and native tool/function calling.

## Start with the TUI

Run `agentloom` inside the project you want to inspect, or pass the project explicitly:

```bash
cd /path/to/AgentLoom
agentloom

# Inspect another AgentLoom project
agentloom --project /path/to/project
```

The TUI is an Agent Builder and project control surface. It can discuss your design, inspect existing definitions, stage and validate Agent YAML, and read canonical runtime status.

It does not execute Shell commands, Git operations, or Agent workloads. You decide when YAML is written and run the Agent in a separate terminal.

### 1. Describe the Agent system

Press `Enter` to send a request. A useful first prompt states the application, roles, inputs, outputs, and acceptance criteria:

```text
Create an application named release_review.
Use one Supervisor and two Workers for API review and test review.
Choose model types from config/llm.yaml.
Show the proposed files and contracts before writing anything.
```

The Builder reads only the project context it needs, stages a draft in memory, and validates the proposed YAML. Continue chatting until the topology and contracts are correct.

### 2. Review and write the YAML

Normal chat never changes files. Enter `/apply` to write the current validated draft. The command writes YAML only; it does not start an Agent.

Use `/models` for the model picker or `/model <type>` to switch directly:

```text
/models
/model summary
/apply
```

### 3. Run the Agent

Open another terminal and run the Supervisor path shown by the Builder:

```bash
uv run loom run applications/<app>/workflows/<agent>.yaml
```

You can also generate a Python entrypoint:

```bash
uv run loom create applications/<app>/workflows/<agent>.yaml \
  -o applications/<app>/<app>_app.py
uv run python applications/<app>/<app>_app.py
```

Keep the TUI open while the Agent runs. `loom run` and generated entrypoints publish the canonical lifecycle that powers project-wide status.

Calling a low-level Agent `.run()` directly bypasses that lifecycle, so no UI can reconstruct a complete Run from it.

### 4. Inspect Agents and Runs

Press `Ctrl+P` or `Tab` to search Applications, Agents, Workers, Runs, Skills, Schedules, and commands. Select entries with the keyboard or mouse.

The detail view distinguishes never-run, running, completed, failed, crashed, interrupted, and unknown states. It shows topology, validation, Worker status, results, and artifact locations without dumping entire logs.

For a failed Run, press `a` to ask the TUI assistant to analyze a sanitized and bounded failure context. The full log remains on disk and the detail view shows its path.

Use `F6` to switch focus. A focused detail view supports arrow keys, `PgUp` / `PgDn`, `Home` / `End`, and the mouse wheel. Press `Esc` or `b` to return.

| Action | Command / key |
|---|---|
| Send chat | `Enter` |
| Browse the project | `Ctrl+P` or `Tab` |
| Select a model | `/models` or `/model <type>` |
| Write the validated draft | `/apply` |
| Refresh the full index | `/refresh` or `r` in details |
| Switch chat/details focus | `F6` |
| Analyze the selected failed Run | `a` |

See [agentloom-tui/README.md](agentloom-tui/README.md) for TUI architecture and contributor details.

### 5. Run schedules explicitly

The TUI can create and manage durable schedules. Automatic firing is a separate foreground service, so closing the TUI never leaves a hidden daemon behind.

```bash
agentloom schedules --project /path/to/project serve
```

## Run an existing Agent

To verify the framework without creating a new application, run the included code review example:

```bash
uv run loom run applications/ai_quality_analysis/workflows/code_review_agent.yaml
```

One run creates a receipt under `.agentloom/runs/<application_id>/<run_id>/`. When checkpointing is enabled, resumable task state lives under `.agentloom/checkpoints/<application_id>/<task_id>/`.

`run_id` identifies one attempt and changes after resume. `task_id` identifies the logical task and remains stable.

## How AgentLoom works

AgentLoom treats a multi-agent system as an application, not a loose collection of prompts.

| Concept | Responsibility |
|---|---|
| Supervisor | Decomposes the application task and coordinates Workers and tools. |
| Worker | Owns one specialized role and exposes a typed callable contract. |
| Agent YAML | Defines role, workflow, model type, tools, Skills, Workers, and runtime policy. |
| Python runtime | Handles model routing, generated Worker tools, concurrency, logs, checkpoints, and artifacts. |

This keeps orchestration reusable while leaving deterministic preprocessing, validation, caching, and output writing in normal Python code.

### Core capabilities

| Need | AgentLoom provides |
|---|---|
| Build an application from configuration | Direct YAML execution with `loom run` and optional Python entrypoint generation. |
| Route roles to different models | Per-Agent `model_type` with credentials isolated in `config/llm.yaml`. |
| Call an Agent like a tool | Worker `agent_function_schema` becomes a validated callable function. |
| Process repeated inputs | Fixed or automatic Worker concurrency with `.batch(tasks)`. |
| Extend an Agent | Built-in tools, local Python functions, MCP servers, Claude-style Skills, and explicit Hooks. |
| Recover and inspect work | Run receipts, bounded logs, Shell audit, checkpoint resume, structured events, Web UI, dashboard, and TUI. |

## Create an application manually

An application keeps its Supervisor, Workers, prompts, optional tools, and outputs together:

```text
applications/release_review/
├── workflows/
│   ├── release_review_agent.yaml
│   └── worker_agents/
│       ├── api_reviewer.yaml
│       └── test_reviewer.yaml
├── config/system.yaml          # optional application overlay
├── sysprompt/                  # optional prompt templates
└── README.md
```

A minimal Supervisor references Worker YAML files:

```yaml
name: "release_review"
description: "Review an API release and its test evidence."
model_type: "powerful"
tool_call_type: "tool_call"

worker_agents:
  - path: "applications/release_review/workflows/worker_agents/api_reviewer.yaml"
  - path: "applications/release_review/workflows/worker_agents/test_reviewer.yaml"

workflow: |
  Ask both Workers for evidence, reconcile conflicts, and return one release decision.

tools: []
skills: []
max_steps: 12
```

Each Worker defines the contract that the Supervisor sees:

```yaml
name: "api_reviewer"
description: "Review API compatibility risks."
model_type: "fast"
tool_call_type: "tool_call"

agent_function_schema:
  description: "Review one release request."
  inputs:
    request:
      description: "Release scope and API diff."
      required: true
  output:
    description: "Evidence-backed compatibility findings."

workflow: |
  Review the request, cite evidence, and return prioritized findings.

tools: []
worker_agents: []
skills: []
max_steps: 8
```

Validate the real model types against `config/llm.yaml`, then run the Supervisor:

```bash
uv run loom run applications/release_review/workflows/release_review_agent.yaml
```

For the complete schema, use the [Agent Configuration Reference](docs/en/agent_config.md).

## Create an application with a coding assistant

The repository includes `agentloom-framework-skill/` for Codex, Claude Code, and other Skill-aware coding assistants.

Ask the assistant to read the Skill before creating files:

```text
Read agentloom-framework-skill/SKILL.md first.

Create an AgentLoom application named <app_name> for this goal:
<task, inputs, outputs, and acceptance criteria>

Create one Supervisor and the necessary Workers under applications/<app_name>/.
Use only model types from config/llm.yaml.
Define every Worker input/output with agent_function_schema.
Validate the YAML, run the application, inspect .agentloom evidence,
and report what passed, failed, or remains limited.
```

The Framework Skill is development guidance for coding assistants. It is not a runtime Skill and is not auto-loaded by Agent applications.

## Architecture

<p align="center">
  <img alt="AgentLoom runtime architecture" src="docs/assets/agentloom-runtime-architecture.svg">
</p>

Worker Agents become generated tools. The Supervisor calls them together with normal Python, MCP, file, Shell, search, Git, Codex, or Skill tools.

The runtime owns execution identity and evidence. TUI and other observers read that canonical state instead of guessing status from process output.

## Example applications

| Application | Demonstrates |
|---|---|
| `ai_quality_analysis` | Twelve specialized Workers coordinated into a staged code review. |
| `unit_test_studio` | A strict pytest generation pipeline with a custom Python entrypoint. |
| `repo_map` | Deterministic preprocessing, bottom-up Agent analysis, batching, and progress persistence. |
| `codex_exec_demo` | Local `codex exec` registered as normal Agent tools with fixed arguments. |

```bash
# Repository architecture map
uv run python applications/repo_map/repo_map_app.py /path/to/project \
  --output_dir /tmp/repo-map-output

# Pytest generation for selected functions
uv run python applications/unit_test_studio/studio_runner.py \
  /path/to/project "src/utils.py:parse_config" \
  --output_dir tests/generated

# Local Codex tool example
uv run loom run applications/codex_exec_demo/workflows/use_codex_exec_demo.yaml
```

## CLI reference

| Command | Purpose |
|---|---|
| `uv run loom run <workflow>` | Run an application. |
| `uv run loom run <workflow> --output-format jsonl` | Stream versioned lifecycle events. |
| `uv run loom create <workflow>` | Generate a Python entrypoint. |
| `uv run loom list-tasks` | List resumable tasks. |
| `uv run loom dashboard` | Open the terminal task dashboard. |
| `uv run loom ui` | Open the Web visualization panel. |
| `uv run loom schedules ...` | Manage durable schedules. |
| `uv run loom sessions ...` | Search and maintain execution history. |
| `uv run loom learn review ...` | Trigger Application or Project review. |
| `uv run loom reviews ...` | Inspect, apply, or roll back review decisions. |
| `uv run loom memory ...` | Administer curated memory. |
| `uv run loom feedback submit <run_id> ...` | Attach outcome feedback to a Run. |
| `uv run loom clean-tasks` | Remove retained checkpoint tasks. |
| `uv run loom clean-runtime` | Apply configured Run and artifact retention. |
| `uv run loom migrate-runtime --dry-run\|--apply` | Preview or apply legacy runtime migration. |

Run `uv run loom <command> --help` for the full command contract.

## Documentation

| Document | Covers |
|---|---|
| [Configuration Overview](docs/en/config-overview.md) | Configuration layers, merging, and isolation. |
| [Agent Configuration](docs/en/agent_config.md) | Supervisor and Worker YAML fields. |
| [LLM Configuration](docs/en/llm_config.md) | Model types, providers, retries, and caching. |
| [System Configuration](docs/en/system_config.md) | Runtime, permissions, execution environments, and tools. |
| [Skills Configuration](docs/en/skills_config.md) | Skill packages, loading, and policy. |
| [Hooks Reference](docs/en/hooks.md) | Direct Hooks, Bundles, events, and execution. |
| [Checkpoint Resume](docs/en/checkpoint.md) | Run evidence, checkpoint layout, and recovery. |
| [Self-Learning v6](docs/en/self_learning.md) | History, candidates, review, approval, and promotion. |
| [Structured Run API](docs/en/run_observability.md) | Python receipts, typed errors, event sinks, and JSONL. |

## Development and support

```bash
# Framework tests
uv run pytest tests -q

# TUI tests
cd agentloom-tui
bun test
bun run typecheck
```

- Issues: [github.com/linora-u/AgentLoom/issues](https://github.com/linora-u/AgentLoom/issues)
- Contact: [raine_walker@163.com](mailto:raine_walker@163.com?subject=AgentLoom%20Collaboration)
- TUI upstream provenance and license: [agentloom-tui/upstream/README.md](agentloom-tui/upstream/README.md)

If AgentLoom helps your project, consider starring the repository or contributing a focused example, fix, or documentation improvement.
