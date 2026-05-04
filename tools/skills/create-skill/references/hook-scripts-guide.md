# Hook Script Development Complete Guide

## Execution Environment

### 5 Environment Variables

| Environment Variable | Description | Default | Example |
|---------|------|--------|------|
| `AGENT_NAME` | Current Agent name | `"default"` | `"supervisor_agent"` |
| `TASK_ID` | Current task ID | `""` | `"task_abc123"` |
| `TOOL_NAME` | Tool name that triggered the Hook | `""` | `"shell_tool"` |
| `HOOK_EVENT` | Event name | `""` | `"PreToolUse"` |
| `HOOK_CONTEXT_JSON` | Complete context JSON | `"{}"` | See below |

### HOOK_CONTEXT_JSON Structure

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "cwd": "/home/user/AgentLoom",
  "hook_event_name": "PreToolUse",
  "tool_name": "shell_tool",
    "tool_input": {"command": "ls -la"},
  "tool_response": null
}
```

### Working Directory

The `cwd` of Hook scripts is always the **Skill directory** (the directory containing SKILL.md). The `./scripts/xxx.py` path is resolved based on this.

---

## Output JSON Format

Hook scripts output **a single JSON object** via stdout:

```json
{
  "decision": "allow",
  "modified_input": {},
  "modified_response": {},
  "agent_context": "",
  "user_message": "",
  "reason": "",
  "telemetry": {}
}
```

### 7 Allowed Fields

| Field | Type | Description |
|------|------|------|
| `decision` | `string` | `"allow"` / `"block"` / `"modify"`, defaults to `"allow"` |
| `modified_input` | `dict` | Modify tool input (only with `decision: "modify"` + `PreToolUse`) |
| `modified_response` | `dict` | Modify tool output (only with `decision: "modify"` + `PostToolUse`) |
| `agent_context` | `string` | Inject into Agent system prompt |
| `user_message` | `string` | Message sent to the user |
| `reason` | `string` | Reason description (recommended to fill in when blocking) |
| `telemetry` | `dict` | Custom telemetry data |

> **Strict Requirement**: Only these 7 fields are allowed! Including other fields will cause the Hook to fail with `block`.

---

## Three Values for decision

| Value | Effect |
|----|------|
| `"allow"` | Allow the operation to continue |
| `"block"` | Block the operation (PreToolUse blocks tool execution, other events block subsequent Hooks) |
| `"modify"` | Modify input or output then continue |

---

## Exit Code Rules

| stdout | Exit Code | Result |
|--------|--------|------|
| Empty | `0` | ✅ Default allow |
| Empty | Non-`0` | ❌ block |
| Valid JSON | `0` | ✅ Follow decision in JSON |
| Valid JSON | Non-`0` | ❌ Force block |
| Non-JSON | Any | ❌ block |

---

## Complete common.py Template

```python
# scripts/common.py
import json
import os
from pathlib import Path


def get_agent_name() -> str:
    """Get current Agent name from environment variable"""
    return os.environ.get("AGENT_NAME", "") or "default"


def get_tool_name() -> str:
    """Get current tool name from environment variable"""
    return os.environ.get("TOOL_NAME", "") or "unknown"


def get_task_id() -> str:
    """Get task ID from environment variable"""
    return os.environ.get("TASK_ID", "") or ""


def get_hook_event() -> str:
    """Get Hook event name from environment variable"""
    return os.environ.get("HOOK_EVENT", "") or "Unknown"


def get_hook_context() -> dict:
    """Parse HOOK_CONTEXT_JSON"""
    raw = os.environ.get("HOOK_CONTEXT_JSON", "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def get_tool_input() -> dict:
    """Extract tool_input from Hook context"""
    ti = get_hook_context().get("tool_input")
    return ti if isinstance(ti, dict) else {}


def get_tool_response():
    """Extract tool_response from Hook context"""
    return get_hook_context().get("tool_response")


def output(result: dict) -> None:
    """Print JSON result to stdout (framework reads from stdout)"""
    print(json.dumps(result, ensure_ascii=False))


def _find_agent_loom_root() -> Path:
    """Derive the AgentLoom project root directory.

    Resolution order:
    1. $AGENT_LOOM_RUNTIME_ROOT env var (for tests with temp dirs).
    2. Walk upward from this file and look for config/llm.yaml
       — the globally unique AgentLoom root marker.
    3. pyproject.toml fallback (backward compat).
    4. cwd fallback.
    """
    env_root = os.environ.get("AGENT_LOOM_RUNTIME_ROOT", "").strip()
    if env_root:
        return Path(env_root)

    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "config" / "llm.yaml").exists():
            return current
        current = current.parent

    candidate = Path(__file__).resolve().parent.parent.parent.parent
    if (candidate / "pyproject.toml").exists():
        return candidate

    return Path.cwd()


def runtime_dir(agent_name: str) -> Path:
    """Return <agent_loom_root>/.runtime/<agent_name> path"""
    return _find_agent_loom_root() / ".runtime" / agent_name
```

> **Root directory detection priority**: `$AGENT_LOOM_RUNTIME_ROOT` environment variable > upward traversal for `config/llm.yaml` (globally unique) > `pyproject.toml` fallback > `Path.cwd()`.
> `config/llm.yaml` is preferred because it is the globally unique identifier file for the AgentLoom project. Upward traversal ensures correct detection regardless of how deeply the Skill is nested (e.g., `applications/xxx/skills/my-skill/`).
> Do not use `config/system.yaml` to determine the root directory, as application-level directories may also contain this file.

---

## Hook Script Templates

### TaskCreated Script

```python
# scripts/on_task_start.py
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from common import get_agent_name, get_tool_input, runtime_dir, output


def main():
    agent = get_agent_name()
    tool_input = get_tool_input()
    task_text = tool_input.get("task_text", "")
    rd = runtime_dir(agent)
    rd.mkdir(parents=True, exist_ok=True)

    output({
        "decision": "allow",
        "agent_context": f"Runtime directory ready at {rd}",
        "telemetry": {"agent": agent, "runtime_dir": str(rd)},
    })


if __name__ == "__main__":
    main()
```

### PreToolUse Script (with Validation)

```python
# scripts/on_pre_tool_use.py
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from common import get_agent_name, get_tool_name, get_tool_input, output


def main():
    agent = get_agent_name()
    tool = get_tool_name()
    tool_input = get_tool_input()

    # Example: intercept dangerous commands
    if tool == "shell_tool":
        command = tool_input.get("command", "")
        if "rm -rf /" in command:
            output({
                "decision": "block",
                "reason": f"Blocked dangerous command: {command}",
            })
            return

    output({"decision": "allow"})


if __name__ == "__main__":
    main()
```

### PostToolUse Script (with Logging)

```python
# scripts/on_post_tool_use.py
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from common import get_agent_name, get_tool_name, get_tool_response, output


def main():
    agent = get_agent_name()
    tool = get_tool_name()
    response = get_tool_response()

    # Example: log tool invocation
    output({
        "decision": "allow",
        "agent_context": f"[log] {tool} executed by {agent}",
        "telemetry": {"tool": tool, "agent": agent},
    })


if __name__ == "__main__":
    main()
```

### PostToolUseFailure Script (Tool Exception Handling)

```python
# scripts/on_post_tool_error.py
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from common import get_agent_name, get_tool_name, get_tool_response, output


def main():
    agent = get_agent_name()
    tool = get_tool_name()
    response = get_tool_response()

    # tool_response format: {"error": "<exception message>", "error_type": "<exception class name>"}
    error_msg = response.get("error", "") if isinstance(response, dict) else str(response)
    error_type = response.get("error_type", "Unknown") if isinstance(response, dict) else "Unknown"

    # Example: log tool error and allow
    output({
        "decision": "allow",
        "agent_context": f"[error] {tool} failed: {error_type} - {error_msg}",
        "telemetry": {"tool": tool, "agent": agent, "error_type": error_type},
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        output({"decision": "allow", "reason": f"Hook error (safe allow): {e}"})
```

### SubagentStart Script (Subtask Start Tracking)

```python
# scripts/on_subtask_start.py
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from common import get_agent_name, get_tool_input, output


def main():
    agent = get_agent_name()
    tool_input = get_tool_input()
    # tool_input contains: agent_name (Worker Agent name), sub_task_id
    worker_name = tool_input.get("agent_name", "unknown_worker")
    sub_task_id = tool_input.get("sub_task_id", "")

    output({
        "decision": "allow",
        "agent_context": f"[subtask] Worker '{worker_name}' started (sub_task_id: {sub_task_id})",
        "telemetry": {"worker": worker_name, "sub_task_id": sub_task_id},
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        output({"decision": "allow", "reason": f"Hook error (safe allow): {e}"})
```

### SubagentStop Script (Subtask Completion Tracking)

```python
# scripts/on_subtask_finish.py
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from common import get_agent_name, get_tool_input, output


def main():
    agent = get_agent_name()
    tool_input = get_tool_input()
    # tool_input contains: agent_name, sub_task_id, success (bool); on failure also contains error
    worker_name = tool_input.get("agent_name", "unknown_worker")
    sub_task_id = tool_input.get("sub_task_id", "")
    success = tool_input.get("success", True)
    error = tool_input.get("error", "")

    status = "completed" if success else f"failed: {error}"
    output({
        "decision": "allow",
        "agent_context": f"[subtask] Worker '{worker_name}' {status}",
        "telemetry": {"worker": worker_name, "success": success},
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        output({"decision": "allow", "reason": f"Hook error (safe allow): {e}"})
```

### Stop Script (Final Check)

```python
# scripts/on_stop.py
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from common import get_agent_name, get_tool_input, output


def main():
    agent = get_agent_name()
    tool_input = get_tool_input()
    final_answer = tool_input.get("final_answer", "")

    # Default allow
    output({
        "decision": "allow",
        "telemetry": {"agent": agent, "answer_length": len(final_answer)},
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        output({"decision": "allow", "reason": f"Hook error (safe allow): {e}"})
```

---

## Error Handling Best Practices

It is recommended that all Hook scripts wrap the `main()` call with `try/except` to prevent script exceptions from causing a non-zero exit code → framework force block:

```python
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # A non-zero exit code will cause the framework to force block, even if the JSON says allow
        output({"decision": "allow", "reason": f"Hook error (safe allow): {e}"})
```

---

## Hook Timeout Configuration

The default Hook timeout is 20 seconds. It can be customized via the `timeout` field in the hook action:

```yaml
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
          timeout: 60    # Unit: seconds, set to 60 seconds
  PreToolUse:
    - matcher: "Write|Edit"
      hooks:
        - type: command
          command: python ./scripts/on_pre_tool_use.py
          timeout: 30    # Each hook action can be configured individually
```

---

## decision: "block" Actual Effects by Event

`block` does not always mean "prevent tool execution". Its effect depends on the Hook's trigger timing:

| Event | Actual Effect | Understanding |
|------|-------------|------|
| **`PreToolUse`** | Can directly prevent tool execution | Suitable for pre-validation, permission control, risk interception |
| **`PostToolUse`** | Cannot undo already completed tool execution, but can prevent the result from propagating further | Suitable for secondary judgment on execution results or limiting returned content |
| **`PostToolUseFailure`** | Does not change the original error propagation | Primarily for supplementary logging, state cleanup, context appending |
| **`Stop`** | Can prevent the Agent from giving its final answer | Suitable for final checks, ensuring required steps are completed |
| **Lifecycle events** (TaskCreated/TaskCompleted/StopFailure/SubagentStart/SubagentStop) | Does not interrupt the main task flow, but stops subsequent Hooks for the current event from executing | Suitable for initialization, logging, notifications, state cleanup |

> **Recommendation**: If the goal is to prevent an operation from actually happening, intercept at the `PreToolUse` stage. If the Hook triggers after the operation has completed, `block` is more suitable for expressing "limit subsequent processing" rather than "undo what has already happened".

---

## tool_input Structure by Event

Different events have different fields in `tool_input`:

| Event | Key Fields in `tool_input` |
|------|---------------------------|
| `TaskCreated` | `task_id`, `cwd`, `task_text` (task text), `agent_name`, `worker_agents` (Worker name list) |
| `TaskCompleted` / `StopFailure` | `task_id`, `cwd`, `task_text`, `agent_name`; StopFailure additionally contains `error`, `error_type` |
| `SubagentStart` | `agent_name` (Worker Agent name), `sub_task_id` |
| `SubagentStop` | `agent_name`, `sub_task_id`, `success` (boolean); on failure additionally contains `error` |
| `PreToolUse` / `PostToolUse` / `PostToolUseFailure` | Complete tool call input parameters (varies by tool) |
| `Stop` | `final_answer` (the final answer the Agent is about to give) |

---

## Common Errors and How to Avoid Them

| Error | Cause | Solution |
|------|------|------|
| Hook fails silently | stdout outputs non-JSON content | Ensure output only via `output()`, do not have `print("debug")` |
| Unexpected `block` decision | Script exit code is non-0 | Wrap main() with try/except, output allow even on exception |
| Unknown field error | JSON contains keys outside the 7 allowed fields | Only use decision, modified_input, modified_response, agent_context, user_message, reason, telemetry |
| Hook timeout | Script execution exceeds 20 seconds | Optimize script performance, or set `timeout: 60` in the hook action (see "Hook Timeout Configuration" section above) |
| Import failure | Missing `sys.path.insert` | Add `sys.path.insert(0, os.path.dirname(__file__))` at the beginning of the script |

---

## tool_response Values by Event

| Event | tool_response Value |
|------|-----------------|
| `TaskCreated` | `null` |
| `TaskCompleted` | `{"result": <final task result>}` |
| `StopFailure` | `{"error": "<error message>", "error_type": "<exception class name>"}` |
| `SubagentStart` | `null` |
| `SubagentStop` | `null` (but `tool_input` contains `success` boolean; on failure also contains `error`) |
| `PreToolUse` | `null` (tool has not executed yet) |
| `PostToolUse` | `{"result": <tool return value>}` |
| `PostToolUseFailure` | `{"error": "<exception message>", "error_type": "<exception class name>"}` |
| `Stop` | `{"memory_steps": <execution step count>}` or `null` |

> **⚠️ `PostToolUse` and `PostToolUseFailure` are mutually exclusive**: For the same tool call, only one of them will be triggered — `PostToolUse` on success, `PostToolUseFailure` on exception.
| `SubagentStop` | `null` |
