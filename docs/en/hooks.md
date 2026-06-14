# Hooks System

## 1. Overview

Hooks is AgentLoom's lifecycle interception system, allowing custom logic to be injected at key points during Agent execution (tool calls, task start/stop, session management, etc.).

Core capabilities:
- **16 Hook events** — Covering the full lifecycle: tools, tasks, sessions, compaction, and more
- **4 Hook types** — command (Shell), prompt (LLM), http (REST), agent (multi-turn verifier)
- **3-level pattern matching** — Wildcard / exact+pipe-delimited / regular expressions
- **True parallel execution** — Multiple hooks run concurrently via ThreadPoolExecutor
- **Enforced timeouts** — Each executor has independent timeout mechanisms
  - command: Timer + process group SIGTERM→SIGKILL escalation
  - prompt/agent: litellm native timeout parameter
  - http: httpx timeout
  - function hooks: ThreadPoolExecutor future.result(timeout)
- **Process group isolation** — command hooks use `os.setsid` + `os.killpg` to kill the entire process tree
- **Permission priority aggregation** — deny > allow > passthrough, safe merging of multi-hook results
- **YAML config bridge** — Declarative HookCommand auto-converted to executable Callable
- **Async Hook support** — First-line `{"async": true}` streaming detection + background process management
- **once flag** — Auto-removed after single execution
- **Dedup mechanism** — Prevents duplicate registration of the same hook
- **Thread safety** — RLock protects all shared state
- **Global toggle** — Enable/disable all hooks with one call

## 2. Hook Events

### 2.1 Tool Lifecycle

| Event | Enum Value | Trigger Timing |
|-------|------------|----------------|
| `PreToolUse` | `HookEvent.PRE_TOOL_USE` | Before tool execution |
| `PostToolUse` | `HookEvent.POST_TOOL_USE` | After successful tool execution |
| `PostToolUseFailure` | `HookEvent.POST_TOOL_USE_FAILURE` | After tool execution failure |

### 2.2 Session Lifecycle

| Event | Enum Value | Trigger Timing |
|-------|------------|----------------|
| `SessionStart` | `HookEvent.SESSION_START` | Session begins |
| `SessionEnd` | `HookEvent.SESSION_END` | Session ends |

### 2.3 Stop and Completion

| Event | Enum Value | Trigger Timing |
|-------|------------|----------------|
| `Stop` | `HookEvent.STOP` | Before final answer verification |
| `StopFailure` | `HookEvent.STOP_FAILURE` | Terminated due to API error |

### 2.4 Sub-Agent Lifecycle

| Event | Enum Value | Trigger Timing |
|-------|------------|----------------|
| `SubagentStart` | `HookEvent.SUBAGENT_START` | Sub-Agent starts |
| `SubagentStop` | `HookEvent.SUBAGENT_STOP` | Sub-Agent completes |

### 2.5 Task Lifecycle

| Event | Enum Value | Trigger Timing |
|-------|------------|----------------|
| `TaskCreated` | `HookEvent.TASK_CREATED` | Task created |
| `TaskCompleted` | `HookEvent.TASK_COMPLETED` | Task completed |

### 2.6 Other Events

| Event | Enum Value | Trigger Timing |
|-------|------------|----------------|
| `PreCompact` | `HookEvent.PRE_COMPACT` | Before context compaction |
| `PostCompact` | `HookEvent.POST_COMPACT` | After context compaction |
| `Setup` | `HookEvent.SETUP` | Repository initialization/maintenance |
| `ConfigChange` | `HookEvent.CONFIG_CHANGE` | Configuration change |
| `Notification` | `HookEvent.NOTIFICATION` | Notification dispatch |

### 2.7 Removed Legacy Names

The following legacy names have been removed. Use the new canonical names instead:

| Legacy Name | New Name |
|-------------|----------|
| `PostToolError` | `PostToolUseFailure` |
| `TaskStart` | `TaskCreated` |
| `TaskComplete` | `TaskCompleted` |
| `TaskFail` | `StopFailure` |
| `SubtaskStart` | `SubagentStart` |
| `SubtaskFinish` | `SubagentStop` |

## 3. Hook Types

### 3.1 Command Hook (Shell Commands)

```yaml
hooks:
  PreToolUse:
    - matcher: "Write"
      hooks:
        - type: command
          command: "bash check-write.sh"
          timeout: 5
          once: false
```

**Exit code protocol:**
- `0` — Success
- `2` — Blocking error (prevents tool execution)
- Other — Non-blocking error (logs a warning, allows continuation)

**Process management:**
- Uses `subprocess.Popen` + `os.setsid` for process group isolation
- On timeout, escalates SIGTERM→SIGKILL to kill the entire process tree (no orphan processes)
- Reuses `build_subprocess_env()` to filter sensitive environment variables

**stdin/stdout protocol:**
- stdin: JSON hook input + newline
- stdout: First line streaming detection for async flag, remainder is JSON result or plain text
- Environment variables: `AGENTLOOM_PROJECT_DIR`, `AGENT_NAME`, `TASK_ID`, `TOOL_NAME`, `HOOK_EVENT`, `HOOK_CONTEXT_JSON_FILE`, `STEP_NUMBER`

**Async Hook protocol:**
- Script outputs `{"async": true}` on first line → Returns success immediately, process handed off to AsyncHookRegistry for background management
- Optional `{"async": true, "asyncTimeout": 5000}` to specify background timeout (milliseconds)
- Background processes are auto-killed via process group after timeout

### 3.2 Prompt Hook (LLM Verification)

```yaml
hooks:
  Stop:
    - hooks:
        - type: prompt
          prompt: "Verify that all tests passed. $ARGUMENTS"
          timeout: 30
          model: "claude-3-haiku-20240307"
```

Returns `{ok: true/false, reason: "..."}` format.

### 3.3 HTTP Hook (REST POST)

```yaml
hooks:
  PreToolUse:
    - matcher: "Write"
      hooks:
        - type: http
          url: "https://hook.example.com/validate"
          headers:
            Authorization: "Bearer $MY_TOKEN"
          allowed_env_vars: ["MY_TOKEN"]
          timeout: 60
```

### 3.4 Agent Hook (Multi-Turn Verifier Agent)

```yaml
hooks:
  Stop:
    - hooks:
        - type: agent
          prompt: "Verify the code changes are safe. $ARGUMENTS"
          timeout: 60
          model: "claude-3-haiku-20240307"
```

## 4. Pattern Matching

The `matcher` field of a Hook supports three levels of matching:

1. **Wildcard**: `"*"`, `""`, or omitted → matches all
2. **Exact / pipe-delimited**: `"Write"` or `"Write|Edit|Delete"` → exact match
3. **Regular expression**: `"^read_.*"` → regex partial match

## 5. HookManager API

```python
from src.lib.smolagents.hooks import HookManager, HookEvent

manager = HookManager.get_instance()

# Register
manager.register_hook(HookEvent.PRE_TOOL_USE, "*", my_hook, timeout=10.0, once=True)

# Trigger
result = manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool_name", {"key": "value"})

# Global toggle
manager.disable_hooks()
manager.enable_hooks()

# Debug
hooks = manager.get_registered_hooks(HookEvent.PRE_TOOL_USE)
metrics = manager.get_hook_metrics()
manager.clear_hooks()

# Remove a specific hook
manager.remove_hook(HookEvent.PRE_TOOL_USE, my_hook)
```

## 6. YAML Configuration

### 6.1 Configuration Entry Points Overview

Hooks are currently declared via **Skill YAML**, then loaded through the `skills:` field in `system.yaml` or Agent YAML.

| Configuration Location | Direct `hooks:` field? | Description |
|------------------------|------------------------|-------------|
| **Skill YAML** (`SKILL.md` frontmatter) | ✅ Yes | **Primary method** — declare hooks in the YAML frontmatter `hooks:` field |
| **system.yaml** | ❌ Indirect | Load Skills containing hooks via `skills:` |
| **Agent YAML** | ❌ Indirect | Same — load hooks by referencing Skills via `skills:` |

> **Note**: Writing a `hooks:` top-level field directly in Agent YAML will not take effect — the `HooksConfigManager` bridge is implemented but not wired into the Agent initialization flow. All hooks should be configured through Skills.

### 6.2 Configuring Hooks in Skill YAML (Recommended)

Declare `hooks:` in the YAML frontmatter of `skills/<skill-name>/SKILL.md`:

```yaml
---
name: my-security-checker
description: "Pre-tool security validation"
version: "1.0.0"
hooks:
  PreToolUse:
    - matcher: "Write|Edit|Bash"
      hooks:
        - type: command
          command: python ./scripts/check_security.py
          timeout: 10
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/log_tool_call.py
  PostToolUse:
    - matcher: "Write|Edit"
      hooks:
        - type: command
          command: python ./scripts/on_file_changed.py
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
          once: true
  Stop:
    - hooks:
        - type: prompt
          prompt: "Verify the output is correct. $ARGUMENTS"
          timeout: 30
---
```

**Loading timing**: Skill hooks are registered during `load_skill_metadata()` when the skill is configured. The model does not need to call `load_skill()` first. `load-mode: on-demand` and `load-mode: eager` only control prompt loading, not hook registration.

### 6.3 Loading Globally via system.yaml

Reference Skills containing hooks in the `skills:` field of `config/system.yaml`:

```yaml
# config/system.yaml
skills:
  - path: "skills/agent-recall-with-files"
    load-mode: "eager"              # Full body injected into system prompt

  - path: "skills/agent-visualization"
    load-mode: "on-demand"          # Catalogue only; hooks still registered
```

Configured skills register their hooks. There is no separate hidden or hook-only skill state.

### 6.4 Loading via Agent YAML

You can also reference Skills in a specific Agent's YAML:

```yaml
# applications/my_app/workflows/my_agent.yaml
name: my_agent
model_type: powerful

skills:
  - path: "skills/agent-recall-with-files"
    load-mode: "on-demand"
```

### 6.5 Complete YAML Schema

```yaml
hooks:                                    # Dict[EventName, List[MatcherGroup]]
  <EventName>:                            # One of 16 event names
    - matcher: "<pattern>"                # Optional, defaults to "*" (matches all tools)
      hooks:                              # List[HookAction]
        - type: command                   # Required: "command" | "prompt" | "http" | "agent"

          # ─── command type fields ───
          command: "bash check.sh"        # Shell command
          timeout: 20                     # Timeout in seconds, default 20
          once: false                     # Auto-remove after single execution
          shell: "/bin/bash"              # Optional, specify shell interpreter

          # ─── prompt type fields ───
          # prompt: "Verify ... $ARGUMENTS"
          # model: "claude-3-haiku-..."   # Optional, override default model
          # timeout: 30

          # ─── http type fields ───
          # url: "https://hook.example.com/validate"
          # headers:
          #   Authorization: "Bearer $MY_TOKEN"
          # allowed_env_vars: ["MY_TOKEN"]  # Env vars allowed for expansion in headers
          # timeout: 60

          # ─── agent type fields ───
          # prompt: "Verify the code changes. $ARGUMENTS"
          # model: "claude-3-haiku-..."
          # timeout: 60
```

**Event names** — 16 supported:

| Category | Event Names |
|----------|-------------|
| Tool lifecycle | `PreToolUse`, `PostToolUse`, `PostToolUseFailure` |
| Session lifecycle | `SessionStart`, `SessionEnd` |
| Stop / completion | `Stop`, `StopFailure` |
| Sub-Agent | `SubagentStart`, `SubagentStop` |
| Task | `TaskCreated`, `TaskCompleted` |
| Compaction | `PreCompact`, `PostCompact` |
| Other | `Setup`, `ConfigChange`, `Notification` |

**Matcher patterns** — 3 levels:

| Level | Example | Description |
|-------|---------|-------------|
| Wildcard | `"*"`, `""`, or omitted | Matches all tools |
| Exact / pipe-delimited | `"Write"`, `"Write\|Edit\|Bash"` | Exact tool name match |
| Regular expression | `"^read_.*"` | Regex partial match |

### 6.6 Real-World Configuration Examples

#### Example 1: File Recall Skill (agent-recall-with-files)

```yaml
---
name: agent-recall-with-files
description: "Cross-session experience recall via file-based memory"
version: "6.0.0"
allowed-tools: "Read, Write, Edit, Bash, Glob, Grep"
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
  TaskCompleted:
    - hooks:
        - type: command
          command: python ./scripts/on_task_complete.py
  StopFailure:
    - hooks:
        - type: command
          command: python ./scripts/on_task_fail.py
  SubagentStart:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_subtask_start.py
  SubagentStop:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_subtask_finish.py
  PreToolUse:
    - matcher: "Write|Edit|Bash|Read|Glob|Grep"
      hooks:
        - type: command
          command: python ./scripts/on_pre_tool_use.py
  PostToolUse:
    - matcher: "Write|Edit|Bash|Read|Glob|Grep"
      hooks:
        - type: command
          command: python ./scripts/on_post_tool_use.py
  Stop:
    - hooks:
        - type: command
          command: python ./scripts/on_stop.py
---
```

#### Example 2: Visualization Observer Skill (agent-visualization)

```yaml
---
name: agent-visualization
description: "Passive observer. Auto-collects agent lifecycle events."
version: "1.0.0"
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
  TaskCompleted:
    - hooks:
        - type: command
          command: python ./scripts/on_task_complete.py
  StopFailure:
    - hooks:
        - type: command
          command: python ./scripts/on_task_fail.py
  SubagentStart:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_subtask_start.py
  SubagentStop:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_subtask_finish.py
  PreToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_pre_tool_use.py
  PostToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_post_tool_use.py
  PostToolUseFailure:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_post_tool_error.py
---
```

### 6.7 Loading Priority

Hooks are loaded in the following priority order (low to high):

1. **system.yaml** global skills → Inherited by all Agents
2. **AGENT_ROOT/skills/** directory auto-discovery
3. **Agent YAML** `skills:` field

Skills with duplicate names cannot be loaded (raises an error). Different Skills' hooks can listen to the same event — they are **executed in parallel** when triggered, with results aggregated by permission priority.

### 6.8 Environment Variables for Hook Scripts

command-type Hook scripts have access to the following environment variables at execution time:

| Variable | Description |
|----------|-------------|
| `AGENTLOOM_PROJECT_DIR` | Project root directory |
| `AGENT_NAME` | Current Agent name |
| `TASK_ID` | Current task ID |
| `TOOL_NAME` | Tool name that triggered the Hook |
| `HOOK_EVENT` | Current Hook event name |
| `HOOK_CONTEXT_JSON_FILE` | Path to a temporary JSON file containing the full Hook context |
| `STEP_NUMBER` | Current Agent step number (auto-incremented by smolagents framework each step), for state-aware hook scripts |

Scripts receive JSON-formatted Hook input via **stdin** and return results via **stdout**.

## 7. Parallel Execution

When multiple hooks match the same event, they are executed in parallel via `ThreadPoolExecutor`:

- **Single hook**: Direct invocation, no ThreadPool overhead
- **Multiple hooks**: Up to 8 concurrent workers, total duration ≈ slowest hook's time
- **Permission aggregation**: After parallel results are merged, deny always takes priority over allow

## 8. Configuration Bridge

Hooks declared in YAML are automatically bridged to executable functions via `HooksConfigManager`:

```python
from src.lib.smolagents.hooks import HookManager, HooksConfigManager

cm = HooksConfigManager()
cm.update(yaml_hooks_dict)

manager = HookManager.get_instance()
manager.set_config_manager(cm)

# trigger_hooks() automatically merges function hooks and config hooks
result = manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "Write", {})
```

Function hooks and config hooks can coexist and are executed simultaneously when triggered.

## 9. Async Hook Registry

`AsyncHookRegistry` manages background async hook processes:

```python
from src.lib.smolagents.hooks import AsyncHookRegistry

registry = AsyncHookRegistry.get_instance()

# Check completed background hooks
completed = registry.check_for_responses()
for hook in completed:
    print(hook.result)

# Cleanup
registry.remove_delivered()
registry.finalize_all()  # Shutdown: kill all background processes
```

- The registry stores `subprocess.Popen` handles for real process control
- Timed-out background processes are killed via SIGTERM→SIGKILL
- `finalize_all()` cleans up all background processes at system shutdown

## 10. Security

- **Path validation**: Built-in `validate_workspace_path` hook automatically blocks file access outside the workspace
- **Environment variable filtering**: command hooks use `build_subprocess_env()` to filter API keys and other sensitive variables
- **CRLF protection**: HTTP hook header values are automatically stripped of CR/LF/NUL characters
- **Process group kill**: On timeout, SIGTERM→SIGKILL escalation kills the entire process tree — no orphan processes
- **Zero thread leaks**: prompt/agent hooks use litellm's native timeout, no dependency on Thread.join
- **Config snapshots**: Immutable snapshots used during execution prevent runtime config changes from affecting in-flight hook batches
