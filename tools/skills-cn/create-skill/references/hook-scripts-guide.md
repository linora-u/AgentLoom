# Hook 脚本开发完整指南

## 执行环境

### 5 个环境变量

| 环境变量 | 说明 | 默认值 | 示例 |
|---------|------|--------|------|
| `AGENT_NAME` | 当前 Agent 名称 | `"default"` | `"supervisor_agent"` |
| `TASK_ID` | 当前任务 ID | `""` | `"task_abc123"` |
| `TOOL_NAME` | 触发 Hook 的工具名 | `""` | `"shell_tool"` |
| `HOOK_EVENT` | 事件名称 | `""` | `"PreToolUse"` |
| `HOOK_CONTEXT_JSON` | 完整上下文 JSON | `"{}"` | 见下方 |

### HOOK_CONTEXT_JSON 结构

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

### 工作目录

Hook 脚本的 `cwd` 始终是 **Skill 目录**（包含 SKILL.md 的目录）。`./scripts/xxx.py` 路径基于此目录解析。

---

## 输出 JSON 格式

Hook 脚本通过 stdout 输出**单个 JSON 对象**：

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

### 7 个允许的字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `decision` | `string` | `"allow"` / `"block"` / `"modify"`，默认 `"allow"` |
| `modified_input` | `dict` | 修改工具输入（仅在 `decision: "modify"` + `PreToolUse` 时有效） |
| `modified_response` | `dict` | 修改工具输出（仅在 `decision: "modify"` + `PostToolUse` 时有效） |
| `agent_context` | `string` | 注入到 Agent 系统提示词 |
| `user_message` | `string` | 发送给用户的消息 |
| `reason` | `string` | 原因描述（阻断时建议填写） |
| `telemetry` | `dict` | 自定义遥测数据 |

> **严格要求**：仅允许这 7 个字段！包含其他字段将导致 Hook 以 `block` 失败。

---

## decision 的三个取值

| 取值 | 效果 |
|----|------|
| `"allow"` | 允许操作继续执行 |
| `"block"` | 阻断操作（PreToolUse 阻止工具执行，其他事件阻止后续 Hook） |
| `"modify"` | 修改输入或输出后继续 |

---

## 退出码规则

| stdout | 退出码 | 结果 |
|--------|--------|------|
| 空 | `0` | ✅ 默认允许 |
| 空 | 非 `0` | ❌ 阻断 |
| 有效 JSON | `0` | ✅ 按 JSON 中的 decision 执行 |
| 有效 JSON | 非 `0` | ❌ 强制阻断 |
| 非 JSON | 任意 | ❌ 阻断 |

---

## 完整 common.py 模板

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
    """推导 AgentLoom 项目根目录。

    检测顺序：
    1. $AGENT_LOOM_RUNTIME_ROOT 环境变量（测试时使用临时目录）。
    2. 从当前文件逐层向上查找 config/llm.yaml
       —— AgentLoom 根目录的全局唯一标识文件。
    3. pyproject.toml 兜底（向后兼容）。
    4. cwd 兜底。
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

> **根目录检测优先级**：`$AGENT_LOOM_RUNTIME_ROOT` 环境变量 > 逐层向上查找 `config/llm.yaml`（全局唯一） > `pyproject.toml` 兆底 > `Path.cwd()`。
> 优先使用 `config/llm.yaml` 是因为它是 AgentLoom 项目的全局唯一标识文件。逐层向上查找确保无论 Skill 嵌套多深（如 `applications/xxx/skills/my-skill/`）都能正确检测。
> 不要使用 `config/system.yaml` 判断根目录，因为应用级目录也可能包含此文件。

---

## Hook 脚本模板

### TaskCreated 脚本

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

### PreToolUse 脚本（带验证）

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

    # 示例：拦截危险命令
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

### PostToolUse 脚本（带日志记录）

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

    # 示例：记录工具调用日志
    output({
        "decision": "allow",
        "agent_context": f"[log] {tool} executed by {agent}",
        "telemetry": {"tool": tool, "agent": agent},
    })


if __name__ == "__main__":
    main()
```

### PostToolUseFailure 脚本（工具异常处理）

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

    # tool_response 格式：{"error": "<异常消息>", "error_type": "<异常类名>"}
    error_msg = response.get("error", "") if isinstance(response, dict) else str(response)
    error_type = response.get("error_type", "Unknown") if isinstance(response, dict) else "Unknown"

    # 示例：记录工具错误并放行
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

### SubagentStart 脚本（子任务开始追踪）

```python
# scripts/on_subtask_start.py
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from common import get_agent_name, get_tool_input, output


def main():
    agent = get_agent_name()
    tool_input = get_tool_input()
    # tool_input 包含：agent_name（Worker Agent 名称）、sub_task_id
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

### SubagentStop 脚本（子任务完成追踪）

```python
# scripts/on_subtask_finish.py
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from common import get_agent_name, get_tool_input, output


def main():
    agent = get_agent_name()
    tool_input = get_tool_input()
    # tool_input 包含：agent_name、sub_task_id、success（布尔）；失败时额外含 error
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

### Stop 脚本（最终检查）

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

    # 默认放行
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

## 错误处理最佳实践

建议所有 Hook 脚本用 `try/except` 包裹 `main()` 调用，防止脚本异常导致非零退出码 → 框架强制阻断：

```python
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 非零退出码会导致框架强制阻断，即使 JSON 中写的是 allow
        output({"decision": "allow", "reason": f"Hook error (safe allow): {e}"})
```

---

## Hook 超时配置

默认 Hook 超时时间为 20 秒。可通过 hook action 中的 `timeout` 字段自定义：

```yaml
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
          timeout: 60    # 单位：秒，设置为 60 秒
  PreToolUse:
    - matcher: "Write|Edit"
      hooks:
        - type: command
          command: python ./scripts/on_pre_tool_use.py
          timeout: 30    # 每个 hook action 可单独配置
```

---

## decision: "block" 在不同事件中的实际效果

`block` 并不总是表示"阻止工具执行"。其效果与 Hook 的触发时机有关：

| 事件 | 实际效果 | 适用理解 |
|------|-------------|------|
| **`PreToolUse`** | 可以直接阻止工具执行 | 适合做前置校验、权限控制、风险拦截 |
| **`PostToolUse`** | 不会撤销已经完成的工具执行，但可以阻止结果继续向后传递 | 适合对执行结果做二次判断或限制返回内容 |
| **`PostToolUseFailure`** | 不改变原始错误的传播结果 | 主要用于补充记录、清理状态、追加上下文 |
| **`Stop`** | 可以阻止 Agent 直接给出最终答复 | 适合做最终检查，确保必要步骤已完成 |
| **生命周期事件**（TaskCreated/TaskCompleted/StopFailure/SubagentStart/SubagentStop） | 不会中断任务主流程，但会结束当前事件后续 Hook 的继续执行 | 适合做初始化、记录、通知、状态整理 |

> **建议**：如果目标是阻止某项操作真正发生，应优先在 `PreToolUse` 阶段拦截；如果 Hook 触发时操作已经完成，则 `block` 更适合表达"限制后续处理"，而不是"撤销已发生的执行结果"。

---

## 各事件 tool_input 结构说明

不同事件的 `tool_input` 包含不同的字段：

| 事件 | `tool_input` 包含的关键字段 |
|------|---------------------------|
| `TaskCreated` | `task_id`、`cwd`、`task_text`（任务文本）、`agent_name`、`worker_agents`（Worker Agent 名称列表） |
| `TaskCompleted` / `StopFailure` | `task_id`、`cwd`、`task_text`、`agent_name`；StopFailure 时额外含 `error`、`error_type` |
| `SubagentStart` | `agent_name`（Worker Agent 名称）、`sub_task_id` |
| `SubagentStop` | `agent_name`、`sub_task_id`、`success`（布尔）；失败时额外含 `error` |
| `PreToolUse` / `PostToolUse` / `PostToolUseFailure` | 工具调用的完整输入参数（因工具而异） |
| `Stop` | `final_answer`（Agent 准备给出的最终答案） |

---

## 常见错误及避免方法

| 错误 | 原因 | 解决方案 |
|------|------|------|
| Hook 静默失败 | stdout 输出了非 JSON 内容 | 确保仅通过 `output()` 输出，不要有 `print("debug")` |
| 意外的 `block` 决策 | 脚本退出码非 0 | 用 try/except 包裹 main()，异常时也输出 allow |
| 未知字段错误 | JSON 包含 7 个允许字段之外的键 | 仅使用 decision、modified_input、modified_response、agent_context、user_message、reason、telemetry |
| Hook 超时 | 脚本执行超过 20 秒 | 优化脚本性能，或在 hook action 中设置 `timeout: 60`（参见上方"Hook 超时配置"章节） |
| 导入失败 | 缺少 `sys.path.insert` | 在脚本开头添加 `sys.path.insert(0, os.path.dirname(__file__))` |

---

## 各事件的 tool_response 取值

| 事件 | tool_response 取值 |
|------|-----------------|
| `TaskCreated` | `null` |
| `TaskCompleted` | `{"result": <最终任务结果>}` |
| `StopFailure` | `{"error": "<错误消息>", "error_type": "<异常类名>"}` |
| `SubagentStart` | `null` |
| `SubagentStop` | `null`（但 `tool_input` 中含 `success` 布尔值，失败时额外含 `error`） |
| `PreToolUse` | `null`（工具尚未执行） |
| `PostToolUse` | `{"result": <工具返回值>}` |
| `PostToolUseFailure` | `{"error": "<异常消息>", "error_type": "<异常类名>"}` |
| `Stop` | `{"memory_steps": <执行步骤数>}` 或 `null` |

> **⚠️ `PostToolUse` 和 `PostToolUseFailure` 互斥**：同一次工具调用只会触发其中一个 —— 成功时触发 `PostToolUse`，异常时触发 `PostToolUseFailure`。
