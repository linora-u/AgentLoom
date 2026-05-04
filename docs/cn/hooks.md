# Hooks 系统

## 1. 概述

Hooks 是 AgentLoom 的生命周期拦截系统，允许在 Agent 执行的关键节点（工具调用、任务启停、会话管理等）注入自定义逻辑。

核心能力：
- **16 种 Hook 事件** — 覆盖工具、任务、会话、压缩等完整生命周期
- **4 种 Hook 类型** — command（Shell）、prompt（LLM）、http（REST）、agent（多轮验证）
- **3 级模式匹配** — 通配符 / 精确+管道分隔 / 正则表达式
- **真正的并行执行** — 多个 hook 通过 ThreadPoolExecutor 并行运行
- **超时强制执行** — 各 executor 内部独立超时机制
  - command: Timer + 进程组 SIGTERM→SIGKILL 升级
  - prompt/agent: litellm 原生 timeout 参数
  - http: httpx timeout
  - 函数 hook: ThreadPoolExecutor future.result(timeout)
- **进程组隔离** — command hook 使用 `os.setsid` + `os.killpg` 彻底杀进程树
- **权限优先级聚合** — deny > allow > passthrough，多 hook 结果安全合并
- **YAML 配置桥接** — 声明式 HookCommand 自动转换为可执行 Callable
- **异步 Hook 支持** — 首行 `{"async": true}` 流式检测 + 后台进程管理
- **once 标记** — 执行一次后自动移除
- **去重机制** — 防止相同 hook 重复注册
- **线程安全** — RLock 保护所有共享状态
- **全局开关** — 一键禁用/启用所有 hook

## 2. Hook 事件

### 2.1 工具生命周期

| 事件 | 枚举值 | 触发时机 |
|------|--------|---------|
| `PreToolUse` | `HookEvent.PRE_TOOL_USE` | 工具执行前 |
| `PostToolUse` | `HookEvent.POST_TOOL_USE` | 工具执行成功后 |
| `PostToolUseFailure` | `HookEvent.POST_TOOL_USE_FAILURE` | 工具执行失败后 |

### 2.2 会话生命周期

| 事件 | 枚举值 | 触发时机 |
|------|--------|---------|
| `SessionStart` | `HookEvent.SESSION_START` | 会话开始 |
| `SessionEnd` | `HookEvent.SESSION_END` | 会话结束 |

### 2.3 停止与完成

| 事件 | 枚举值 | 触发时机 |
|------|--------|---------|
| `Stop` | `HookEvent.STOP` | 最终答案验证前 |
| `StopFailure` | `HookEvent.STOP_FAILURE` | API 错误导致终止 |

### 2.4 子 Agent 生命周期

| 事件 | 枚举值 | 触发时机 |
|------|--------|---------|
| `SubagentStart` | `HookEvent.SUBAGENT_START` | 子 Agent 启动 |
| `SubagentStop` | `HookEvent.SUBAGENT_STOP` | 子 Agent 完成 |

### 2.5 任务生命周期

| 事件 | 枚举值 | 触发时机 |
|------|--------|---------|
| `TaskCreated` | `HookEvent.TASK_CREATED` | 任务创建 |
| `TaskCompleted` | `HookEvent.TASK_COMPLETED` | 任务完成 |

### 2.6 其他事件

| 事件 | 枚举值 | 触发时机 |
|------|--------|---------|
| `PreCompact` | `HookEvent.PRE_COMPACT` | 上下文压缩前 |
| `PostCompact` | `HookEvent.POST_COMPACT` | 上下文压缩后 |
| `Setup` | `HookEvent.SETUP` | 仓库初始化/维护 |
| `ConfigChange` | `HookEvent.CONFIG_CHANGE` | 配置变更 |
| `Notification` | `HookEvent.NOTIFICATION` | 通知发送 |

### 2.7 已移除的旧名称

以下旧名称已被移除，请使用新的规范名：

| 旧名称 | 新名称 |
|--------|--------|
| `PostToolError` | `PostToolUseFailure` |
| `TaskStart` | `TaskCreated` |
| `TaskComplete` | `TaskCompleted` |
| `TaskFail` | `StopFailure` |
| `SubtaskStart` | `SubagentStart` |
| `SubtaskFinish` | `SubagentStop` |

## 3. Hook 类型

### 3.1 Command Hook（Shell 命令）

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

**退出码协议：**
- `0` — 成功
- `2` — 阻断错误（阻止工具执行）
- 其他 — 非阻断错误（记录警告，允许继续）

**进程管理：**
- 使用 `subprocess.Popen` + `os.setsid` 进程组隔离
- 超时时通过 SIGTERM→SIGKILL 升级杀死整个进程树（无孤儿进程）
- 复用 `build_subprocess_env()` 过滤敏感环境变量

**stdin/stdout 协议：**
- stdin: JSON hook input + 换行符
- stdout: 首行流式检测 async 标记，其余为 JSON 结果或纯文本
- 环境变量: `AGENTLOOM_PROJECT_DIR`, `AGENT_NAME`, `TASK_ID`, `TOOL_NAME`, `HOOK_EVENT`, `HOOK_CONTEXT_JSON_FILE`, `STEP_NUMBER`

**异步 Hook 协议：**
- 脚本首行输出 `{"async": true}` → 立即返回成功，进程移交 AsyncHookRegistry 后台管理
- 可选 `{"async": true, "asyncTimeout": 5000}` 指定后台超时（毫秒）
- 后台超时后进程组被自动杀死

### 3.2 Prompt Hook（LLM 验证）

```yaml
hooks:
  Stop:
    - hooks:
        - type: prompt
          prompt: "Verify that all tests passed. $ARGUMENTS"
          timeout: 30
          model: "claude-3-haiku-20240307"
```

返回 `{ok: true/false, reason: "..."}` 格式。

### 3.3 HTTP Hook（REST POST）

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

### 3.4 Agent Hook（多轮验证 Agent）

```yaml
hooks:
  Stop:
    - hooks:
        - type: agent
          prompt: "Verify the code changes are safe. $ARGUMENTS"
          timeout: 60
          model: "claude-3-haiku-20240307"
```

## 4. 模式匹配

Hook 的 `matcher` 字段支持三级匹配：

1. **通配符**: `"*"`, `""`, 或省略 → 匹配所有
2. **精确/管道分隔**: `"Write"` 或 `"Write|Edit|Delete"` → 精确匹配
3. **正则表达式**: `"^read_.*"` → 正则部分匹配

## 5. HookManager API

```python
from src.lib.smolagents.hooks import HookManager, HookEvent

manager = HookManager.get_instance()

# 注册
manager.register_hook(HookEvent.PRE_TOOL_USE, "*", my_hook, timeout=10.0, once=True)

# 触发
result = manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool_name", {"key": "value"})

# 全局开关
manager.disable_hooks()
manager.enable_hooks()

# 调试
hooks = manager.get_registered_hooks(HookEvent.PRE_TOOL_USE)
metrics = manager.get_hook_metrics()
manager.clear_hooks()

# 移除特定 hook
manager.remove_hook(HookEvent.PRE_TOOL_USE, my_hook)
```

## 6. YAML 配置

### 6.1 配置入口总览

Hooks 目前通过 **Skill YAML** 声明，然后通过 `system.yaml` 或 Agent YAML 的 `skills:` 字段加载。

| 配置位置 | 是否直接写 `hooks:` | 说明 |
|----------|---------------------|------|
| **Skill YAML** (`SKILL.md` frontmatter) | ✅ 是 | **主要方式**，在 YAML frontmatter 的 `hooks:` 字段中声明 |
| **system.yaml** | ❌ 间接 | 通过 `skills:` 引用包含 hooks 的 Skill，并用 `allow-hook` 控制开关 |
| **Agent YAML** | ❌ 间接 | 同上，通过 `skills:` 引用 Skill 来加载 hooks |

> **注意**：Agent YAML 顶层写 `hooks:` 字段目前不会生效——`HooksConfigManager` 桥接器已实现但未在 Agent 初始化流程中接线。所有 hooks 都应通过 Skill 的方式配置。

### 6.2 Skill YAML 中配置 Hooks（推荐方式）

在 `skills/<skill-name>/SKILL.md` 的 YAML frontmatter 中声明 `hooks:`：

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

**加载时机**：Skill 的 hooks 在 `load_skill_metadata()` 时**立即注册**（Eager Registration），不需要等 LLM 调用 `load_skill()`。这意味着即使 `allow-model: false` 的隐藏 Skill，其 hooks 也能正常工作。

### 6.3 通过 system.yaml 全局加载

在 `config/system.yaml` 的 `skills:` 字段中引用包含 hooks 的 Skill：

```yaml
# config/system.yaml
skills:
  - path: "skills/agent-recall-with-files"
    invocation-control:
      allow-model: "force-inject"   # LLM 可见，强制注入系统提示
      allow-hook: true              # hooks 注册 ✅

  - path: "skills/agent-visualization"
    invocation-control:
      allow-model: false            # LLM 不可见（纯被动观察者）
      allow-hook: true              # hooks 仍然注册 ✅
```

**`invocation-control` 字段说明**：

| 字段 | 取值 | 说明 |
|------|------|------|
| `allow-model` | `true` | LLM 可按需调用该 Skill |
| | `false` | LLM 不可见，仅 hooks 生效（被动 Skill） |
| | `"force-inject"` | 强制注入到系统提示中 |
| `allow-hook` | `true` (默认) | 注册该 Skill 的 hooks |
| | `false` | 跳过该 Skill 的 hooks 注册 |

### 6.4 通过 Agent YAML 加载

在具体 Agent 的 YAML 中也可以引用 Skill：

```yaml
# applications/my_app/workflows/my_agent.yaml
name: my_agent
model_type: powerful

skills:
  - path: "skills/agent-recall-with-files"
    invocation-control:
      allow-model: true
      allow-hook: true
```

### 6.5 完整 YAML Schema

```yaml
hooks:                                    # Dict[EventName, List[MatcherGroup]]
  <EventName>:                            # 16 种事件名之一
    - matcher: "<pattern>"                # 可选，默认 "*"（匹配所有工具）
      hooks:                              # List[HookAction]
        - type: command                   # 必填："command" | "prompt" | "http" | "agent"

          # ─── command 类型字段 ───
          command: "bash check.sh"        # Shell 命令
          timeout: 20                     # 超时秒数，默认 20
          once: false                     # 执行一次后自动移除
          shell: "/bin/bash"              # 可选，指定 Shell 解释器

          # ─── prompt 类型字段 ───
          # prompt: "Verify ... $ARGUMENTS"
          # model: "claude-3-haiku-..."   # 可选，覆盖默认模型
          # timeout: 30

          # ─── http 类型字段 ───
          # url: "https://hook.example.com/validate"
          # headers:
          #   Authorization: "Bearer $MY_TOKEN"
          # allowed_env_vars: ["MY_TOKEN"]  # 允许在 headers 中展开的环境变量
          # timeout: 60

          # ─── agent 类型字段 ───
          # prompt: "Verify the code changes. $ARGUMENTS"
          # model: "claude-3-haiku-..."
          # timeout: 60
```

**事件名** 支持以下 16 种：

| 分类 | 事件名 |
|------|--------|
| 工具生命周期 | `PreToolUse`, `PostToolUse`, `PostToolUseFailure` |
| 会话生命周期 | `SessionStart`, `SessionEnd` |
| 停止/完成 | `Stop`, `StopFailure` |
| 子Agent | `SubagentStart`, `SubagentStop` |
| 任务 | `TaskCreated`, `TaskCompleted` |
| 压缩 | `PreCompact`, `PostCompact` |
| 其他 | `Setup`, `ConfigChange`, `Notification` |

**matcher 模式** 支持三级：

| 级别 | 示例 | 说明 |
|------|------|------|
| 通配符 | `"*"`, `""`, 或省略 | 匹配所有工具 |
| 精确 / 管道分隔 | `"Write"`, `"Write\|Edit\|Bash"` | 精确匹配工具名 |
| 正则表达式 | `"^read_.*"` | 正则部分匹配 |

### 6.6 实际配置示例

#### 示例 1：文件回忆 Skill（agent-recall-with-files）

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

#### 示例 2：可视化观察 Skill（agent-visualization）

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

### 6.7 加载优先级

Hooks 加载遵循以下优先级（从低到高）：

1. **system.yaml** 中的全局 skills → 所有 Agent 继承
2. **AGENT_ROOT/skills/** 目录自动发现
3. **Agent YAML** 中的 `skills:` 字段

同名 Skill 不可重复加载（会报错）。不同 Skill 的 hooks 可以监听相同事件，触发时**并行执行**，结果按权限优先级聚合。

### 6.8 Hook 脚本的环境变量

command 类型的 Hook 脚本在执行时可获取以下环境变量：

| 变量名 | 说明 |
|--------|------|
| `AGENTLOOM_PROJECT_DIR` | 项目根目录 |
| `AGENT_NAME` | 当前 Agent 名称 |
| `TASK_ID` | 当前任务 ID |
| `TOOL_NAME` | 触发 Hook 的工具名 |
| `HOOK_EVENT` | 当前 Hook 事件名 |
| `HOOK_CONTEXT_JSON_FILE` | 包含完整 Hook 上下文的临时 JSON 文件路径 |
| `STEP_NUMBER` | 当前 Agent 步数（smolagents 框架每步自增），用于状态感知的 hook 脚本 |

脚本通过 **stdin** 接收 JSON 格式的 Hook 输入，通过 **stdout** 返回结果。

## 7. 并行执行

多个 hook 匹配同一事件时，通过 `ThreadPoolExecutor` 并行执行：

- **单个 hook**: 直接调用，无 ThreadPool 开销
- **多个 hook**: 最多 8 个并发 worker，总耗时 ≈ 最慢 hook 的时间
- **权限聚合**: 并行结果合并后，deny 始终优先于 allow

## 8. 配置桥接

YAML 中声明的 hook 会自动通过 `HooksConfigManager` 桥接为可执行函数：

```python
from src.lib.smolagents.hooks import HookManager, HooksConfigManager

cm = HooksConfigManager()
cm.update(yaml_hooks_dict)

manager = HookManager.get_instance()
manager.set_config_manager(cm)

# trigger_hooks() 会自动合并 function hooks 和 config hooks
result = manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "Write", {})
```

函数 hook 和配置 hook 可以共存，触发时同时执行。

## 9. 异步 Hook 注册表

`AsyncHookRegistry` 管理后台异步 hook 进程：

```python
from src.lib.smolagents.hooks import AsyncHookRegistry

registry = AsyncHookRegistry.get_instance()

# 检查完成的后台 hook
completed = registry.check_for_responses()
for hook in completed:
    print(hook.result)

# 清理
registry.remove_delivered()
registry.finalize_all()  # Shutdown: 杀死所有后台进程
```

- 注册表保存 `subprocess.Popen` 句柄，可真正控制进程
- 超时的后台进程会被 SIGTERM→SIGKILL 杀死
- `finalize_all()` 在系统关闭时清理所有后台进程

## 10. 安全

- **路径验证**: 内置 `validate_workspace_path` hook 自动拦截工作区外的文件访问
- **环境变量过滤**: command hook 使用 `build_subprocess_env()` 过滤 API keys 等敏感变量
- **CRLF 防护**: HTTP hook 的 header 值自动过滤 CR/LF/NUL 字符
- **进程组杀死**: 超时后 SIGTERM→SIGKILL 升级，彻底杀死整棵进程树，不留孤儿进程
- **零线程泄漏**: prompt/agent hook 使用 litellm 原生 timeout，不依赖 Thread.join
- **配置快照**: 执行期间使用不可变快照，防止运行时配置变更影响正在执行的 hook 批次
