# Skill 模板与字段参考

## 完整 SKILL.md 模板

```
---
name: my-skill
description: "Clear description of what the Skill does. Use when: XXX scenario."
version: "1.0.0"
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
  PreToolUse:
    - matcher: "Write|Edit|Bash"
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
  SubagentStart:
    - matcher: "*"                  # 可选：匹配 Worker Agent 名称，"*" 匹配所有
      hooks:
        - type: command
          command: python ./scripts/on_subtask_start.py
  SubagentStop:
    - matcher: "*"                  # 可选：匹配 Worker Agent 名称
      hooks:
        - type: command
          command: python ./scripts/on_subtask_finish.py
  Stop:
    - hooks:
        - type: command
          command: python ./scripts/on_stop.py
---

# My Skill

Skill 功能描述。

## 使用场景
- 适用于...
- 不适用于...

## 操作步骤
1. 首先...
2. 然后...
3. 最后...

## 注意事项
- ...
```

---

## YAML Frontmatter 字段参考

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `name` | `string` | 否 | 目录名 | Skill 唯一标识符 |
| `description` | `string` | 强烈推荐 | `""` | LLM 根据此字段决定是否使用该 Skill |
| `version` | `string` | 否 | `null` | 语义化版本号 |
| `allowed-tools` | `string` / `list` | 否 | `null` | 使用的工具列表 |
| `hooks` | `dict` | 否 | `null` | Hook 事件定义 |

> **注意**：`platform` 和 `invocation-control` **不在** SKILL.md 中定义；它们在引用侧（system.yaml 或 Agent YAML）配置。

---

## 抽象工具名映射

建议在 `allowed-tools` 和 `matcher` 中使用抽象名称：

| 抽象名称 | Claude 平台实际名称 |
|--------|-------------------|
| `Read` | `read_file` |
| `Write` | `write_markdown_file` |
| `Edit` | `edit_file` |
| `Bash` | `shell_tool` |
| `Glob` | `list_files_glob` |
| `Grep` | `ripgrep_search_directory` |

---

## 9 个 Hook 事件速查

| 事件 | 需要 matcher | tool_name 值 | 典型用途 |
|------|:-----------:|:------------:|---------|
| `TaskCreated` | ✖ | `"task"` | 初始化运行时目录 |
| `TaskCompleted` | ✖ | `"task"` | 清理资源、发送通知 |
| `StopFailure` | ✖ | `"task"` | 记录失败原因 |
| `SubagentStart` | 可选 | Worker 名称 | 子任务进度跟踪。matcher 可匹配特定 Worker Agent 名称，如 `"worker_a\|worker_b"`；使用 `"*"` 匹配所有 Worker |
| `SubagentStop` | 可选 | Worker 名称 | 子任务结果记录。matcher 规则与 SubagentStart 相同 |
| `PreToolUse` | ✅ | 实际工具名 | 验证输入、修改参数 |
| `PostToolUse` | ✅ | 实际工具名 | 处理输出、记录日志 |
| `PostToolUseFailure` | ✅ | 实际工具名 | 错误处理 |
| `Stop` | ✖ | `"final_answer"` | 最终状态验证 |

---

## 引用侧配置速查（system.yaml / Agent YAML）

### invocation-control

```yaml
skills:
  - path: "skills/my-skill"
    invocation-control:
      allow-model: true              # true（按需）/ false（隐藏）/ "force-inject"（强制注入）
      allow-hook: true               # true / false
```

### 三种 Skill 类型配置

```yaml
# 强制注入类型（核心 Skill）
- path: "skills/agent-recall-with-files"
  invocation-control:
    allow-model: "force-inject"
    allow-hook: true

# 按需类型（默认）
- path: "skills/my-domain-skill"
# invocation-control 可省略，默认为 allow-model: true, allow-hook: true

# 隐藏类型（后台监控）
- path: "skills/agent-visualization"
  invocation-control:
    allow-model: false
    allow-hook: true
```

---

## 最小化 Skill（无 Hook，仅 LLM 指令）

```
---
name: coding-standards
description: "Team coding standards. Use when: code review, new feature development reference."
version: "1.0.0"
---

# 编码标准

## Python 规范
- 使用类型提示
- 函数不超过 50 行
- ...
```

这类 Skill 最适合作为 `force-inject` 类型，将标准直接注入 Agent 的系统提示词。
