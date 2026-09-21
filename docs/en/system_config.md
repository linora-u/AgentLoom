# AgentLoom System Global Configuration (`system.yaml`) Complete Reference

> **Document scope**: This document details **every** configuration parameter in `config/system.yaml`.
> For override relationships between configuration files, see [Configuration System Overview](config-overview.md).
> For LLM model parameters, see [LLM Configuration Reference](llm_config.md).
> For Agent YAML parameters, see [Agent Configuration Reference](agent_config.md).
> Goal Mode is not global configuration and can be enabled only in a top-level
> Supervisor Agent YAML; see [Goal Mode](goal_mode.md).

`config/system.yaml` is the AgentLoom framework's **core global configuration file**, controlling system metadata, context compression strategy, top-level prompts, global Skills, logging, tool system, tool access control, and more.

> ⚠️ **Isolation between system.yaml and llm.yaml**: All LLM-related configuration (`model`, `llm`, `langfuse`) **must and can only** be placed in `config/llm.yaml`. If these keys are written in `system.yaml`, the framework will automatically filter them during loading and output a warning log. See [LLM Configuration Reference](llm_config.md) for details.

The configuration loading order is `config/system.yaml` → `config/llm.yaml` → `applications/<app>/config/system.yaml` (optional). Application-level `system.yaml` discovery is based on the Agent YAML configuration file path: the framework searches upward from the file's directory for a `workflows/` directory, whose parent directory is the application root (app_root). If `app_root/config/system.yaml` exists, it is automatically overlaid; if not, it is skipped. Nested applications (e.g., `my_app/sub_module`) will hit the nearest `workflows/` directory, providing natural isolation.

---

## Table of Contents

- [Quick Reference: Representative YAML Structure](#quick-reference-representative-yaml-structure)
- [1. system — System Metadata](#1-system--system-metadata)
- [1.5 model_request_headers — Model Request Header Privacy](#15-model_request_headers--model-request-header-privacy)
- [2. runtime_options — Backend Options](#2-runtime_options--backend-options)
- [3. Historical Execution Fields](#3-historical-execution-fields)
- [4. skills — Global Skills Configuration](#4-skills--global-skills-configuration)
- [4.5 hooks — Independent Hook Runtime](#45-hooks--independent-hook-runtime)
- [5. lsp_servers — LSP Language Server Configuration](#5-lsp_servers--lsp-language-server-configuration)
- [6. shell_tool — Shell Execution Model](#6-shell_tool--shell-execution-model)
- [7. runtime and logging — Runtime Storage and Logging](#7-runtime-and-logging--runtime-storage-and-logging)
- [8. tools — Tool System Configuration](#8-tools--tool-system-configuration)
- [9. tool_access_control — Tool Access Control](#9-tool_access_control--tool-access-control)
- [10. tool_metadata — Tool Metadata Configuration](#10-tool_metadata--tool-metadata-configuration)
- [11. tool_output_limits — Tool Output Limits](#11-tool_output_limits--tool-output-limits)
- [12. checkpoint — Checkpoint, Resume & Heartbeat](#12-checkpoint--checkpoint-resume--heartbeat)
- [13. self_learning — History, Review, and Curated Memory](#13-self_learning--history-review-and-curated-memory)
- [Appendix A: Pydantic Model Reference Table](#appendix-a-pydantic-model-reference-table)
- [Appendix B: Application-Level Override and Directory Structure](#appendix-b-application-level-override-and-directory-structure)

---

## Quick Reference: Representative YAML Structure

The following shows the **major public sections and representative repository values** for `config/system.yaml` (with framework fallback defaults annotated at key points). Consult the detailed sections below and the checked-in file for less common extension mappings.

```yaml
# ============================================
# System Metadata
# ============================================
system:
  name: "AgentLoom"
  version: "1.0.1"
  user_agent: "AgentLoom/1.0.1"

# ============================================
# Model Request Header Privacy
# ============================================
model_request_headers:
  profile: "opencode"  # agentloom | none | kimicode | openclaw | opencode
  headers: {}

# ============================================
# Runtime Storage and Retention
# ============================================
runtime:
  root_dir: ".agentloom"
  successful_run_retention_days: 7
  failed_run_retention_days: 30
  artifact_retention_days: 3
  cleanup_interval_hours: 24

# ============================================
# Logging Configuration
# ============================================
logging:
  level: "INFO"
  console_enabled: true
  file_enabled: true
  max_file_bytes: 26214400
  backup_count: 3

# ============================================
# Default Toolsets
# ============================================
default_toolsets:
  - "core_shell"
  - "core_file"
  - "core_search"
  - "context"
  - "skills"

# ============================================
# Shell Settings
# ============================================
shell_settings:
  allowed_commands: "*"
  allowed_operators: "*"

# ============================================
# Tool Access Control Configuration (optional, Pydantic defaults below apply even if omitted)
# ============================================
tool_access_control:
  path_validation: []

# ============================================
# Checkpoint / Resume / Heartbeat (optional, framework defaults below apply if omitted)
# ============================================
checkpoint:
  enabled: true            # Enable/disable checkpoint & resume (global switch)
  cleanup_on_success: true # Auto-delete checkpoint directory after successful completion
  max_resume_age: 604800   # Max checkpoint retention in seconds (default 7 days); expired = non-resumable
  heartbeat_interval: 5    # Heartbeat write interval (seconds) for crash detection

# ============================================
# Self-Learning v6
# ============================================
self_learning:
  enabled: true
  events_retention_days: 90
  memory:
    prompt_max_chars: 12000
    max_item_chars: 4000
    scope_budgets: {project: 8000, application: 6000}
  review:
    enabled: true
    application:
      review_model: summary
      trigger: {mode: batch, min_completed_runs: 5}
      approval: {fact: auto, experience: manual}
    project:
      review_model: summary
      trigger: {mode: batch, min_candidates: 5}
      approval: {fact: manual, experience: manual}
    artifacts: {markdown: true, review_auto_applied: true}

```

---

## 1. system — System Metadata

Controls basic system identity, used for logging and, when `model_request_headers.profile: "agentloom"` is selected, model request `User-Agent` construction.

**YAML path**: `system.*`
**Pydantic model**: `SystemSettings`

| Parameter | Type | Default | Required | Description |
|------|------|--------|------|------|
| `system.name` | `str` | `"AgentLoom"` | ❌ No | System name, used for log identification and User-Agent construction |
| `system.version` | `str` | `"1.0.1"` | ❌ No | System version number (informational field) |
| `system.user_agent` | `str` | `"AgentLoom/1.0.1"` | ❌ No | AgentLoom identity string used by the `agentloom` request-header profile |

**Example**:

```yaml
system:
  name: "my-project-agents"
  version: "2.0.0"
  user_agent: "my-project-agents/2.0.0"
```

---

## 1.5 model_request_headers — Model Request Header Privacy

Configures default HTTP headers for outbound model API requests so AgentLoom's default identity is not exposed directly to model providers. Put global privacy defaults here; keep provider/model-specific overrides in `config/llm.yaml` `model.<type>.extra_headers`.

The recommended repository default is `opencode`, which has been validated against the current OpenAI-compatible `llm.yaml` endpoint with the real OpenCode CLI:

```yaml
model_request_headers:
  profile: "opencode"
  headers: {}
```

Built-in profiles:

| profile | Status | Description |
|------|------|------|
| `opencode` | Verified with real OpenCode | Suitable for the current OpenAI-compatible `llm.yaml`; sends OpenCode's current `User-Agent` and session headers |
| `cline` | Verified with real Cline CLI | Suitable for the current OpenAI-compatible `llm.yaml`; sends Cline CLI's current OpenAI-compatible runtime `User-Agent` |
| `kimicode` | Verified with real Kimi Code | Suitable for the current OpenAI-compatible `llm.yaml`; sends Kimi Code's current `User-Agent` and JS SDK headers |
| `openclaw` | Verified with real OpenClaw | Suitable for the current OpenAI-compatible `llm.yaml`; sends OpenClaw's current direct-runtime OpenAI JS SDK headers |
| `roo` | Verified with Roo Code OpenAI provider source | Suitable for the current OpenAI-compatible `llm.yaml`; sends Roo Code's current default `HTTP-Referer`, `X-Title`, and `User-Agent` |
| `agentloom` | Explicit AgentLoom identity | Uses `system.user_agent` |
| `none` | No system default identity header | Leaves only SDK headers and explicitly configured headers |

Claude Code currently uses an Anthropic-compatible plan/coding protocol, so it
cannot be proven endpoint-equivalent with this repository's current
OpenAI-compatible `/api/v3` + `ep-...` config and is not exposed as a built-in
profile. Define a custom `model_request_headers.profiles` entry if you need to
experiment with Claude Code-style headers.

The public npm registry does not currently provide a directly runnable official
Roo CLI, and the repository CLI does not expose an OpenAI-compatible base URL
option. The `roo` validation boundary is therefore a real request through Roo
Code `3.53.0` `OpenAiHandler` provider source, not a full VS Code extension-host
validation.

Custom profile example:

```yaml
model_request_headers:
  profile: "codex"
  profiles:
    codex:
      headers:
        User-Agent: "configured-agent/1.0"
        X-Client-Profile: "codex"
```

Merge order:

1. Built-in profile, or `model_request_headers.profiles.<name>`
2. `model_request_headers.headers`
3. `config/llm.yaml` `model.<type>.extra_headers`

Later layers override earlier layers case-insensitively. This feature only controls AgentLoom-managed HTTP headers; it does not guarantee TLS fingerprints, body schemas, or header ordering are identical to a real client. Proving exact parity with a real client version requires capturing that client against the same endpoint and diffing the requests. The current validation boundary is recorded in the Chinese [model request header parity note](../cn/model_request_header_tool_parity.md).

| Parameter | Type | Default | Required | Description |
|------|------|--------|------|------|
| `model_request_headers.profile` | `str` | `"agentloom"` | ❌ No | Selects a built-in or custom request-header profile; the repository example uses `opencode` |
| `model_request_headers.profiles` | `dict` | `{}` | ❌ No | Local custom or overriding named request-header profiles |
| `model_request_headers.headers` | `dict` | `{}` | ❌ No | System-level extra headers sent with every model request |

---

## 2. runtime_options — Backend Options

Only `runtime_options` is interpreted for backend settings. Planning, summary, Todo and templates belong to the selected runtime. Configure them on each Agent in mixed applications so smol options do not become Pi requirements.

```yaml
# smol Agent YAML
agent_runtime: smolagents
runtime_options:
  smart_summary: false
  todo_mode: "off"
```

Options merge through system, Application and Agent layers, with validation owned by the selected adapter. See [Agent configuration](agent_config.md) for smol fields.

## 3. Historical Execution Fields

Historical top-level `smart_summary`, `prompt`, `todo`, `max_steps`, `planning_interval`, and `max_consecutive_parse_errors` are silently ignored, without conversion or rejection. They no longer provide system defaults.

Set a custom smol template with a string `runtime_options.prompt_template_path` in Agent YAML. Bundled reference templates live under `src/runtimes/smolagents/prompts/`.

---

## 4. skills — Global Skills Configuration

Project `skills/` and Application `skills/` directories are discovered
automatically. This setting adds project-relative discovery roots:

**YAML path**: `skills` (top-level field)
**Type**: `{paths: list[str]}`

```yaml
skills:
  paths:
    - shared/skills
```

The model sees only the permitted Skill catalogue until it calls `skill(name)`.
There is no loading-mode setting. Skill activation never grants execution
authority. Remove the `skills` toolset from an Agent's `toolsets` to hide both
the tool and catalogue. See [Skills](skills_config.md).

## 4.5 hooks — Independent Hook Runtime

Hooks are configured independently from Skills. For example, this explicitly
authorizes a local audit Bundle:

```yaml
hooks:
  bundles:
    local-audit:
      path: hooks/local-audit
```

See the [Hooks reference](hooks.md) for direct declarations, Bundle manifests,
layer replacement, tombstones, event semantics, and the Shell protocol.

---

## 5. lsp_servers — LSP Language Server Configuration

Configures LSP language servers that are pre-warmed at agent startup and remain alive for the session. Provides code intelligence features (go-to-definition, find references, document symbols, hover type info).

After `uv sync`, all required binaries are automatically available:
- **Python**: `jedi-language-server` (pip dependency, in `.venv/bin/`)
- **Go**: `go` binary (via `go-bin` PyPI package), `gopls` via `go install`
- **TypeScript**: `node` + `npm` (via `nodejs-bin` PyPI package), `typescript-language-server` auto-installed
- **Rust/Java/C#/Kotlin**: auto-downloaded by the LSP backend

```yaml
lsp_servers:
  enabled: true
  max_restarts: 3
  servers:
    - python
    - go
    - typescript
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `true` | Enable/disable LSP services |
| `max_restarts` | `int` | `3` | Max crash recovery attempts per server |
| `servers` | `list` | `[python]` | Languages to start (40+ supported) |

> Servers are managed by `agentloom.integrations.lsp.lsp_server_manager.LSPServerManager`. Unsupported languages automatically fall back to tree-sitter AST analysis (46+ languages).

---

## 6. shell_tool — Shell Execution Model

Agents execute shell commands only through structured `shell_tool` calls. Commands run in controlled local subprocesses; permissions, path rules, and command allow-lists are governed by `tool_access_control` and `shell_settings`. The framework has no separate mode for model-generated Python execution.

### 6.1 Shell Path Auto-Detection

Shell path is determined automatically via a smart detection chain. No manual configuration is needed. Only **bash** and **zsh** are supported (other shells like sh, fish, csh are excluded due to syntax incompatibilities).

Detection chain:

| Priority | Source | Description |
|--------|------|------|
| 1️⃣ | Environment variable `$SHELL` (Unix) / `$COMSPEC` (Windows) | Used **only when** the path points to bash/zsh **and** passes executability validation; otherwise skipped |
| 2️⃣ | `shutil.which("zsh")` / `shutil.which("bash")` | Auto-search PATH, order adjusted based on `$SHELL` preference |
| 3️⃣ | Hardcoded path scan | `/bin`, `/usr/bin`, `/usr/local/bin`, `/opt/homebrew/bin` × [bash, zsh] |
| 4️⃣ | All failed | Raises `FileNotFoundError` with a clear message |

**Executability validation (two-tier check)**:
- **Tier 1**: `os.access(path, os.X_OK)` — fast permission-bit check
- **Tier 2** (fallback): Actually execute `<shell> --version` — compatible with Nix and other environments where permission bits are unreliable

**Preference ordering**: If `$SHELL` contains `bash`, bash is preferred over zsh; otherwise zsh is preferred.

**Subprocess environment safety**: All shell subprocesses automatically filter sensitive environment variables (API keys, cloud credentials, CI tokens) and inject protective variables (`GIT_EDITOR=true` to prevent interactive editors). Supports both exact-match and prefix-match filtering strategies.

**Execution architecture**: Uses a stateless subprocess model — each command spawns an independent `subprocess.Popen`, with context continuity maintained through environment snapshots and session state files. This avoids PTY long-connection fragility issues (buffer overflows, interactive command hangs, etc.).

**Environment snapshot**: At agent session init, the user's shell environment (functions, aliases, shell options, PATH) is captured and saved as `snapshot.sh`. Each command execution sources this snapshot to restore shell configuration without carrying over command-side `export` changes. The snapshot also injects extglob protection (`shopt -u extglob` / `setopt NO_EXTENDED_GLOB`) to prevent TOCTOU attacks.

**CWD tracking**: Uses out-of-band file tracking (`pwd -P >| cwd_file`) — no control characters embedded in stdout. After each command, the tracking file is read to update the working directory state.

**Ephemeral environment variables**: `export` statements are visible inside the same shell command, for example `export X=1 && echo $X`, but do not persist to later `shell_tool` calls. Keep assignment and use in one command when command-local environment is needed.

**Process tree management**: Subprocesses use `start_new_session=True` to create independent process groups. On timeout or error, `os.killpg()` sends SIGTERM → SIGKILL to clean up the entire process tree, preventing orphan processes.

**Size watchdog**: Background monitoring of output file size; automatically sends SIGKILL if it exceeds 100MB, preventing disk exhaustion.

**Background task management**: Commands that exceed their timeout are automatically promoted to background tasks instead of being killed. Agents can use `check_background_task(task_id)` to inspect status and recent output, `kill_background_task(task_id)` to terminate, and `list_background_tasks()` to list all background tasks. Commands can also be started directly in background via `shell_tool(command, run_in_background=True)`. Background tasks are managed by the `BackgroundTaskRegistry` singleton with configurable max concurrency (default 10).

**Stall detection**: A stall watchdog polls the output file of each background task every 5 seconds. If output stops growing for 45 seconds and the last line matches an interactive-prompt pattern (e.g. `(y/n)`, `Continue?`, `Press Enter`), the task is flagged as stalled and the agent is notified.

**Pipe redirect normalization**: For commands containing pipes (`|`), `< /dev/null` is automatically moved to apply to the first command in the pipeline (e.g. `rg foo | wc -l` → `rg foo < /dev/null | wc -l`), preventing tools like `rg` from hanging while waiting for stdin. Complex syntax (`$()`, backticks, control structures) is conservatively skipped.

**Ephemeral environment variables**: `export` statements are ephemeral — they only take effect within the current command and do NOT persist across separate tool calls. PATH is preserved via the snapshot mechanism captured at session init.

**Foreground stall detection and auto-kill**: During foreground command execution, the main thread uses a 1-second polling loop (instead of a blocking wait). The StallWatchdog monitors the output file concurrently. Once it detects a stall caused by an interactive prompt (45 seconds of no output growth + last line matches a prompt pattern), the main thread discovers this within 1 second and automatically terminates the process, returning a clear stall warning. Partial output (content produced before the stall) is preserved in the result. This prevents commands from hanging for the full 120-second timeout when waiting for user input that will never arrive.

Background tasks configuration:
```yaml
shell_settings:
  background_tasks:
    enabled: true                  # Enable background tasks
    max_concurrent: 10             # Max concurrent background tasks
    auto_background_on_timeout: true  # Auto-promote on timeout
    max_output_bytes: 104857600    # Max output per task (100MB)
    stall_detection: true          # Enable stall detection
    stall_threshold_seconds: 45    # Stall detection threshold (seconds)
```

> Detection results are cached for the process lifetime and will not re-detect on each command.

## 7. runtime and logging — Runtime Storage and Logging

`runtime` defines the single framework-owned storage root and bounded retention. `logging` controls the console and the run-scoped file backend. Logger state is bound to the current run; it is not shared as a process-global output path.

Both sections are global-only. Application-level `config/system.yaml` and Agent YAML containing either section are rejected, so a typo or misplaced override cannot silently fragment—or pretend to move—task discovery across runtime roots.

An isolated subprocess or validation harness may set `AGENTLOOM_RUNTIME_ROOT`. It overrides the complete canonical runtime home—runs, checkpoints, sessions, learning, and `self_learning.db` move together; there is no self-learning-only root override.

**YAML paths**: `runtime.*`, `logging.*`

| Parameter | Type | Default | Required | Description |
|------|------|--------|------|------|
| `runtime.root_dir` | `str` | `".agentloom"` | ❌ No | Only root for framework runtime state. Relative paths resolve from the project root; absolute paths are accepted |
| `runtime.successful_run_retention_days` | `int` | `7` | ❌ No | Retention for completed run directories |
| `runtime.failed_run_retention_days` | `int` | `30` | ❌ No | Retention for failed/interrupted run directories |
| `runtime.artifact_retention_days` | `int` | `3` | ❌ No | Retention for raw `artifacts/` inside a retained run |
| `runtime.cleanup_interval_hours` | `int` | `24` | ❌ No | Minimum interval between automatic cleanup attempts |
| `logging.level` | `str` \| `int` | `"INFO"` | ❌ No | Log filtering level (case-insensitive) |
| `logging.console_enabled` | `bool` | `true` | ❌ No | Write formatted runtime messages to the console |
| `logging.file_enabled` | `bool` | `true` | ❌ No | Write the current attempt to `logs/runtime.log` |
| `logging.max_file_bytes` | `int` | `26214400` | ❌ No | Maximum bytes per runtime log segment (25 MiB) |
| `logging.backup_count` | `int` | `3` | ❌ No | Number of rotated runtime log segments |

### 7.1 Canonical paths and lifecycle

Each attempt writes under `.agentloom/runs/<application_id>/<run_id>/`:

```text
manifest.json
logs/runtime.log[.1-.3]
audit/shell.jsonl[.1-.2]
audit/task_tree.json
audit/task_events.jsonl
artifacts/result.txt
artifacts/{shell,background,skills}/
```

The task tree, task events, and result files are written when the corresponding evidence exists. Their paths are recorded in `manifest.json` before successful checkpoint cleanup, so Run inspection does not depend on a live checkpoint. The Shell audit uses its own fixed 10 MiB segments and two backups. A resumed task receives a new `run_id` and run directory while keeping its original `task_id` and `.agentloom/checkpoints/<application_id>/<task_id>/` state. File logging can be disabled without disabling the checkpoint or Shell audit:

```bash
loom run applications/<app>/workflows/<agent>.yaml --no-file-log
```

There is no `--log-to-file`, `logging.enabled`, `logging.dir`, or `logging.file_path` compatibility path.

### 7.2 Retention and storage boundaries

Automatic cleanup runs at most once per configured interval. `loom clean-runtime` applies the policy explicitly. It only deletes eligible run directories or their raw artifacts; it never traverses checkpoints, workspaces, self-learning state, or Application-owned output directories.

Persistent Agent recall uses `.agentloom/workspaces/agents/<application_id>/<agent_path>/`. Current-task Todo state uses `todos.json` inside the canonical checkpoint. Durable schedules and scheduler service state use `.agentloom/schedules/`. Every framework-owned runtime consumer follows the same `runtime.root_dir`; Application-owned output directories remain separate.

To verify a real attempt, read `manifest.json` and its referenced logs, audits, and artifacts; an exit code alone is not sufficient.

---

## 8. tools — Tool System Configuration

Controls the tool list available to Agents at initialization and the security policy for Shell command execution.

**YAML path**: `tools.*`

### 8.1 default_toolsets — Default Toolsets

| Parameter | Type | Default | Required | Description |
|------|------|--------|------|------|
| `default_toolsets` | `list[str]` | `[]` | ❌ No | Toolset list loaded by default for all Agents at startup |

Agent YAML `toolsets:` replaces this global list entirely. `toolsets: []` means no built-in tools. Complete catalog toolsets:

| Toolset | Tools |
|--------|-------|
| `core_shell` | `shell_tool`, `check_background_task`, `kill_background_task`, `list_background_tasks` |
| `core_file` | `read_file`, `edit_file`, `write_file`, `list_directory` |
| `core_search` | `grep_search`, `glob_search` |
| `context` | `loom_retrieve_context` |
| `skills` | `skill` |
| `self_learning` | `session_search`, `session_scroll`, `memory`, `skill_manage` |
| `planning` | `todo_write` |
| `markdown_report` | `write_markdown_file`, `write_markdown_file_raw`, `append_markdown_sections` |
| `code_nav` | `get_file_outline`, `ast_grep_search_file`, `lsp_find_definition`, `lsp_find_references`, `lsp_get_document_symbols`, `lsp_hover`, `lsp_get_workspace_symbols` |

Complete predefined tool list:

| Tool Name | Function |
|--------|------|
| `write_file` | Create new file or overwrite existing |
| `read_file` | Read file content (supports offset/limit for ranges) |
| `edit_file` | Apply one or more unique text edits |
| `get_file_outline` | Get code outline (functions/classes/structs) |
| `list_directory` | List directory structure |
| `grep_search` | Regex search file contents |
| `glob_search` | Find files by glob pattern |
| `ast_grep_search_file` | AST pattern search |
| `lsp_find_definition` | Find symbol definition |
| `lsp_find_references` | Find symbol references |
| `lsp_get_document_symbols` | List document symbols |
| `lsp_hover` | Show hover/type information |
| `lsp_get_workspace_symbols` | Search workspace symbols |
| `loom_retrieve_context` | Retrieve compressed context refs |
| `skill` | Load one selected Skill into the conversation |
| `session_search` | Search redacted records from prior Runs |
| `session_scroll` | Read surrounding events from a prior Run |
| `memory` | Read or propose durable Project/Application facts |
| `skill_manage` | Create or update generated Skill proposals |
| `todo_write` | Update the current task plan when Todo is enabled |
| `shell_tool` | Execute Shell commands (restricted by whitelist) |
| `check_background_task` | Check background task status and recent output |
| `kill_background_task` | Terminate a running background task |
| `list_background_tasks` | List all background tasks |
| `write_markdown_file` | Write Markdown file |
| `write_markdown_file_raw` | Write raw Markdown content |
| `append_markdown_sections` | Append Markdown sections |

**Example**:

```yaml
default_toolsets:
  - "core_shell"
  - "core_file"
  - "core_search"
  - "context"
  - "skills"
```

See [Built-in Tool Catalog](tool_catalog.md) for the metadata/loading seam,
extension rules, and required real-Application validation.

### 8.2 shell_settings — Shell Tool Security Policy

| Parameter | Type | Default | Required | Description |
|------|------|--------|------|------|
| `shell_settings.allowed_commands` | `str` \| `list[str]` | `"*"` | ❌ No | Whitelist of allowed Shell commands |
| `shell_settings.allowed_operators` | `str` \| `list[str]` | `"*"` | ❌ No | Whitelist of allowed Shell operators |

**allowed_commands configuration**:

| Config Value | Behavior |
|--------|------|
| `"*"` or `["*"]` | Disable command whitelist defense, allow all commands |
| `["ls", "pwd", "cat", "grep", "echo"]` | Only allow commands in the whitelist |

**allowed_operators configuration**:

| Config Value | Behavior |
|--------|------|
| `"*"` or `["*"]` | Allow all Shell operators |
| `["\|"]` | Only allow pipes, forbid redirection (prevent file writes) |

**Valid Shell operators**: `|`, `||`, `&&`, `>`, `>>`, `<`, `;`

**Security configuration examples**:

```yaml
# Maximum permissions (development environment)
shell_settings:
  allowed_commands: "*"
  allowed_operators: "*"

# Read-only audit mode (prevent filesystem modification)
shell_settings:
  allowed_commands:
    - "ls"
    - "pwd"
    - "cat"
    - "grep"
    - "echo"
    - "find"
    - "wc"
    - "head"
    - "tail"
  allowed_operators:
    - "|"
```

#### security_checks — Command Security Checks

Each check can be individually toggled (all enabled by default):

| Parameter | Default | Description |
|------|--------|------|
| `security_checks.command_substitution` | `true` | Block `$()` and backtick command substitution |
| `security_checks.process_substitution` | `true` | Block `<()` / `>()` process substitution |
| `security_checks.env_injection` | `true` | Block dangerous env vars (LD_PRELOAD, PATH, etc.) |
| `security_checks.ifs_injection` | `true` | Block IFS variable manipulation |
| `security_checks.control_characters` | `true` | Block control characters |
| `security_checks.incomplete_commands` | `true` | Block incomplete command fragments |
| `security_checks.dangerous_shell_prefix` | `true` | Block bash -c / sudo / env invocations |
| `security_checks.zsh_dangerous_commands` | `true` | Block Zsh dangerous builtins |
| `security_checks.parameter_expansion` | `true` | Block `${}` parameter expansion |
| `security_checks.destructive_patterns` | `true` | Block rm -rf /, git reset --hard, etc. |

#### Dangerous Paths & Security Mechanisms

| Parameter | Type | Default | Description |
|------|------|--------|------|
| `shell_settings.dangerous_paths` | `list[str]` | `["/", "/etc", "/usr", ...]` | Paths forbidden for rm/rmdir operations |
| `shell_settings.block_destructive` | `bool` | `true` | Enable dangerous path blocking |

> **Path boundaries** are managed by `tool_access_control.include_paths` (§9), shared across ALL tools
> (including shell_tool).  Shell-specific `dangerous_paths` only controls destructive operation blocking.
> `dangerous_paths` takes precedence over `include_paths`; even if `/etc` is included, `rm -rf /etc` is still blocked.

**Security mechanisms**:

- **cd boundary enforcement**: `cd` targets must be within workspace or `tool_access_control.include_paths`,
  preventing the shell session from escaping to arbitrary directories.
- **CWD synchronisation**: Path validation uses the shell session's actual working directory
  (not the Python process CWD), ensuring session-scoped `cd` commands are tracked.
- **Compound command tracking**: Commands like `cd src && cd ../tests && ls` are tracked
  segment-by-segment, validating each `cd` target against the effective CWD.
- **Symlink resolution**: Paths are resolved via `realpath` to prevent symlink-based escapes.

#### Security Policy Transparency

The framework automatically exposes security policy configuration to the LLM, preventing the AI from repeatedly attempting operations that will be blocked.

**How it works**:
- **Dynamic shell_tool description injection**: At agent initialisation, the `shell_tool` tool description is automatically replaced with a security policy summary generated from the current configuration, including the allowed directory list, active security checks, and denial behaviour rules.
- **Environment prompt security section**: The agent's environment prompt automatically includes security behaviour guidance, teaching the AI how to respond when a tool call is blocked (do not retry, use alternative tools, etc.).
- **Enriched denial messages**: Security block error messages include not only the denial reason but also a suggested alternative action (e.g. "Use edit_file instead of heredoc"), helping the AI quickly choose the correct fallback.
- **Single source of truth**: The policy summary reads from the same configuration sources as enforcement (`get_allowed_directories()` and `security_checks`), ensuring prompt and enforcement are always in sync.

**Effect**:
- The LLM knows which operations are restricted on the first attempt, reducing wasted steps
- Path violation errors include `Use paths within allowed directories, or use read_file/grep_search tools instead.` guidance
- Command security block errors include `Suggested alternative:` advice (e.g. `Use write_file or edit_file tool for multi-line content`)

> This feature requires no additional configuration — it activates automatically based on existing `security_checks` and `tool_access_control` settings.

#### audit_log — Shell Audit Log

| Parameter | Type | Default | Description |
|------|------|--------|------|
| `shell_settings.audit_log.enabled` | `bool` | `true` | Enable per-agent shell audit logs |
| `shell_settings.audit_log.log_policy_snapshot` | `bool` | `true` | Write one `POLICY_SNAPSHOT` entry per run with the effective shell policy, including all-allow defaults such as `allowed_commands: "*"` |
| `shell_settings.audit_log.log_success` | `bool` | `false` | Also log successful commands as `COMMAND_SUCCESS` entries |

`POLICY_SNAPSHOT` is written before the first shell command execution, so fully permissive runs still leave auditable evidence that command/operator allow-list checks were intentionally disabled.

#### sandbox — Sandbox Mode

| Parameter | Type | Default | Description |
|------|------|--------|------|
| `shell_settings.sandbox.enabled` | `bool` | `false` | Enable OS-level sandbox isolation |
| `shell_settings.sandbox.mode` | `str` | `"bwrap"` | Sandbox backend: `bwrap` (bubblewrap) / `docker` / `none` |
| `shell_settings.sandbox.allow_write` | `list[str]` | `[".", "/tmp"]` | Writable paths inside sandbox |
| `shell_settings.sandbox.deny_write` | `list[str]` | `["/etc", "/usr"]` | Paths denied for writing inside sandbox |
| `shell_settings.sandbox.network_isolation` | `bool` | `false` | Isolate network access |
| `shell_settings.sandbox.excluded_commands` | `list[str]` | `[]` | Command patterns that bypass sandbox |

**Full configuration example**:

```yaml
shell_settings:
  allowed_commands: "*"
  allowed_operators: "*"
  security_checks:
    command_substitution: true
    env_injection: true
    control_characters: true
    dangerous_shell_prefix: true
    destructive_patterns: true
  dangerous_paths: ["/", "/etc", "/usr", "/var", "/boot", "/sys", "/proc"]
  block_destructive: true
  audit_log:
    enabled: true
    log_policy_snapshot: true
    log_success: false
  sandbox:
    enabled: false
    mode: "bwrap"
    allow_write: [".", "/tmp"]
    deny_write: ["/etc", "/usr"]
    network_isolation: false
    excluded_commands: []
```

## 9. tool_access_control — Tool Access Control

Defines file path access rules for tools. By default, tools can only access files within the workspace. All path access control is unified through `path_validation` rules.

**YAML path**: `tool_access_control.*`
**Pydantic model**: `ToolAccessControlSettings`

| Parameter | Type | Default | Required | Description |
|------|------|--------|------|------|
| `tool_access_control.path_validation` | `list[dict]` | `[]` | ❌ No | Tool access control rule list. Only listed tools are path-validated; empty list = all allowed |

> **Note**: The workspace root is always equal to the project root
> (`agent_root`, i.e. the directory that contains `pyproject.toml`).
> It is detected automatically and cannot be overridden via configuration.

### 9.1 path_validation Rule Fields

Each `path_validation` entry defines path access rules for a group of tools:

| Field | Type | Default | Description |
|------|------|---------|------|
| `tools` | `list[str]` | — | Tool names that require path validation. `"*"` matches all tools |
| `exclude_paths` | `list[str]` | `[]` | Directories to deny access to. Supports `~` expansion, glob patterns (`fnmatch`), and `"*"` (deny all paths) |
| `include_paths` | `list[str]` | `[]` | Additional directories to allow outside workspace. Supports `~` expansion, glob patterns (`fnmatch`), and `"*"` (allow all paths) |
| `path_param_patterns` | `list[str]` | `[]` (code falls back to built-in defaults) | Tool parameter name patterns; matched names are treated as file paths |

### 9.2 Wildcards & Glob Matching

`include_paths` and `exclude_paths` support the following patterns:

| Pattern | Description | Example |
|---------|-------------|---------|
| `"*"` | **Wildcard**: matches all paths. `include_paths: ["*"]` = allow all; `exclude_paths: ["*"]` = deny all | `include_paths: ["*"]` |
| `~` | **Tilde expansion**: expands to user home directory | `include_paths: ["~/libs"]` → `/home/user/libs` |
| Glob pattern | **fnmatch matching**: supports `*`, `?`, `[seq]` wildcards | `include_paths: ["/home/*/code"]` matches `/home/lin/code` |
| Exact path | **Prefix matching**: absolute or relative path checked by prefix | `exclude_paths: ["secrets", "/opt/data"]` |

### 9.3 Conflict Resolution Rules

- **exclude takes priority over include** (security-first): when a path matches both `include_paths` and `exclude_paths`, **access is denied**
- Tool not in any rule → no path checks applied, allow all
- Tool in multiple rules → `include_paths` / `exclude_paths` are **merged (union)** across all matching rules
- `tools: ["*"]` matches all tools (acts as a global rule)

### 9.4 Validation Logic (Two-Layer Architecture)

**Layer 1 — Hook layer (all tools, automatic)**:
1. Tool not in any rule → allow without checks
2. Tool found in a rule → extract path parameters
3. For each path:
   - UNC / Windows special paths → block
   - Expand tilde `~`
   - Follow symlink chain (check every intermediate path)
   - Path matches `exclude_paths` (including glob)? → block (**exclude first**)
   - Path within workspace or matches `include_paths` (including glob)? → otherwise block

**Layer 2 — Search result filtering (search tools only)**:

`grep_search` and `glob_search` additionally respect `exclude_paths` for search result filtering. When the search root directory is allowed (e.g. `path="src/"`), but a sub-directory is in `exclude_paths`, matches from that sub-directory are automatically hidden from results. File operation tools (`read_file`, `edit_file`, etc.) only need Layer 1.

**Example**:
```yaml
tool_access_control:
  path_validation:
    # Shell and file tools: allow external dirs, exclude sensitive dirs
    - tools: ["shell_tool", "read_file", "edit_file", "grep_search"]
      include_paths: ["~/shared-libs", "/home/*/code"]
      exclude_paths: ["secrets", ".env"]

    # Move/copy tools: exclude build dirs
    - tools: ["", ""]
      exclude_paths: ["build", "dist"]
      path_param_patterns: ["source", "destination"]

    # Unrestricted tool (allow all paths)
    - tools: ["some_unrestricted_tool"]
      include_paths: ["*"]

    # Fully locked tool (deny all paths)
    - tools: ["some_locked_tool"]
      exclude_paths: ["*"]
```

---

## 10. tool_metadata — Tool Metadata Configuration

Declare runtime metadata for each tool (truncation threshold, concurrency safety, category, etc.). Agent YAML can override per-tool.

**YAML path**: `tool_metadata.*`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `tool_metadata.<name>.max_result_chars` | `int \| null` | `20000` | When tool output exceeds this character count, the framework persists the full result to disk and sends a preview + file path to the LLM. Set to `null` to disable truncation |
| `tool_metadata.<name>.is_concurrency_safe` | `bool` | `true` | Whether the tool can be called in parallel (used for Agent concurrency scheduling) |
| `tool_metadata.<name>.category` | `str` | `"general"` | Tool category (search / file_ops / shell / git / general) |
| `tool_metadata.<name>.disable_type_coercion` | `bool` | `false` | Disable automatic LLM parameter type coercion for this tool |
| `tool_metadata.default.*` | — | — | Fallback defaults for tools without explicit configuration |

**Example**:
```yaml
tool_metadata:
  grep_search:
    max_result_chars: 20000
    is_concurrency_safe: true
    category: search
  shell_tool:
    # shell_tool has its own large-output truncation and artifact notice.
    # Keep the outer shim threshold above that preview so the notice survives.
    max_result_chars: 40000
    is_concurrency_safe: false
    category: shell
  default:
    max_result_chars: 20000
    is_concurrency_safe: true
```

**Agent YAML override**:
```yaml
tools:
  - name: grep_search
    max_result_chars: 10000  # This Agent uses a smaller threshold
```

---

## 11. tool_output_limits — Tool Output Limits

Override the hardcoded per-tool character limits in the context compression layer (Layer 2). Independent from `tool_metadata.max_result_chars` (immediate truncation); this section controls the compression-phase character limit.

**YAML path**: `tool_output_limits.*`

**Example**:
```yaml
tool_output_limits:
  grep_search: 3000
  shell_tool: 2000
  read_file: null
  default: 3000
```

> This section is currently reserved (commented out by default). When enabled, it overrides the `TOOL_MAX_RETAIN_CHARS` dictionary in `context_compression.py`.

---

## 12. checkpoint — Checkpoint, Resume & Heartbeat

Controls Agent task **checkpoint/resume**, **heartbeat monitoring**, and **crash detection**. Enabled globally by default — every application built on AgentLoom gains this capability automatically with no extra configuration required.

**YAML path**: `checkpoint.*`

| Parameter | Type | Default | Required | Description |
|-----------|------|---------|----------|-------------|
| `checkpoint.enabled` | `bool` | `true` | ❌ No | Global switch. Set to `false` to disable all checkpoint/heartbeat functionality |
| `checkpoint.cleanup_on_success` | `bool` | `true` | ❌ No | Auto-delete checkpoint directory after successful task completion. Recommended `true` in production; set `false` for debugging to preserve artifacts |
| `checkpoint.max_resume_age` | `int` (sec) | `604800` | ❌ No | Maximum checkpoint retention period (7 days). Checkpoints older than this are treated as expired and cannot be resumed |
| `checkpoint.heartbeat_interval` | `int` (sec) | `5` | ❌ No | Heartbeat file write interval. A daemon thread writes process state (PID, step count, timestamp) to disk at this frequency for crash detection |

### 12.1 Runtime Directory Structure

Run evidence and task recovery state have independent lifecycles under the same runtime root:

```text
.agentloom/
├── runs/<application_id>/<run_id>/
│   ├── manifest.json
│   ├── logs/runtime.log[.1-.3]
│   ├── audit/{shell.jsonl[.1-.2],task_tree.json,task_events.jsonl,goal.json}
│   └── artifacts/{result.txt,shell,background,skills}/
├── checkpoints/<application_id>/<task_id>/
    ├── task_events.jsonl
    ├── task_tree.json
    ├── checkpoint.json
    ├── goal.json
    ├── heartbeat.json
    ├── workers/<worker_name>/calls/<call_index>/checkpoint.json
    ├── context_store/
│   └── file-history/
└── schedules/{jobs.json,serve-status.json,executions/}
```

Except for `manifest.json`, these run entries are conditional on logging being enabled or the corresponding evidence existing.

**Key design decisions**:
- `run_id` changes for every attempt; `task_id` remains stable across resume
- The run manifest records `task_id`; checkpoint run events and heartbeat record the current `run_id`
- Checkpoint lookup uses the canonical Application/task path and never depends on logs, `.task_index.json`, or a legacy scan
- Log closing, rotation, and runtime retention cannot remove checkpoint state
- Agent workspaces remain separate from run artifacts; Application `output_dir` remains Application-owned
- A Supervisor-owned Goal adds canonical `goal.json`; completion copies it to
  the run manifest/audit before normal success cleanup; interruptions retain it for resume

> See [Checkpoint & Resume](checkpoint.md) for the storage contract and
> [Goal Mode](goal_mode.md) for Goal-specific lifecycle rules.

### 12.2 Heartbeat Mechanism & Crash Detection

The framework maintains two levels of heartbeat:

| Level | File Location | Payload Fields |
|-------|--------------|----------------|
| **Supervisor** | `{task_id}/heartbeat.json` | `pid`, `run_id`, `timestamp`, `timestamp_iso`, `status`, `step`, `agent_name` |
| **Worker** | `{task_id}/workers/{name}/heartbeat.json` | `agent_name`, `run_id`, `pid`, `timestamp`, `calls` (per concurrent call: `status`, `step`, `started_at`, `finished_at`) |

**Crash detection logic** (`HEARTBEAT_STALE_THRESHOLD = 30` seconds):

1. Heartbeat file missing → `crashed`
2. Heartbeat `status` is `stopped` or `exited` → `crashed`
3. PID no longer alive (`os.kill(pid, 0)` fails) → `crashed`
4. Heartbeat timestamp older than 30 seconds → `crashed`
5. None of the above → `running`

### 12.3 Resume Flow

When a checkpoint for a `task_id` is found within `max_resume_age` and its status is non-normal (`crashed`/`interrupted`), the framework automatically:

1. Restores Supervisor memory steps from `checkpoint.json` (skipping already-completed reasoning steps)
2. Restores the task-scoped ContextStore and file-history index
3. For each Worker call, resumes incomplete memory under the same `call_index`, or returns a completed cached result when `input_hash` matches
4. Creates a new run directory and continues with a new `run_id` until the task completes

### 12.4 Configuration Examples

```yaml
# Debug scenario: retain checkpoints, shorten resume window
checkpoint:
  enabled: true
  cleanup_on_success: false   # Keep artifacts for post-mortem inspection
  max_resume_age: 86400       # Retain for 1 day only
  heartbeat_interval: 5

# Production scenario (use framework defaults — this entire block can be omitted)
checkpoint:
  enabled: true
  cleanup_on_success: true
  max_resume_age: 604800
  heartbeat_interval: 5
```

---

## 13. self_learning — History, Review, and Curated Memory

Controls searchable history and the review pipeline that turns typed candidates into curated Application or Project memory.

**YAML path**: `self_learning.*`

| Parameter | Type / Values | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `true` | Enables self-learning storage and model-facing memory access. |
| `events_retention_days` | `int` | `90` in repository config | Reserved compatibility value. Current history pruning requires explicit `loom sessions prune --retention-days N`. |
| `memory.prompt_max_chars` | `int` | repository-configured | Maximum memory prompt budget. |
| `memory.max_item_chars` | `int` | repository-configured | Maximum size of one curated item. |
| `memory.scope_budgets.project/application` | `int` | repository-configured | Per-scope injection budgets. |
| `review.enabled` | `bool` | `false` | Enables reviewer execution; both scope models must then be non-empty. |
| `review.application.review_model` | model type | `""` | Model used for Application candidate extraction. |
| `review.application.trigger.mode` | `manual\|batch\|after_run` | `batch` | Application review trigger. |
| `review.application.trigger.min_completed_runs` | `int >= 1` | `5` | Batch threshold for completed root runs. |
| `review.project.review_model` | model type | `""` | Model used for Project candidate extraction. |
| `review.project.trigger.mode` | `manual\|batch\|after_run` | `batch` | Project review trigger. |
| `review.project.trigger.min_candidates` | `int >= 1` | `5` | Batch threshold for Project context. |
| `review.<scope>.approval.fact/experience` | `auto\|manual` | `manual` | Per-type approval policy. Auto still requires the code evidence gate. |
| `review.artifacts.markdown` | `bool` | `true` | Selects Markdown artifacts (`REPORT.md`, `INBOX.md`, `INDEX.md`); `false` selects JSON batch/inbox/index files. |
| `review.artifacts.review_auto_applied` | `bool` | `true` | Includes auto-applied additions in review artifacts. |

Within `self_learning.review`, Application/Agent overlays may change only `application`; `review.enabled`, Project policy, and artifact settings remain project-root owned. Other `self_learning` fields follow normal overlay merging. The v5 fields `memory.review_model` and `write_approval` are invalid. For candidate schemas, review commands, approval states, and the human-only Project promotion boundary, see [Self-Learning v6](self_learning.md).

---

## Appendix A: Pydantic Model Reference Table

The framework uses Pydantic to validate system configuration. The following shows the mapping between configuration fields and Pydantic models:

| Config Section | Pydantic Model | Source File |
|--------|--------------|--------|
| Root configuration | `RootSettings` | `src/configuration/config_validation.py` |
| `system.*` | `SystemSettings` | `src/configuration/config_validation.py` |
| `model_request_headers.*` | `ModelRequestHeadersSettings` | `src/configuration/config_validation.py` |
| `tool_access_control.*` | `ToolAccessControlSettings` | `src/configuration/config_validation.py` |
| `runtime.*` | `RuntimeSettings` | `src/configuration/config_validation.py` |
| `logging.*` | `LoggingSettings` | `src/configuration/config_validation.py` |
| `self_learning.*` | `SelfLearningSettings` / `SelfLearningReviewSettings` | `src/configuration/config_validation.py` |

**`RootSettings` complete field definitions**:

| Field | Type | Default |
|------|------|--------|
| `system` | `SystemSettings` | `SystemSettings()` |
| `model_request_headers` | `ModelRequestHeadersSettings` | `ModelRequestHeadersSettings()` |
| `tool_access_control` | `ToolAccessControlSettings` | `ToolAccessControlSettings()` |
| `runtime` | `RuntimeSettings` | `RuntimeSettings()` |
| `logging` | `LoggingSettings` | `LoggingSettings()` |
| `context_engine` | `dict[str, Any]` | `{}` |
| `model` | `dict[str, Any]` | `{}` |
| `tools` | `list[Any]` | `[]` |
| `default_toolsets` | `list[str]` | `[]` |
| `toolsets` | `list[str]` | `[]` |
| `shell_settings` | `dict[str, Any]` | `{}` |
| `tool_metadata` | `dict[str, Any]` | `{}` |
| `tool_output_limits` | `dict[str, Any]` | `{}` |
| `self_learning` | `SelfLearningSettings` | `SelfLearningSettings()` |
| `hooks` | `dict[str, Any]` | `{}` |

> `RootSettings` allows extension fields; the selected backend validates `runtime_options`. Historical top-level smol fields such as `prompt` are not interpreted even when retained in raw configuration. `RuntimeSettings` and `LoggingSettings` deliberately use `extra="forbid"`; removed runtime/logging keys fail validation instead of silently selecting a second storage path.

**Fault-tolerant parsing tool set**:

| Parser | Purpose | Located in |
|--------|------|------|
| `BoolParser` | Compatible boolean input normalization, used for logging and some LLM configuration switches | `config_validation.py` / `src/runtime/logging/logger_manager.py` / `src/configuration/llm_config.py` |
| `IntParser` | Tolerant integer parsing; legacy `max_tokens: "max"` now resolves to the finite model default | `config_validation.py` / `src/configuration/llm_config.py` |
| `FloatParser` | Compatible float and integer string input, used for `temperature`, `retry_delay`, `max_retry_delay` in model config | `config_validation.py` / `src/configuration/llm_config.py` |
| `EnumParser` | General-purpose enum normalization helper, not currently consumed directly in the system.yaml main pipeline | `config_validation.py` |
| `LogLevelParser` | Parses `logging.level`, supports standard `logging` levels and `OFF` | `config_validation.py` / `src/runtime/logging/logger_manager.py` |

---

## Appendix B: Application-Level Override and Directory Structure

### Application Standard Directory Structure

Each Application **must** contain a `workflows/` directory, which the framework uses as the application marker. The `config/` and `sysprompt/` directories are optional:

```
<app>/
├── workflows/          ← Required (application marker, framework uses this to locate app_root)
│   ├── xxx_agent.yaml
│   └── worker_agents/
├── config/             ← Optional (if present, overlaid on config/system.yaml)
│   └── system.yaml
└── sysprompt/          ← Optional (custom prompt templates, see .example.yaml for reference)
    └── my_prompt.yaml
```

### Application-Level Override Discovery Mechanism

The framework starts from the Agent YAML file path and **searches upward** for a `workflows/` directory, whose parent directory is `app_root`. It then checks whether `app_root/config/system.yaml` exists:

- **Exists** → Automatically overlaid as an application-level override
- **Does not exist** → Skipped, using global configuration directly

> Whether using `loom run` or `python xxx_demo.py`, the discovery mechanism works consistently and reliably.

### Nested Applications

Nested applications (e.g., `my_app/sub_module`) hit the **nearest** `workflows/` directory, providing natural isolation:

```
applications/my_app/
├── config/system.yaml              ← my_app's own override
├── workflows/my_app_agent.yaml     ← Hits my_app
└── sub_module/
    ├── config/system.yaml          ← sub_module's independent override
    └── workflows/agent.yaml        ← Hits sub_module (won't jump to my_app)
```

### Override Examples

#### Configuration Key Override Level Reference Table

The following table shows the support status of each configuration key at different levels:

| Config Key | Global system.yaml | App-level system.yaml | Agent YAML |
|--------|----------------|-----------------|-----------|
| `system` | ✅ Supported | ✅ Supported | ✅ Supported |
| `runtime_options` | ✅ Supported | ✅ Supported | ✅ Supported, backend validated |
| `skills` | ✅ Supported | ✅ Supported | ✅ Supported (Agent private) |
| `runtime` | ✅ Supported | ❌ Rejected | ❌ Rejected |
| `logging` | ✅ Supported | ❌ Rejected | ❌ Rejected |
| `checkpoint` | ✅ Supported | ✅ Supported | ❌ Ignored |
| `tool_access_control` | ✅ Supported | ✅ Supported | ✅ Supported |
| `tools` | ✅ Supported | ✅ Supported | ✅ Supported (dict override) |

```yaml
# applications/my_app/config/system.yaml
# Only write fields that need to be overridden; the rest are automatically inherited from global config

tool_access_control:
  exclude_paths:
    - ".git"
  tool_access_control:
    - tools: ["read_file", "edit_file"]
      exclude_paths: ["build"]
```

**Merge effect**:

- `tool_access_control.exclude_paths`: `[]` → `[".git"]` ✅ Completely replaced
- `tool_access_control.path_validation`: `[]` → app-level rules ✅ Completely replaced
- `system.name`: Remains `"AgentLoom"` ✅ Inherited from global
- `logging.level`: Remains `"INFO"` ✅ Inherited from global
- All other fields: Remain at global configuration values ✅
