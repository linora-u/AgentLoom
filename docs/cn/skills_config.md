# AgentLoom Skills 配置完整参考

> **文档定位**：本文档详细说明如何从零创建一个 Skill，包括目录结构、SKILL.md 所有参数、Hook 系统、脚本开发以及完整配置示例。
> 关于 Agent YAML 的 `skills` 字段，请参阅 [Agent 配置文档](agent_config.md)。
> 关于全局 skills 配置，请参阅 [系统配置文档](system_config.md)。
> 关于配置文件之间的覆盖关系，请参阅 [配置体系总览](config-overview.md)。

Skill 是 AgentLoom 框架中**可复用的 Agent 能力扩展包**。通过 Skill，你可以为 Agent 注入领域知识、挂载生命周期 Hook、在工具调用前后执行自定义逻辑，从而在不修改框架代码的情况下扩展 Agent 的行为。

---

## 目录

- [1. Skill 是什么 & 何时使用](#1-skill-是什么--何时使用)
- [2. Skill 目录结构规范](#2-skill-目录结构规范)
- [3. SKILL.md 文件格式](#3-skillmd-文件格式)
- [4. YAML Frontmatter 字段详解](#4-yaml-frontmatter-字段详解)
  - [4.1 name — 技能名称](#41-name--技能名称)
  - [4.2 description — 技能描述](#42-description--技能描述)
  - [4.3 version — 版本号](#43-version--版本号)
  - [4.4 allowed-tools — 允许的工具](#44-allowed-tools--允许的工具)
  - [4.5 hooks — Hook 事件定义](#45-hooks--hook-事件定义)
- [5. 引用配置参数（system.yaml / Agent YAML）](#5-引用配置参数systemyaml--agent-yaml)
  - [5.1 platform — 平台标识](#51-platform--平台标识)
  - [5.2 invocation-control — 调用权限与可见性控制](#52-invocation-control--调用权限与可见性控制)
- [6. Hook 系统完整说明](#6-hook-系统完整说明)
  - [6.1 Hook 定义语法](#61-hook-定义语法)
  - [6.2 全部 9 个 Hook 事件](#62-全部-9-个-hook-事件)
  - [6.3 matcher 匹配规则](#63-matcher-匹配规则)
  - [6.4 Hook 注册时机](#64-hook-注册时机)
- [7. Hook 脚本开发指南](#7-hook-脚本开发指南)
  - [7.1 执行环境变量](#71-执行环境变量)
  - [7.2 HOOK_CONTEXT_JSON 结构](#72-hook_context_json-结构)
  - [7.3 输出 JSON 格式（stdout）](#73-输出-json-格式stdout)
  - [7.4 decision 三种取值](#74-decision-三种取值)
  - [7.5 退出码处理规则](#75-退出码处理规则)
  - [7.6 Hook 执行流程](#76-hook-执行流程)
  - [7.7 常用工具函数模式（common.py）](#77-常用工具函数模式commonpy)
- [8. Skill 加载与引用配置](#8-skill-加载与引用配置)
  - [8.1 三层加载机制](#81-三层加载机制)
  - [8.2 引用 Skill 的语法](#82-引用-skill-的语法)
  - [8.3 工具名映射（Tools Mapping）](#83-工具名映射tools-mapping)
- [9. 从零创建 Skill 实战教程](#9-从零创建-skill-实战教程)
- [10. 内置 Skill 详解](#10-内置-skill-详解)
  - [10.1 agent-recall-with-files](#101-agent-recall-with-files)
  - [10.2 agent-visualization](#102-agent-visualization)
- [11. load_skill() 和 list_skills() API](#11-load_skill-和-list_skills-api)
- [12. 完整配置示例集](#12-完整配置示例集)
- [13. 常见问题 FAQ](#13-常见问题-faq)
- [附录：字段速查表](#附录字段速查表)

---

## 1. Skill 是什么 & 何时使用

### 1.1 三种类型

框架通过 `invocation-control.allow-model` 三态参数（详见 [5.2 节](#52-invocation-control--调用权限与可见性控制)）将 Skill 分为三种类型：

| 类型 | `allow-model` 取值 | LLM 感知方式 | 典型用途 |
|------|-------------------|-------------|----------|
| **强制注入型 Skill** | `"force-inject"` | 完整指令在 Agent 初始化时嵌入 system prompt，LLM **始终遵循**，无需调用 `load_skill()` | 记忆系统、安全规范等核心能力 |
| **按需加载型 Skill** | `true`（默认） | 出现在技能目录（`<available_skills>`）中，由 LLM 决定何时调用 `load_skill()` 加载 | 领域操作指南、工作流规范 |
| **隐藏型 Skill** | `false` | LLM 完全不知道该 Skill 存在，只通过 Hook 静默运行 | 事件采集、可视化、透明监控 |

### 1.2 适用场景

**推荐使用 Skill 的情况：**
- 需要在多个 Agent 或工作流中复用同一套操作规范
- 需要在工具调用前后执行自定义逻辑（路径验证、日志、输入改写）
- 需要跨会话持久化 Agent 的经验和状态
- 需要在 Agent 生命周期特定时刻自动触发操作

**不需要 Skill 的情况：**
- 只是一次性的简单任务，不需要复用
- 逻辑非常简单，直接写在 Agent 的 `workflow` 字段即可
- 不涉及工具拦截或生命周期操作

---

## 2. Skill 目录结构规范

一个完整的 Skill 目录结构如下：

```
skills/
└── my-skill/                    # Skill 目录，名称即默认 name
    ├── SKILL.md                 # 【必需】核心定义文件
    ├── scripts/                 # 【推荐】Hook 脚本目录
    │   ├── common.py            #   共享工具函数（供各 Hook 脚本 import）
    │   ├── on_task_start.py     #   TaskCreated 事件 Hook
    │   ├── on_task_complete.py  #   TaskCompleted 事件 Hook
    │   ├── on_task_fail.py      #   StopFailure 事件 Hook
    │   ├── on_pre_tool_use.py   #   PreToolUse 事件 Hook
    │   ├── on_post_tool_use.py  #   PostToolUse 事件 Hook
    │   ├── on_subtask_start.py  #   SubagentStart 事件 Hook
    │   └── on_subtask_finish.py #   SubagentStop 事件 Hook
    ├── templates/               # 【可选】运行时文件模板
    │   ├── context.md           #   context.md 初始模板
    │   └── trace.md             #   trace.md 初始模板
    └── references/              # 【可选】补充参考文档
        ├── examples.md          #   使用示例（供 LLM 参考）
        └── reference.md         #   快速查阅手册
```

### 各目录/文件说明

| 文件/目录 | 是否必需 | 说明 |
|-----------|---------|------|
| `SKILL.md` | **必需** | 包含 YAML frontmatter（元数据）和 Markdown 正文（LLM 指令）。框架通过扫描该文件发现 Skill |
| `scripts/` | 推荐（有 Hook 时必须） | 存放 Hook 脚本。Hook 命令 `python ./scripts/xxx.py` 中的 `./` 相对于 **Skill 目录**（即 `SKILL.md` 所在目录） |
| `scripts/common.py` | 推荐 | 封装多个 Hook 共用的工具函数（读取环境变量、输出 JSON、文件操作等），避免代码重复 |
| `templates/` | 可选 | 存放运行时文件的初始模板。在 `TaskCreated` 时可读取并写入 `.runtime/` 目录 |
| `references/` | 可选 | 人类可读的参考文档。可在 SKILL.md 正文中以链接方式引用，LLM 按需读取 |

> **注意**：框架递归扫描目录，识别文件名为 `skill.md` 或 `skills.md`（大小写不敏感，包含 `SKILL.md`/`SKILLS.MD`）。子目录名即默认 Skill name。

---

## 3. SKILL.md 文件格式

SKILL.md 文件由两部分组成：

```
---
# YAML Frontmatter（元数据，被框架解析）
name: my-skill
description: "说明这个 Skill 做什么"
version: "1.0.0"
allowed-tools: "Read, Write, Bash"
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
---

# Markdown 正文（LLM 指令，被 load_skill() 返回给 LLM）

# My Skill

这里写给 LLM 看的操作指南。当 LLM 调用 load_skill("my-skill") 后，
这里的内容会完整返回给 LLM，LLM 必须按照这里的指令执行。

## 使用场景
...

## 操作步骤
1. 首先...
2. 然后...
```

---

## 4. YAML Frontmatter 字段详解

以下所有字段均在 `SKILL.md` 文件顶部的 `---` 围栏内定义。

### 4.1 name — 技能名称

| 项目 | 说明 |
|------|------|
| 类型 | `string` |
| 必填 | 否 |
| 默认值 | Skill 目录的文件夹名称 |

在全局 Skill 注册表中唯一标识该 Skill。LLM 通过 `load_skill("<name>")` 调用时使用。

**解析规则**：如果该字段缺失、不是字符串或为空字符串，框架自动取 `SKILL.md` 所在目录的名称作为 name。

```yaml
# 显式指定名称（可以与目录名不同）
name: my-custom-skill

# 不写则默认为目录名，例如目录为 skills/my-skill/ 则 name = "my-skill"
```

> ⚠️ **同名冲突**：同一 Agent 的 Skill 视图中不能有两个同名 Skill。如果发生覆盖，框架会输出 warning 并使用后加载的 Skill。

---

### 4.2 description — 技能描述

| 项目 | 说明 |
|------|------|
| 类型 | `string` |
| 必填 | **强烈推荐** |
| 默认值 | `""` 空字符串 |

显示在 LLM 可见的技能目录（skills catalogue）中。LLM 根据此描述决定是否调用该 Skill。**描述越清晰，LLM 越能在正确时机使用 Skill。**

支持 YAML 多行语法（`|` 或 `>`）：

```yaml
# 单行
description: "跨会话记忆系统，通过文件持久化 Agent 的经验和洞察。"

# 多行（使用 | 保留换行）
description: |
  跨会话文件记忆系统。
  适用于：多步骤任务、恢复中断任务、从历史经验中学习。
  不适用于：简单一次性任务。
```

**解析规则**：非字符串类型的值会被静默转为空字符串。

---

### 4.3 version — 版本号

| 项目 | 说明 |
|------|------|
| 类型 | `string` |
| 必填 | 否 |
| 默认值 | `null` |

语义化版本号，仅用于文档记录，框架不做版本约束或兼容性检查。

```yaml
version: "1.0.0"
version: "2.3.1"
```

---

### 4.4 allowed-tools — 允许的工具

| 项目 | 说明 |
|------|------|
| 类型 | `string` 或 `list[string]` |
| 必填 | 否 |
| 默认值 | `null` |

声明该 Skill 会使用哪些工具。加载时通过 `tools_mapping` 将抽象工具名（如 `Read`）映射为实际工具名（如 `read_file`）。

**字符串格式**（用逗号、管道符或空格分隔，三种写法等价）：

```yaml
allowed-tools: "Read, Write, Bash"
allowed-tools: "Read|Write|Bash"
allowed-tools: "Read Write Bash"
```

**列表格式**：

```yaml
allowed-tools:
  - "Read"
  - "Write"
  - "Bash"
```

> **提示**：推荐使用抽象工具名（`Read`/`Write`/`Bash`/`Glob`/`Grep`/`Edit`），配合 `tools_mapping` 使 Skill 在不同平台下自动适配实际工具名。具体映射关系见 [工具名映射](#83-工具名映射tools-mapping)。

---

### 4.5 hooks — Hook 事件定义

| 项目 | 说明 |
|------|------|
| 类型 | `dict` |
| 必填 | 否 |
| 默认值 | `null` |

定义该 Skill 在哪些生命周期事件上挂载脚本。键为事件名称（对应 `HookEvent` 枚举值），值为 hook 定义列表。

详细语法见 [第 6 章：Hook 系统完整说明](#6-hook-系统完整说明)。

```yaml
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
  PreToolUse:
    - matcher: "Write|Edit|Bash"
      hooks:
        - type: command
          command: python ./scripts/on_pre_tool_use.py
```

---

## 5. 引用配置参数（system.yaml / Agent YAML）

以下参数**不在 SKILL.md 内定义**，而是在引用 Skill 时（`config/system.yaml` 或 Agent YAML 的 `skills:` 字段）指定。

### 5.1 platform — 平台标识

| 项目 | 说明 |
|------|------|
| 类型 | `string` |
| 默认值 | `"Claude"` |
| 设置位置 | `config/system.yaml` 或 Agent YAML 的 `skills:` 条目 |

指定该 Skill 使用哪个平台的工具名映射。用于将 `allowed-tools` 和 Hook `matcher` 中的抽象工具名转换为实际工具函数名。

```yaml
skills:
  - path: "skills/my-skill"
    platform: "Claude"    # 默认值，使用 Claude 平台的工具映射
```

工具名映射关系定义在 `config/system.yaml` 的 `tools_mapping` 下，详见 [工具名映射](#83-工具名映射tools-mapping)。

> **注意**：`platform` 只能在引用配置（system.yaml 或 Agent YAML 的 `skills:` 条目）中设置，**不能**在 SKILL.md 的 frontmatter 中定义（框架不会从 frontmatter 中解析 `platform` 字段）。不指定时默认为 `"Claude"`。

---

### 5.2 invocation-control — 调用权限与可见性控制

| 项目 | 说明 |
|------|------|
| 类型 | `dict` (嵌套对象) |
| 必填 | 否 |
| 默认值 | `{"allow-model": true, "allow-hook": true}` |
| 设置位置 | `config/system.yaml` 或 Agent YAML 的 `skills:` 条目 |

通过嵌套选项精确控制技能的可见性、加载策略与 Hook 权限。该字段在**引用侧**（`config/system.yaml` 或 Agent YAML 的 `skills` 条目）配置，而不是在 SKILL.md 中定义。

```yaml
skills:
  - path: "skills/agent-recall-with-files"
    invocation-control:
      allow-model: "force-inject"
      allow-hook: true
```

#### allow-model — 三态控制

`allow-model` 支持三个取值，统一控制 LLM 与该 Skill 的关系：

| 取值 | 含义 | 在 `<available_skills>` 目录 | `load_skill()` 行为 | system prompt 注入 |
|------|------|------|------|------|
| `true`（默认） | **按需加载** — LLM 可见，需主动调用 `load_skill()` | ✅ 出现 | ✅ 返回完整指令 | ❌ 不注入 |
| `false` | **对 LLM 隐藏** — LLM 完全不知道该 Skill 存在 | ❌ 不出现 | ❌ 返回错误 | ❌ 不注入 |
| `"force-inject"` | **强制注入** — Skill 完整指令在 Agent 初始化时嵌入 system prompt | ❌ 不出现（已在 prompt 中） | ⚠️ 返回去重提示 | ✅ 注入到 `<force_injected_skills>` |

#### allow-hook — 布尔开关

- `allow-hook: true`（默认）：Hook 正常注册并触发。
- `allow-hook: false`：该 Skill 的 Hook **不会注册**，也不会被触发。

`allow-hook` 与 `allow-model` 是**正交维度**——即使 `allow-model: false`（LLM 不可见），只要 `allow-hook: true`，Hook 仍然会在后台执行。

#### 示例

```yaml
skills:
  # 被动型 Skill：LLM 不感知，只靠 Hook 在后台运行
  - path: "skills/agent-visualization"
    invocation-control:
      allow-model: false
      allow-hook: true

  # 核心 Skill：完整指令强制注入到 system prompt
  - path: "skills/agent-recall-with-files"
    invocation-control:
      allow-model: "force-inject"
      allow-hook: true

  # 普通 Skill：LLM 按需加载（默认行为，可省略 invocation-control）
  - path: "skills/my-skill"
```

**容错解析**：
- `allow-model` 支持宽松解析：`true`/`"true"`/`"yes"`/`"on"`/`"y"`/`1` → `true`；`false`/`"false"`/`"no"`/`"off"`/`"n"`/`""`/`0` → `false`；`"force-inject"`/`"force_inject"`/`"inject"` → `"force-inject"`（大小写不敏感）。
- `allow-hook` 使用相同的宽松布尔解析规则。
- 未填写时均默认 `true`。

> **💡 推荐使用 `"force-inject"`**
>
> 实验分析表明，当 `allow-model: true`（按需加载）时，如果 Skill 的 `description` 描述不够精准，LLM 可能**不会主动调用 `load_skill()`**，而是选择自己编写代码来完成本应由 Skill 处理的任务——这会绕过 Skill 预设的操作规范。
>
> 将关键 Skill 设为 `allow-model: "force-inject"` 可以避免这一问题：Skill 指令在 Agent 初始化时直接嵌入 system prompt，LLM **无需判断是否调用**，必定按照 Skill 指令执行。
>
> **推荐策略**：
> - 核心 Skill（如记忆系统、安全规范）→ `"force-inject"`，确保 LLM 始终遵循
> - 领域辅助 Skill（如特定 API 操作指南）→ `true`（按需加载），节省 Token
> - 后台监控 Skill（如事件采集）→ `false`，对 LLM 完全透明

---

## 6. Hook 系统完整说明

### 6.1 Hook 定义语法

```yaml
hooks:
  <EventName>:             # 事件名称，见下方事件列表
    - matcher: "<pattern>" # 可选，默认为 "*"（匹配所有工具）
      hooks:
        - type: command    # 目前仅支持 "command" 类型
          command: "python ./scripts/on_xxx.py"  # 执行命令，cwd = Skill 目录
```

每个事件可以挂载多个 hook 定义（列表），每个定义可包含多个 `hooks` 条目。

**关键要点**：
- `matcher` 字段：用于过滤触发该 Hook 的工具名，生命周期事件（TaskCreated 等）不需要 matcher。所有事件的 `matcher` **默认值为 `"*"`**（匹配所有），显式设为 `null` 也会自动转为 `"*"`
- `type: command` 是目前框架**唯一支持的 Hook 类型**
- `command` 执行时工作目录（`cwd`）固定为 **Skill 目录**（`SKILL.md` 所在目录），因此 `./scripts/xxx.py` 路径解析正确
- Hook 脚本执行有**超时限制**（默认 20 秒）。超时后脚本进程会被终止，Hook 以 `block` 决策失败。可在 hook action 中通过 `timeout` 字段自定义（单位：秒），例如 `timeout: 60`

---

### 6.2 全部 9 个 Hook 事件

框架支持以下 9 个 Hook 事件（源自 `HookEvent` 枚举）：

| 事件名 | YAML 键值 | 触发时机 | 需要 matcher | `tool_name` 值 | 典型用途 |
|--------|-----------|---------|-------------|---------------|---------|
| 任务开始 | `TaskCreated` | Agent 开始执行任务时 | 否 | `"task"` | 初始化运行时目录、读取历史状态 |
| 任务完成 | `TaskCompleted` | 任务成功完成时 | 否 | `"task"` | 清理资源、发送通知、记录成果 |
| 任务失败 | `StopFailure` | 任务执行失败时 | 否 | `"task"` | 记录失败原因、清理状态 |
| 子任务开始 | `SubagentStart` | Worker Agent 被激活时 | 可选（缺省为 `"*"`，匹配所有） | Worker Agent 名称 | 子任务进度追踪 |
| 子任务完成 | `SubagentStop` | Worker Agent 完成时 | 可选（缺省为 `"*"`，匹配所有） | Worker Agent 名称 | 子任务结果日志 |
| 工具调用前 | `PreToolUse` | 工具执行**之前** | 是（工具名） | 实际工具函数名 | 校验输入、修改参数、注入上下文 |
| 工具调用后 | `PostToolUse` | 工具**成功执行后** | 是（工具名） | 实际工具函数名 | 处理输出、记录日志 |
| 工具调用错误 | `PostToolUseFailure` | 工具**执行异常时** | 是（工具名） | 实际工具函数名 | 错误处理、回滚操作 |
| 停止前 | `Stop` | Agent 准备给出最终答案时 | 否 | `"final_answer"` | 最终状态验证、确保工作完成 |

> **`tool_name` 说明**：不同事件触发时，环境变量 `TOOL_NAME` 和 `HOOK_CONTEXT_JSON` 中的 `tool_name` 字段取值不同。生命周期事件使用固定字符串（如 `"task"`），子任务事件使用 Worker Agent 名称（即 matcher 匹配的也是 Agent 名称），工具事件使用实际工具函数名（经 `tools_mapping` 映射后的名称）。

> **并发场景注意**：当应用层通过 `tool.batch()` 批量并发调用同一 Worker 时，`SubagentStart` 和 `SubagentStop` 事件会在**每个并发 Worker 实例中独立触发**，各携带独立的 `sub_task_id`。Hook 脚本如需写共享文件或全局状态，应自行处理并发写入安全（如以 `sub_task_id` 为维度写入独立文件，或使用文件锁）。

**`PostToolUse` 与 `PostToolUseFailure` 的触发关系**：

对于同一次工具调用，`PostToolUse` 和 `PostToolUseFailure` 是**互斥**的，只会触发其中一个：

| 场景 | PreToolUse | 工具执行 | PostToolUse | PostToolUseFailure |
|------|:-:|:-:|:-:|:-:|
| 正常执行 | ✅ allow | ✅ 成功 | ✅ 触发 | ✖ 不触发 |
| 工具报错 | ✅ allow | ❌ 异常 | ✖ 不触发 | ✅ 触发 |
| 被 Hook 阻止 | ❌ block | ✖ 不执行 | ✖ 不触发 | ✖ 不触发 |

**生命周期事件示例**（无需 `matcher`）：

```yaml
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
  TaskCompleted:
    - hooks:
        - type: command
          command: python ./scripts/on_task_complete.py
  Stop:
    - hooks:
        - type: command
          command: python ./scripts/on_stop.py
```

**工具事件示例**（需要 `matcher`）：

```yaml
hooks:
  PreToolUse:
    - matcher: "Write|Edit|Bash|Read|Glob|Grep"   # 匹配这些工具
      hooks:
        - type: command
          command: python ./scripts/on_pre_tool_use.py
  PostToolUse:
    - matcher: "*"                                  # 匹配所有工具
      hooks:
        - type: command
          command: python ./scripts/on_post_tool_use.py
```

---

### 6.3 matcher 匹配规则

`matcher` 的值使用 Python `re.fullmatch()` 进行**严格全字匹配**，同时也原生支持标准的正则表达式语法（如 `.*` 或 `|`）。

| matcher 写法 | 匹配行为 |
|-------------|---------|
| `"*"` | 匹配**所有工具**（特殊值，不走正则，直接放行） |
| `"shell_tool"` | **严格且仅匹配**名称为 `shell_tool` 的工具，不会匹配 `shell_tool_extra` |
| `"Write\|Edit\|Bash"` | 匹配这三个工具之一（全匹配） |
| `".*read.*"` | 任意正则，匹配包含 `read` 的任意工具名 |

**匹配判断优先级**：框架按以下顺序判断 matcher 是否匹配工具名：

1. 如果 matcher 为 `"*"` → **直接放行**（不走正则）
2. 如果 matcher **精确等于**工具名 → 匹配
3. 否则用 `re.fullmatch(pattern, tool_name)` 进行**正则全字匹配**；如果正则语法错误，输出 warning 并跳过

> **注意**：`matcher` 使用的是工具**实际名称**（经过 `tools_mapping` 映射后）。如果配置了 `platform: "Claude"`，SKILL.md 中写的抽象名 `"Write|Edit"` 会在加载时自动映射为 `"write_markdown_file|edit_file"`，且系统在底层会按 `"write_markdown_file|edit_file"` 进行完整匹配。

---

### 6.4 Hook 注册时机

框架采用两阶段注册策略：

1. **立即注册（Eager）**：框架扫描到 `SKILL.md` 并解析元数据时，**全部 9 个事件的 Hook 立即注册**到 `HookManager`。这确保生命周期及底层错误拦截 Hook 不会错过触发时机，也保证了被动技能（不允许模型加载）能够正常捕获事件。

2. **延迟注册（Lazy）**：Skill 的 Markdown 正文（LLM 指令）在首次调用 `load_skill()` 时才读取。但由于所有事件已在第一步完成注册，延迟注册不影响 Hook 的触发。

---

## 7. Hook 脚本开发指南

### 7.1 执行环境变量

Hook 脚本通过 Shell 执行，框架会在执行前将以下 **5 个环境变量**注入到子进程：

| 环境变量 | 说明 | 默认值 | 示例值 |
|---------|------|--------|--------|
| `AGENT_NAME` | 当前正在执行的 Agent 名称 | `"default"` | `"supervisor_agent"` |
| `TASK_ID` | 当前任务的唯一 ID | `""` （空字符串） | `"task_abc123"` |
| `TOOL_NAME` | 触发该 Hook 的工具名称 | `""` （空字符串） | `"shell_tool"` |
| `HOOK_EVENT` | 事件名称 | `""` （空字符串） | `"PreToolUse"` |
| `HOOK_CONTEXT_JSON` | 完整上下文信息（JSON 字符串） | `"{}"` | 见下节 |

> **`AGENT_NAME` 取值优先级**：优先取运行时上下文中当前实际执行的 Agent 名称；若不存在，则取 `tool_input` 中显式传入的 `agent_name`（后者可能是历史数据）；最后回退到 `"default"`。
>
> **`TASK_ID` 取值优先级**：优先取运行时上下文中的任务 ID；若不存在，则取 `tool_input` 中的 `task_id`；最后回退到空字符串 `""`。
>
> **工作目录**：Hook 脚本执行时的 `cwd` 始终是 **Skill 目录**（`SKILL.md` 所在目录），因此脚本中的相对路径（如 `./scripts/xxx.py`）基于 Skill 目录解析。

---

### 7.2 HOOK_CONTEXT_JSON 结构

`HOOK_CONTEXT_JSON` 是一个 JSON 序列化的字符串，包含如下字段：

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "cwd": "/home/user/AgentLoom",
  "hook_event_name": "PreToolUse",
  "tool_name": "shell_tool",
  "tool_input": {
    "command": "ls -la"
  },
  "tool_response": null
}
```

| 字段 | 说明 |
|------|------|
| `session_id` | 会话唯一标识（UUID） |
| `cwd` | 执行时的工作目录 |
| `hook_event_name` | 事件名称，与环境变量 `HOOK_EVENT` 相同 |
| `tool_name` | 工具名称，与环境变量 `TOOL_NAME` 相同 |
| `tool_input` | 工具调用的完整输入参数 |
| `tool_response` | 工具执行结果，不同事件内容不同（见下方详细说明） |

**`tool_response` 各事件取值详情**：

| 事件 | `tool_response` 值 | 说明 |
|------|-------------------|------|
| `PreToolUse` | `null` | 工具尚未执行 |
| `PostToolUse` | `{"result": <工具返回值>}` | 工具成功执行的返回结果 |
| `PostToolUseFailure` | `{"error": "<异常消息>", "error_type": "<异常类名>"}` | 工具执行抛出异常 |
| `TaskCompleted` | `{"result": <任务最终结果>}` | 任务成功完成时的最终输出 |
| `StopFailure` | `{"error": "<错误信息>", "error_type": "<异常类名>"}` | 任务执行失败的异常信息 |
| `Stop` | `{"memory_steps": <执行步数>}` 或 `null` | 当 `memory.steps` 是列表时有值，否则为 `null` |
| `TaskCreated` | `null` | — |
| `SubagentStart` | `null` | — |
| `SubagentStop` | `null` | tool_input 中额外含 `"success": true/false`，失败时还含 `"error": "<异常消息>"` |

**各事件 `tool_input` 结构补充说明**：

| 事件 | `tool_input` 包含的关键字段 |
|------|---------------------------|
| `TaskCreated` | `task_id`、`cwd`、`task_text`（任务文本）、`agent_name`、`worker_agents`（Worker Agent 名称列表） |
| `TaskCompleted` / `StopFailure` | `task_id`、`cwd`、`task_text`、`agent_name`；StopFailure 时额外含 `error`、`error_type` |
| `SubagentStart` | `agent_name`、`sub_task_id` |
| `SubagentStop` | `agent_name`、`sub_task_id`、`success`（布尔）；失败时额外含 `error` |
| `PreToolUse` / `PostToolUse` / `PostToolUseFailure` | 工具调用的完整输入参数（因工具而异） |
| `Stop` | `final_answer`（Agent 准备给出的最终答案） |

**在 Python 脚本中读取上下文**：

```python
import json
import os

context = json.loads(os.environ.get("HOOK_CONTEXT_JSON", "{}"))
tool_input = context.get("tool_input", {})
agent_name = os.environ.get("AGENT_NAME", "default")
```

---

### 7.3 输出 JSON 格式（stdout）

Hook 脚本通过**向 stdout 打印一个 JSON 对象**来向框架传递结果。支持以下 **7 个字段**（输出中包含其他任何 key 都会被视为合约违规，导致 Hook 以 `block` 决策失败）：

```json
{
  "decision": "allow",
  "modified_input": { "key": "新的工具输入参数" },
  "modified_response": { "result": "修改后的工具输出" },
  "agent_context": "要注入到 Agent 系统提示词中的文本",
  "user_message": "要展示给用户的消息",
  "reason": "原因说明（用于 block 时的错误描述）",
  "telemetry": { "custom_key": "自定义遥测数据" }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `decision` | `string` | 可选，缺省为 `"allow"`。取值必须是 `"allow"`、`"block"` 或 `"modify"` 之一（见下节） |
| `modified_input` | `dict` | 要覆盖的工具输入字段（仅 `decision: "modify"` 时生效，合并到原始输入上，只需写要改的字段） |
| `modified_response` | `dict` | 修改后的工具输出（仅 `decision: "modify"` 时生效） |
| `agent_context` | `string` | 追加到 Agent 系统提示词的额外上下文，用于注入记忆、状态等 |
| `user_message` | `string` | 发送给用户/界面的消息（透传给 user_message_sink） |
| `reason` | `string` | 用于说明拦截或处理原因。建议在 `block` 时填写，便于定位问题和理解系统反馈；若未填写，系统会根据触发场景给出默认提示，因此为避免信息不明确，建议显式填写。`allow`/`modify` 时也可选填。 |
| `telemetry` | `dict` | 自定义遥测/调试数据，写入日志 |

---

### 7.4 decision 三种取值

| 取值 | 含义 |
|------|------|
| `"allow"` | 允许当前操作继续执行 |
| `"block"` | 阻止当前阶段继续处理。具体效果取决于 Hook 所处的触发阶段 |
| `"modify"` | 在继续处理前调整输入或输出。`PreToolUse` 可修改输入，`PostToolUse` 可修改输出 |

#### `decision: "block"` 在不同事件中的实际效果

`block` 并不总是表示“阻止工具执行”。其效果与 Hook 的触发时机有关：

| 事件 | 实际效果 | 适用理解 |
|------|-------------|------|
| **`PreToolUse`** | 可以直接阻止工具执行 | 适合做前置校验、权限控制、风险拦截 |
| **`PostToolUse`** | 不会撤销已经完成的工具执行，但可以阻止结果继续向后传递 | 适合对执行结果做二次判断或限制返回内容 |
| **`PostToolUseFailure`** | 不改变原始错误的传播结果 | 主要用于补充记录、清理状态、追加上下文 |
| **`Stop`** | 可以阻止 Agent 直接给出最终答复 | 适合做最终检查，确保必要步骤已完成 |
| **`TaskCreated`**、**`TaskCompleted`**、**`StopFailure`**、**`SubagentStart`**、**`SubagentStop`** | 不会中断任务主流程，但会结束当前事件后续 Hook 的继续执行 | 适合做初始化、记录、通知、状态整理 |

> **建议**：如果目标是阻止某项操作真正发生，应优先在 `PreToolUse` 阶段拦截；如果 Hook 触发时操作已经完成，则 `block` 更适合表达“限制后续处理”，而不是“撤销已发生的执行结果”。

**`modified_response` 合并规则详解**：

`PostToolUse` 时框架按以下优先级处理 `modified_response`：

| 场景 | 行为 | 示例 |
|------|------|------|
| 原始结果和 `modified_response` **都是 dict** | 浅合并（`{**原始, **modified}`），重名键被覆盖 | 原始 `{"a":1,"b":2}` + modified `{"b":3}` → `{"a":1,"b":3}` |
| `modified_response` **含 `"result"` 键** | 直接返回 `modified_response["result"]` 的值 | modified `{"result":"new"}` → `"new"` |
| 其他情况 | 返回原始结果不变 | — |

---

### 7.5 退出码处理规则

框架根据脚本的**退出码**和 **stdout 内容**组合决定最终 `HookResult`：

| stdout 内容 | 退出码 | 结果 |
|------------|--------|------|
| 空 | `0` | ✅ 默认 `allow`，执行成功 |
| 空 | 非 `0` | ❌ `block`，原因：stderr 内容或"退出码 N" |
| 合法 JSON | `0` | ✅ 按 JSON 中的 `decision` 执行 |
| 合法 JSON | 非 `0` | ❌ 强制 `block`，忽略 JSON 中的 `decision` |
| 非 JSON 字符串 | 任意 | ❌ `block`，提示"输出必须是 JSON" |
| JSON 含未知 key | `0` | ❌ `block`，提示"不支持的字段" |

---

### 7.6 Hook 执行流程

当某个事件触发时，框架按注册顺序**依次**执行所有匹配该事件+matcher 的 Hook：

1. 按注册顺序执行匹配的 Hook 列表。**跨 Skill 的执行顺序取决于 Skill 的加载顺序**：全局 Skill（`config/system.yaml`）→ 自动发现 Skill（`AGENT_ROOT/skills/`）→ Agent 级 Skill（Agent YAML），同一 Skill 内的多个 Hook 按 YAML 中声明的顺序执行
2. 某个 Hook 返回 `block` → **立即中断**，后续 Hook 不再执行
3. 某个 Hook 返回 `modify` → 将 `modified_input` 传递给后续 Hook 和工具执行
4. **`agent_context` 累积**：多个 Hook 的 `agent_context` 用换行符拼接，全部注入到 Agent 提示词
5. **`user_message` 累积**：同上，多条消息全部发送

---

### 7.7 常用工具函数模式（common.py）

建议在 `scripts/common.py` 中封装常用函数，参考 `agent-recall-with-files` 的实现：

```python
# scripts/common.py
import json
import os
from pathlib import Path


def get_agent_name() -> str:
    """从环境变量获取当前 Agent 名称，默认 'default'"""
    return os.environ.get("AGENT_NAME", "") or "default"


def get_tool_name() -> str:
    """从环境变量获取当前工具名称"""
    return os.environ.get("TOOL_NAME", "") or "unknown"


def get_hook_context() -> dict:
    """解析 HOOK_CONTEXT_JSON 环境变量为 dict"""
    raw = os.environ.get("HOOK_CONTEXT_JSON", "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def get_tool_input() -> dict:
    """从 Hook 上下文中提取 tool_input"""
    ti = get_hook_context().get("tool_input")
    return ti if isinstance(ti, dict) else {}


def output(result: dict) -> None:
    """向 stdout 打印 JSON 结果（框架从 stdout 读取 HookResult）"""
    print(json.dumps(result, ensure_ascii=False))


def runtime_dir(agent_name: str) -> Path:
    """返回 <agent_loom_root>/.runtime/<agent_name> 路径"""
    # scripts/ → my-skill/ → skills/ → AgentLoom/
    candidate = Path(__file__).resolve().parent.parent.parent.parent
    if (candidate / "pyproject.toml").exists():
        root = candidate
    else:
        root = Path(os.environ.get("AGENT_LOOM_RUNTIME_ROOT", Path.cwd()))
    return root / ".runtime" / agent_name
```

在 Hook 脚本中使用（注意添加 `sys.path` 以便 import）：

```python
# scripts/on_task_start.py
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))  # 将 scripts/ 目录加入 Python 路径
from common import get_agent_name, runtime_dir, output


def main():
    agent = get_agent_name()
    rd = runtime_dir(agent)
    rd.mkdir(parents=True, exist_ok=True)

    output({
        "decision": "allow",
        "agent_context": f"[my-skill] Runtime directory ready at {rd}",
        "telemetry": {"runtime_dir": str(rd)},
    })


if __name__ == "__main__":
    main()
```

---

## 8. Skill 加载与引用配置

### 8.1 三层加载机制

框架按以下顺序加载 Skill，后加载的同名 Skill 会覆盖先加载的（并输出 warning）：

```
第 1 层：全局 Skill（config/system.yaml 的 skills: 字段）
           ↓ 所有 Agent 共享
第 2 层：自动发现（AGENT_ROOT/skills/ 目录递归扫描 `skill.md`/`skills.md`）
           ↓ 所有 Agent 共享（自动加载）
第 3 层：Agent 级 Skill（当前 Agent YAML 的 skills: 字段）
           ↓ 仅当前 Agent
```

> `AGENT_ROOT` 是包含 `config/system.yaml` 的项目根目录（即 `C.agent_root`），不是 Agent YAML 文件所在目录。

---

### 8.2 引用 Skill 的语法

#### 配置格式总览

`skills` 字段支持三种格式，框架内部统一转为列表处理：

**格式 1：单个字符串（最简写法）**

```yaml
skills: "skills/my-skill"
# 等价于: skills: [{path: "skills/my-skill"}]
# platform 默认为 "Claude"
```

**格式 2：单个字典**

```yaml
skills:
  path: "skills/my-skill"
  platform: "Claude"        # 可省略，默认 "Claude"
```

**格式 3：列表（推荐，可混合字符串和字典）**

```yaml
skills:
  - "skills/skill-a"                       # 字符串项，platform 默认 "Claude"
  - path: "skills/skill-b"                # 字典项，platform 默认 "Claude"
  - path: "skills/skill-c"
    platform: "GPT"                        # 显式指定平台
```

> 字典和字符串格式会自动转为单元素列表。`platform` 不指定时默认为 `"Claude"`，用于 `tools_mapping` 中抽象工具名到实际工具名的映射。

#### 字段说明

| 子字段 | 类型 | 默认值 | 必填 | 说明 |
|--------|------|--------|------|------|
| `path` | `string` | — | ✅ 必填 | Skill 目录路径。相对路径基于 `AGENT_ROOT` 解析，也支持绝对路径 |
| `platform` | `string` | `"Claude"` | ❌ 可选 | 工具名映射平台（只能在此处设置，SKILL.md frontmatter 中不支持） |

#### 常见用法示例

**最小配置（只写路径）**：

```yaml
skills:
  - path: "skills/agent-recall-with-files"
```

**完整配置（带所有选项）**：

```yaml
skills:
  - path: "skills/agent-recall-with-files"  # 相对于 AGENT_ROOT 或绝对路径
    platform: "Claude"                       # 工具名映射平台（默认 "Claude"）
```

**路径解析规则**：
- **相对路径**：相对于 `AGENT_ROOT`（项目根目录，包含 `config/system.yaml`）解析
- **绝对路径**：直接使用

**在 `config/system.yaml` 中的全局配置**（对所有 Agent 生效）：

```yaml
# config/system.yaml
skills:
  - path: "skills/agent-recall-with-files"
  - path: "skills/agent-visualization"
```

**在 Agent YAML 中的局部配置**（只对该 Agent 生效）：

```yaml
# applications/my-app/workflows/my-agent.yaml
skills:
  - path: "skills/my-domain-skill"
  - path: "applications/my-app/skills/local-skill"
    platform: "Claude"
```

---

### 8.3 工具名映射（Tools Mapping）

为了让 Skill 定义与具体工具函数解耦，框架通过 `config/system.yaml` 中的 `tools_mapping` 定义抽象工具名到实际工具函数名的映射：

```yaml
# config/system.yaml
tools:
  tools_mapping:
    Claude:
      Read:  "read_file"
      Write: "write_markdown_file"
      Bash:  "shell_tool"
      Glob:  "list_files_glob"
      Grep:  "ripgrep_search_directory"
      Edit:  "edit_file"
```

**映射作用范围**：加载 Skill 时，框架自动对以下内容应用映射：

1. `allowed-tools` 字段中的工具名（`Read` → `read_file`）
2. Hook `matcher` 中的工具名（`"Write|Edit"` → `"write_markdown_file|edit_file"`）

因此，SKILL.md 中推荐使用抽象名，Skill 在任何支持该映射的平台上都能正常工作：

```yaml
# SKILL.md — 使用抽象名（推荐）
allowed-tools: "Read, Write, Edit, Bash, Glob, Grep"
hooks:
  PreToolUse:
    - matcher: "Write|Edit|Bash|Read|Glob|Grep"  # 加载时自动映射为实际工具名
      hooks:
        - type: command
          command: python ./scripts/on_pre_tool_use.py
```

---

## 9. 从零创建 Skill 实战教程

本教程创建一个 **"task-logger"** Skill，在任务开始和结束时自动记录日志。

### Step 1：创建目录结构

```bash
mkdir -p skills/task-logger/scripts
```

```
skills/
└── task-logger/
    ├── SKILL.md
    └── scripts/
        ├── common.py
        ├── on_task_start.py
        └── on_task_complete.py
```

### Step 2：编写 SKILL.md

```
---
name: task-logger
description: "自动记录任务开始/完成时间到日志文件。适用于所有需要追踪任务执行情况的场景。"
version: "1.0.0"
allowed-tools: "Write, Bash"
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
          command: python ./scripts/on_task_complete.py
---

# Task Logger

这个 Skill 会自动在任务开始和完成时记录时间戳到 .logs/task_log.txt 文件。

你无需主动操作，Hook 会自动在后台记录，你只需专注完成任务本身。
```

### Step 3：编写 common.py

```python
# skills/task-logger/scripts/common.py
import json
import os
from pathlib import Path


def get_agent_name() -> str:
    return os.environ.get("AGENT_NAME", "") or "default"


def get_hook_event() -> str:
    return os.environ.get("HOOK_EVENT", "") or "Unknown"


def get_log_path() -> Path:
    """日志文件写在项目根目录下的 .logs/ 中"""
    candidate = Path(__file__).resolve().parent.parent.parent.parent
    root = candidate if (candidate / "pyproject.toml").exists() else Path.cwd()
    log_dir = root / ".logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "task_log.txt"


def output(result: dict) -> None:
    print(json.dumps(result, ensure_ascii=False))
```

### Step 4：编写 on_task_start.py

```python
# skills/task-logger/scripts/on_task_start.py
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from common import get_agent_name, get_log_path, output
from datetime import datetime


def main():
    agent = get_agent_name()
    log_path = get_log_path()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] [START] Agent: {agent}\n")
    output({
        "decision": "allow",
        "agent_context": f"[task-logger] 任务已记录到 {log_path}",
        "telemetry": {"logged_at": timestamp},
    })


if __name__ == "__main__":
    main()
```

### Step 5：编写 on_task_complete.py

```python
# skills/task-logger/scripts/on_task_complete.py
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from common import get_agent_name, get_hook_event, get_log_path, output
from datetime import datetime


def main():
    agent = get_agent_name()
    event = get_hook_event()   # "TaskCompleted" 或 "StopFailure"
    log_path = get_log_path()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = "COMPLETE" if event == "TaskCompleted" else "FAIL"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] [{status}] Agent: {agent}\n")
    output({
        "decision": "allow",
        "telemetry": {"logged_at": timestamp, "status": status},
    })


if __name__ == "__main__":
    main()
```

### Step 6：注册到 Agent 或全局配置

**方式 A：只对特定 Agent 生效**（在 Agent YAML 中）：

```yaml
# applications/my-app/workflows/my-agent.yaml
name: "my_agent"
skills:
  - path: "skills/task-logger"
```

**方式 B：对所有 Agent 全局生效**（在 system.yaml 中）：

```yaml
# config/system.yaml
skills:
  - path: "skills/task-logger"
```

### Step 7：验证运行

运行 Agent 后检查日志：

```bash
cat .logs/task_log.txt
# [2026-03-22 10:30:00] [START] Agent: my_agent
# [2026-03-22 10:35:42] [COMPLETE] Agent: my_agent
```

---

## 10. 内置 Skill 详解

### 10.1 agent-recall-with-files

> ⚠️ **弱 LLM 兼容性说明**：此 Skill 从当前版本起**默认禁用**（`config/system.yaml` 中已注释）。其机制是通过 `PreToolUse`/`PostToolUse` 生命周期 Hook 在工具调用结果消息的末尾追加 recall 内容（context.md 全文、trace.md 尾部 20 行、insights.md 尾部 30 行）。这些 hook 输出经框架 `HookManager` 统一包裹 `<system-reminder>` 标签（框架级通用机制，所有 hook 输出都会被包裹，非该 Skill 特有）。弱 LLM 在处理长上下文时存在**注意力稀疏**问题——末尾追加的指令往往被忽略不执行，或割裂 LLM 对连续工具调用结果的语义理解，影响后续决策质量。**仅在使用强 LLM 时手动启用**（如 Claude Sonnet/Opus、GPT-4o 等能够可靠关注追加系统提醒的模型）。

**路径**：`skills/agent-recall-with-files/`
**版本**：6.0.0
**类型**：主动型（LLM 可见）

**功能**：跨会话文件记忆系统。为每个 Agent 维护 3 个运行时文件，实现任务状态持久化和经验积累。

#### 运行时文件（位于 `<agent_loom_root>/.runtime/<agent_name>/`）

| 文件 | 生命周期 | 用途 |
|------|---------|------|
| `context.md` | 每次任务重置 | 任务目标、当前状态快照、剩余工作。用于任务中断后快速恢复 |
| `trace.md` | 每次任务重置 | 按时间顺序记录的操作日志，仅追加 |
| `insights.md` | **永久保留** | 跨会话经验：踩坑记录、决策依据、关键事实。永远不会被自动清除 |

#### insights.md 标签系统

记录到 `insights.md` 的条目建议使用以下标签：

| 标签 | 用途 | 示例 |
|------|------|------|
| `[pitfall]` | 踩坑记录 | `[2026-03-22] [pitfall] 该 API 必须先调用 init()，否则返回 null` |
| `[decision]` | 重要决策 | `[2026-03-22] [decision] 选择异步方案，原因是性能要求` |
| `[fact]` | 关键事实 | `[2026-03-22] [fact] 配置文件位于 config/llm.yaml` |
| `[dependency]` | 依赖关系 | `[2026-03-22] [dependency] 模块 B 依赖 A 先初始化` |
| `[perf]` | 性能相关 | `[2026-03-22] [perf] 批量操作比逐条快 10 倍` |
| `[config]` | 配置信息 | `[2026-03-22] [config] 超时设置在 execution_env.timeout` |

#### 8 个 Hook 的具体行为

| Hook | 行为 |
|------|------|
| `TaskCreated` | 重建 context.md 和 trace.md（清空重写）；保留已有 insights.md；超过 80 行时自动压缩 |
| `PreToolUse` | 向 Agent 注入：context.md 全文 + trace.md 最近 20 行 + insights.md 最近 30 行；同时规范化工具输入中的 `.runtime/` 路径 |
| `PostToolUse` | 提醒 Agent 更新 trace.md、context.md、insights.md |
| `TaskCompleted` | 提醒 Agent 完成最终状态记录 |
| `StopFailure` | 强调将失败原因记录为 `[pitfall]` 到 insights.md |
| `SubagentStart` | 提醒 Agent 在 trace.md 中记录子任务进度 |
| `SubagentStop` | 如果子任务失败，强调记录为 `[pitfall]` |
| `Stop` | 默认 allow；提醒 Agent 确保运行时文件反映最终状态 |

#### 推荐配置

```yaml
# config/system.yaml 或 Agent YAML
skills:
  - path: "skills/agent-recall-with-files"
    invocation-control:
      allow-model: "force-inject"   # 强制注入到 system prompt
      allow-hook: true
```

> **注意**：`invocation-control` 只能在引用侧（`config/system.yaml` 或 Agent YAML 的 `skills:` 条目）中配置，不能在 SKILL.md 的 frontmatter 中定义（框架不会从 frontmatter 中解析该字段）。项目默认的 `config/system.yaml` 已为该 Skill 配置了 `allow-model: "force-inject"`，无需额外指定。

---

### 10.2 agent-visualization

**路径**：`skills/agent-visualization/`
**版本**：1.0.0
**类型**：被动型（`allow-model: false`，LLM 不可见）

**功能**：透明的事件采集器。自动将 Agent 生命周期事件收集到 `visualization.json` 时间线，可用于可视化 Agent 执行过程。

#### 关键配置

SKILL.md 中只定义 name、description、hooks 等字段，`invocation-control` 在引用侧配置：

```yaml
# config/system.yaml
skills:
  - path: "skills/agent-visualization"
    invocation-control:
      allow-model: false   # LLM 完全不知道这个 Skill 存在
      allow-hook: true
```

#### 注册的 Hook（共 8 个）

| Hook 事件 | 行为 |
|-----------|------|
| `TaskCreated` | 初始化 `visualization.json`，注册 supervisor 和所有 worker agent 到 config；写入 start 事件（`status: "thinking"`） |
| `TaskCompleted` | 写入 completed 事件（`status: "completed"`） |
| `StopFailure` | 写入 error 事件（`status: "error"`） |
| `SubagentStart` | 动态添加 worker 到 config；写入 supervisor 的 agent_call 事件（`status: "waiting"`）和 worker 的 activated 事件（`status: "thinking"`） |
| `SubagentStop` | 写入 worker 的 completed/error 事件；写入 supervisor 的 agent_return 事件（`status: "reviewing"`） |
| `PreToolUse` | 写入 tool_call 事件（`status: "codeact"`）；过滤内部工具 |
| `PostToolUse` | 用工具返回值更新最近一条时间线事件的 description |
| `PostToolUseFailure` | 用错误信息更新最近一条时间线事件，将其 `status` 改为 `"error"` |

#### 文件路径与 Worker 事件路由

`visualization.json` 由 `TaskCreated` hook 创建在 `.runtime/<supervisor_name>/visualization.json`。

**关键机制**：所有 Worker 的事件**不会**写入各自的 `.runtime/<worker_name>/` 目录，而是通过 `find_supervisor_viz_path()` 统一路由到 **supervisor 的 `visualization.json`** 中。该函数遍历 `.runtime/*/visualization.json`，找到 `config.agents` 中包含 `type == "supervisor"` 的文件。这样所有 Agent 的时间线汇聚在同一个文件中，便于整体可视化。

> **初始化阶段重定向**：如果 Worker Agent 尚未被"激活"（时间线中没有对应的 `activated` 事件），其工具调用会被自动归属到 supervisor（因为此时 Worker 还在框架初始化阶段，如 Skill Hook 扫描等）。

#### visualization.json 结构示例

```json
{
  "config": {
    "title": "Agent Execution: supervisor_agent",
    "agents": [
      {"name": "supervisor_agent", "type": "supervisor"},
      {"name": "worker_agent_a", "type": "worker"}
    ]
  },
  "timeline": [
    {
      "step": 1,
      "agent_name": "supervisor_agent",
      "agent_type": "supervisor",
      "event_type": "start",
      "status": "thinking",
      "description": "Task started"
    },
    {
      "step": 2,
      "agent_name": "supervisor_agent",
      "agent_type": "supervisor",
      "event_type": "tool_call",
      "status": "codeact",
      "tool_name": "shell_tool",
      "description": "Calling tool: shell_tool"
    }
  ]
}
```

#### status 值对照表

| status | 含义 | 使用场景 |
|--------|------|----------|
| `"thinking"` | Agent 正在思考 | TaskCreated（supervisor）、SubagentStart（worker 激活） |
| `"codeact"` | 正在执行工具调用 | PreToolUse（所有工具） |
| `"waiting"` | supervisor 等待 worker 返回 | SubagentStart（supervisor 侧） |
| `"reviewing"` | supervisor 审查 worker 结果 | SubagentStop（supervisor 侧） |
| `"completed"` | 成功完成 | TaskCompleted、SubagentStop（worker 成功） |
| `"error"` | 执行出错 | StopFailure、PostToolUseFailure、SubagentStop（worker 失败） |

以下工具调用会被**过滤**，不记录到时间线（框架内部工具）：
`validate_workspace_path`、`shell_hook_wrapper`、`final_answer`

---

## 11. load_skill() 和 list_skills() API

这两个工具函数是 Agent 在运行时与 Skill 系统交互的接口，默认包含在 `config/system.yaml` 的 `default_loaded_tools` 列表中。

> **远程环境注意事项（Docker / E2B）**
>
> 当 `execution_env.type` 为 `"docker"` 或 `"e2b"` 时，框架会跳过 `default_loaded_tools` 中**所有**默认工具的加载（包括 `load_skill` 和 `list_skills`），Agent 在运行时无法主动调用这两个工具。这是有意的设计决策——框架对 docker/e2b 模式采用**整个列表级别的跳过**（而非按工具逐个判断是否适用），因为大部分默认工具（`shell_tool`、`read_file` 等）依赖本地文件系统，在远程环境中不可用。`load_skill` 和 `list_skills` 虽然不依赖本地文件系统（从内存读取），但也被一同跳过。
>
> **但 Skills 系统本身不受影响**：
> - SkillsManager 始终初始化，Hook 始终注册和触发
> - `allow-model: "force-inject"` 的 Skill 始终注入 system prompt
>
> **替代方案**：
> - **推荐**：将关键 Skill 设为 `allow-model: "force-inject"`，无需 `load_skill()` 调用
> - **备选**：在 Agent YAML 的 `tools:` 中显式声明 `load_skill` / `list_skills`
>
> **默认工具分类说明**：
>
> | 工具 | 依赖本地文件系统 | docker/e2b 下默认加载 |
> |------|:-:|:-:|
> | `load_skill` | 否（从内存读取） | ❌ |
> | `list_skills` | 否（从内存读取） | ❌ |
> | `shell_tool` | 是 | ❌ |
> | `read_file` | 是 | ❌ |
> | `list_files_glob` | 是 | ❌ |
> | `ripgrep_search_directory` | 是 | ❌ |
> | `edit_file` | 是 | ❌ |
> | `write_markdown_file` | 是 | ❌ |

### load_skill()

**功能**：加载指定 Skill 的完整指令，返回给 LLM 执行。

```python
load_skill(skill: str, args: Optional[str] = None) -> str
```

| 参数 | 类型 | 说明 |
|------|------|------|
| `skill` | `string` | Skill 的 name 标识符，例如 `"task-logger"` |
| `args` | `string`（可选） | 透传给 Skill 上下文的参数字符串 |

返回 XML 结构的字符串，包含 Skill 名称、描述、允许工具列表、完整指令正文。若 Skill 不存在则抛出 `ValueError` 并列出所有可用名称。

**特殊情况**：如果该 Skill 声明了 `allow-model: "force-inject"`，调用会返回去重提示：

```xml
<skill_already_loaded>
Skill 'agent-recall-with-files' has already been force-injected into the system prompt.
Its full instructions are already in your context under <force_injected_skills>.
You do NOT need to call load_skill for this skill.
</skill_already_loaded>
```

---

### list_skills()

**功能**：列出所有 LLM 可见的 Skill（未被 `allow-model: false` 隐藏的）及其描述。
即使某个 Skill 声明了 `allow-model: "force-inject"`，它仍会出现在 `list_skills()` 中（因为它并没有被隐藏）。

> **澄清**：`list_skills()` 返回的是所有未被 `allow-model: false` 隐藏的 Skill 列表（包括 `force-inject` 的 Skill）。而 system prompt 中的 `<available_skills>` 目录只包含 `allow-model: true`（按需加载）的 Skill，不包含 `force-inject` 的（因为其指令已在 `<force_injected_skills>` 中）。两者范围不同。

```python
list_skills(include_description: bool = True) -> str
```

**返回值示例**：

```json
[
  {"name": "agent-recall-with-files", "description": "Cross-session experience recall..."},
  {"name": "task-logger", "description": "自动记录任务开始/完成时间到日志文件"}
]
```

---

## 12. 完整配置示例集

### 示例 1：最小化 SKILL.md（无 Hook，纯指令）

```
---
name: code-review-guide
description: "代码审查流程指南。进行代码审查时确保覆盖所有关键检查点。"
---

# 代码审查指南

## 必须检查的项目

1. **功能正确性**：逻辑是否符合需求
2. **安全性**：是否有 SQL 注入、XSS 等风险
3. **性能**：是否有明显的性能瓶颈
```

---

### 示例 2：完整 SKILL.md（含全部字段和多个 Hook）

```yaml
---
name: safe-file-ops
description: "安全文件操作 Skill。在所有文件写入操作前自动备份，防止意外覆盖重要文件。"
version: "2.0.0"
allowed-tools: "Read, Write, Edit, Bash"
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
  PreToolUse:
    - matcher: "Write|Edit"
      hooks:
        - type: command
          command: python ./scripts/on_pre_write.py
  PostToolUse:
    - matcher: "Write|Edit"
      hooks:
        - type: command
          command: python ./scripts/on_post_write.py
  StopFailure:
    - hooks:
        - type: command
          command: python ./scripts/on_task_fail.py
---

# 安全文件操作

使用此 Skill 进行文件操作时，系统会自动在写入前备份目标文件。
```

---

### 示例 3：system.yaml 全局配置

```yaml
# config/system.yaml
skills:
  - path: "skills/agent-recall-with-files"  # 在 system.yaml 中配置了 allow-model: "force-inject"
    invocation-control:
      allow-model: "force-inject"
      allow-hook: true

  - path: "skills/agent-visualization"      # 在 system.yaml 中配置了 allow-model: false
    invocation-control:
      allow-model: false
      allow-hook: true

  - path: "skills/company-standards"        # 自定义全局 Skill（默认 allow-model: true）
```

---

### 示例 4：Agent YAML 引用 Skill

```yaml
# applications/my-app/workflows/my-agent.yaml
name: "my_agent"
description: "我的 Agent"
model_type: "powerful"

skills:
  - path: "skills/task-logger"                   # 相对路径
  - path: "applications/my-app/skills/domain"   # 应用内 Skill
    platform: "Claude"
  - path: "skills/critical-workflow"
```

---

## 13. 常见问题 FAQ

**Q：Hook 没有被触发，怎么排查？**

1. 检查事件名称拼写是否正确：区分大小写，必须与表格中完全一致（如 `TaskCreated`，不是 `task_start`）
2. 检查 `matcher` 和实际工具名是否匹配（注意工具名经 `tools_mapping` 映射后的实际名称）
3. 查看运行日志，搜索 `Loaded skill metadata: <name>` 确认 Skill 是否成功加载
4. 确认 Hook 脚本路径 `./scripts/xxx.py` 相对于 Skill 目录正确

---

**Q：Hook 脚本报错阻止了工具执行，但我想让它默认放行？**

确保脚本在任何情况下退出码为 `0` 且输出合法 JSON：

```python
try:
    # ... 你的逻辑 ...
    output({"decision": "allow"})
except Exception as e:
    output({"decision": "allow", "reason": f"Hook error (ignored): {e}"})
    sys.exit(0)
```

---

**Q：`allow-model: "force-inject"` 的 Skill，Hook 还会执行吗？**

会。`allow-model` 只影响 Skill 指令如何呈现给 LLM，**不影响 Hook 的执行**。Hook 由 `allow-hook` 控制，两者是正交维度。

---

**Q：SKILL.md 正文能引用 `references/` 下的文件吗？**

可以以 Markdown 链接方式引用，但框架**不会自动加载链接文件**。LLM 需要主动通过 `read_file` 工具按需读取。这是渐进式加载机制，避免一次性占用过多 Token。

---

**Q：`import common` 报找不到模块？**

Hook 脚本工作目录是 Skill 目录，需要手动将 `scripts/` 加入 Python 路径：

```python
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from common import ...
```

---

**Q：同一个事件能挂多个 Hook 脚本吗？**

可以，在 `hooks` 列表中添加多个条目，按顺序执行：

```yaml
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start_a.py
        - type: command
          command: python ./scripts/on_task_start_b.py
```

任意一个返回 `block` 则中断后续执行。

---

**Q：如果设置了 `allow-model: false` 的被动技能，Hook 还会执行吗？**

会。只要 `allow-hook: true`（默认就是 true），系统依然会在后台自动触发它的 Hook 机制。
如果 `allow-hook: false`，该 Skill 的 Hook 不会注册，也不会被触发。这就是为什么要拆分成两个正交参数的原因。

---

## 附录：字段速查表

### SKILL.md Frontmatter 字段（框架直接解析）

| 字段 | 类型 | 必填 | 默认值 | 简要说明 |
|------|------|------|--------|---------|
| `name` | `string` | 否 | 目录名 | 全局唯一标识符 |
| `description` | `string` | 推荐 | `""` | LLM 技能目录中展示的描述 |
| `version` | `string` | 否 | `null` | 语义化版本号，仅用于文档 |
| `allowed-tools` | `string` 或 `list` | 否 | `null` | 声明可用工具，支持工具名映射 |
| `hooks` | `dict` | 否 | `null` | 生命周期 Hook 定义 |

### 引用配置参数（system.yaml / Agent YAML 中设置）

| 字段 | 类型 | 默认值 | 简要说明 |
|------|------|--------|---------|
| `path` | `string` | 无（必填） | Skill 目录路径（相对或绝对） |
| `platform` | `string` | `"Claude"` | 工具名映射平台标识 |
| `invocation-control` | `dict` | `{"allow-model": true, "allow-hook": true}` | 三态控制 LLM 可见性与加载策略（`true`/`false`/`"force-inject"`），以及 Hook 权限 |

### 全部 9 个 Hook 事件

| 事件名 | YAML 键值 | 是否需要 matcher | `tool_name` 值 |
|--------|-----------|----------------|---------------|
| 任务开始 | `TaskCreated` | 否 | `"task"` |
| 任务完成 | `TaskCompleted` | 否 | `"task"` |
| 任务失败 | `StopFailure` | 否 | `"task"` |
| 子任务开始 | `SubagentStart` | 可选（缺省为 `"*"`） | Worker Agent 名称 |
| 子任务完成 | `SubagentStop` | 可选（缺省为 `"*"`） | Worker Agent 名称 |
| 工具调用前 | `PreToolUse` | 是（工具名） | 实际工具函数名 |
| 工具调用后 | `PostToolUse` | 是（工具名） | 实际工具函数名 |
| 工具调用错误 | `PostToolUseFailure` | 是（工具名） | 实际工具函数名 |
| 停止前 | `Stop` | 否 | `"final_answer"` |

### Hook 脚本输出字段速查

| 字段 | 类型 | 说明 |
|------|------|------|
| `decision` | `string` | `"allow"` / `"block"` / `"modify"`（可选，缺省为 `"allow"`） |
| `modified_input` | `dict` | 合并覆盖工具输入（`modify` 时，只需写要改的字段） |
| `modified_response` | `dict` | 修改工具输出（`modify` 时） |
| `agent_context` | `string` | 注入 Agent 提示词的文本 |
| `user_message` | `string` | 展示给用户的消息 |
| `reason` | `string` | 用于说明拦截或处理原因；`block` 时建议填写，以便反馈更清晰 |
| `telemetry` | `dict` | 自定义遥测/调试数据 |
