<div align="center"><sub>
<a href="../../README.md">English</a> | 简体中文
</sub></div>

<h1 align="center">AgentLoom</h1>

<p align="center">
  <strong>用 YAML 构建多 Agent 应用，并通过可追溯证据的终端 Studio 运行和维护。</strong>
</p>

<p align="center">
  类型化 Worker、权限确认、断点恢复、Goal 预算和经审核的记忆，都以同一套运行时权威状态为准。
</p>

<p align="center">
  <a href="https://github.com/linora-u/AgentLoom/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/linora-u/AgentLoom/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://www.python.org/downloads/"><img alt="python >=3.12" src="https://img.shields.io/badge/python-%3E%3D3.12-3776AB?logo=python&logoColor=white"></a>
  <img alt="version 1.0.1" src="https://img.shields.io/badge/version-1.0.1-007EC6">
</p>

<p align="center">
  <img alt="真实终端中运行的 AgentLoom Application Studio" src="../assets/agentloom-studio.svg">
</p>

<p align="center"><sub>开启 reduced-motion 后录制的真实终端会话。Studio 从当前项目读取 Application、Skill、校验状态、Run 和命令。</sub></p>

AgentLoom 把多 Agent 系统当成一个**有执行契约的 Application**。YAML 定义
Supervisor、类型化 Worker、模型、Tool、Skill、Hook、权限和 Runtime 策略。
Application Studio 可以修改这份契约、展示 Diff、为副作用请求授权、发起运行、
读取结构化证据，并根据失败证据继续修复。

## 核心价值

### Worker 会成为类型化工具

Worker 通过 `agent_function_schema` 声明接口，Runtime 将它转换成 Supervisor
可调用、带参数校验的工具。不同 Worker 可以使用不同模型和工具、并发执行，同时保持
稳定的输入输出契约，不依赖提示词里的口头约定。

### Run 产出证据，不靠解析终端猜状态

每个已分配存储的 Run 都有独立的 `run_id`、manifest 和带版本的生命周期事件；
启用文件日志时还会生成有界日志，并保留审计记录和产物。逻辑任务使用稳定的
`task_id` 恢复。TUI、CLI JSON/JSONL 和 Python API 读取同一份权威状态。预检
拒绝发生在 Run 及其存储分配之前。

### 长任务有明确的完成责任人

Goal Mode 让根 Supervisor 跨 continuation 和 Worker 调用持续推进同一个目标。
只有根 Supervisor 能携带证据完成 Goal。可选 token 预算覆盖整棵 Agent 树；启用
checkpoint 时，达到预算后进入 `budget_limited` 并保留恢复状态。

### 记忆有审核边界

AgentLoom 把两类需求分开处理：

- [`agent-recall-with-files`](../../skills/agent-recall-with-files/SKILL.md)
  使用运行时约定的工作区文件保存轻量的任务恢复信息和 Agent 局部经验。可选
  Hook Bundle 会注入最近记录，并按新鲜度提醒更新。
- [Self-Learning v6](self_learning.md) 分开保存可搜索历史和经过证据门禁的记忆。
  Fact 和 Experience 候选需通过证据门禁及作用域审批策略；提升到 Project
  级别必须由人工发起。

### 扩展不会隐式获得权限

Skill 是按需加载的模型上下文包。Hook 是独立显式授权的 Runtime 代码。内置 Tool
的元数据可以在不导入实现的情况下发现；真正的 Tool、文件、Shell 和 MCP 权限
仍由 Agent 配置与权限策略决定。

## 快速开始

源码安装器会针对当前代码版本构建 TUI，并准备锁定依赖的 Python 环境：

```bash
git clone https://github.com/linora-u/AgentLoom.git
cd AgentLoom
./install
```

目前支持 macOS 和 Linux Shell，需要 Git 与 Bash。安装器会通过官方安装器补齐
`uv` 和 Bun，再把配套运行环境安装到 `~/.agentloom`。打开新终端后验证：

```bash
agentloom --version
agentloom --snapshot
```

创建本地模型配置：

```bash
cp config/llm.example.yaml config/llm.yaml
```

```yaml
model:
  default_model_type: powerful
  powerful:
    model: "openai/<model-id>"
    api_key: "<api-key>"
    base_url: "https://<openai-compatible-endpoint>"  # OpenAI 可省略
    tool_choice: "auto"
  fast:
    model: "openai/<fast-model-id>"
    api_key: "<api-key>"
    base_url: "https://<openai-compatible-endpoint>"
    tool_choice: "auto"
```

`config/llm.yaml` 已被 Git 忽略，是 Studio 和 Application Agent 共用的唯一模型
目录。在任意 AgentLoom 项目中启动 Studio：

```bash
agentloom

# 或查看另一个项目
agentloom --project /path/to/project
```

可以从一条包含角色和验收条件的需求开始：

```text
创建一个名为 release_review 的 Application。
使用一个 Supervisor，以及负责 API 审查和测试审查的两个 Worker。
模型类型只能从 config/llm.yaml 选择。
完成校验后，在第一次真实 Run 前向我确认。
```

Studio 直接修改当前 Application，并展示每次 Diff。它按照下面的闭环工作：

```text
检查 → 修改 → 校验 → 请求 Run 权限 → 执行 → 检查证据 → 修复
```

如果用户没有批准执行，Studio 会明确报告“配置已校验，未运行”，不会把静态校验
包装成运行成功。

## Application Studio

TUI 是围绕 Application 设计的控制面，不是简单的日志查看器。

- **Application 工作区：**查看 Effective Config、Supervisor/Worker 拓扑、配置
  来源、模型、Tool、Skill、Hook、MCP、权限和校验结果。
- **Agent Loop：**检查项目、修改当前 Application、展示 Tool 与 Diff 卡片、提出
  业务问题、执行冒烟运行，并诊断失败 Run。
- **权限边界：**默认 `Application Only` 允许读取项目、写入当前 Application。
  Shell、全局文件、其他 Application 和未知新路径需要可见的权限确认。
  `Full Access` 是显式 Session 开关，退出后自动重置。
- **Session 连续性：**切换 Application 会保留 Studio 对话记忆；`/new` 开始新
  对话，`/compact` 在保留已完成文件修改和持久历史的前提下压缩当前上下文。
- **Revision 安全：**每个 Run 固定 Application 内容哈希。后续修改只改变
  Working Revision，不会热切换正在执行的 Running Revision。
- **Run 诊断：**摘要展示终态、Goal 进度、token 用量、完成证据和恢复操作，默认
  不展示全部底层事件。

| 操作 | 按键 / 命令 |
|---|---|
| 发送 Studio 消息 | `Enter` |
| 搜索 Application、Agent、Skill、Run、模型、权限和命令 | `Ctrl+X` |
| 开始新对话 | `/new` |
| 压缩当前对话 | `/compact` |
| 选择 Studio 模型 | `/models` |
| 刷新项目索引 | `/refresh` |
| 诊断当前选中的失败 Run | `a` |
| 关闭详情、拒绝决策或中断 Agent Loop | `Esc` |

界面行为、架构、更新、调度和开发命令见
[Application Studio](../../agentloom-tui/README.md)。

## 定义 Application

Application 将 Supervisor、Worker、提示词、可选 Tool 和输出放在一起：

```text
applications/release_review/
├── workflows/
│   ├── release_review_agent.yaml
│   └── worker_agents/
│       ├── api_reviewer.yaml
│       └── test_reviewer.yaml
├── config/system.yaml          # 可选的 Application 覆盖配置
├── skills/                     # 可选的私有 Skill
└── sysprompt/                  # 可选的提示词模板
```

Supervisor 引用 Worker 定义：

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
max_steps: 12
goal:
  enabled: true
  token_budget: 120000
```

每个 Worker 声明 Supervisor 看到的接口：

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
max_steps: 8
```

直接运行 Supervisor：

```bash
uv run loom run applications/release_review/workflows/release_review_agent.yaml
```

也可以让支持 Skill 的编程助手先读取
[`agentloom-framework-skill/SKILL.md`](../../agentloom-framework-skill/SKILL.md)，
再创建文件、校验配置、运行 Application，并检查 `.agentloom` 证据。

## Runtime 模型

<p align="center">
  <img alt="AgentLoom Runtime 架构" src="../assets/agentloom-runtime-architecture.svg">
</p>

Python Runtime 负责模型路由、Worker Tool 生成、并发、权限、Hook、checkpoint 和
证据。确定性的预处理、校验、缓存和输出仍然使用普通 Python 代码。

Runtime 存储将执行尝试和可恢复任务分开：

```text
.agentloom/
├── runs/<application_id>/<run_id>/
│   ├── manifest.json
│   ├── logs/runtime.log
│   ├── audit/
│   └── artifacts/
├── checkpoints/<application_id>/<task_id>/
│   ├── checkpoint.json
│   ├── workers/<worker>/calls/<index>/checkpoint.json
│   ├── todos.json
│   ├── goal.json
│   ├── context_store/
│   └── file-history/
└── workspaces/agents/<application_id>/<agent_path>/
    ├── insights.md
    └── tasks/<task_id>/{context.md,trace.md}
```

Goal、Todo、context-store、file-history 和 Recall 文件只会在对应能力已配置或被使用
时出现。

## 运行与集成

无需新建 Application，即可运行仓库内置的代码审查 Application：

```bash
uv run loom run applications/ai_quality_analysis/workflows/code_review_agent.yaml
```

其他程序负责调度时，使用机器可读的生命周期事件：

```bash
uv run loom run <workflow> --output-format json
uv run loom run <workflow> --output-format jsonl
```

在 Python 中调用 `execute_app()`，会返回包含输出、时间、结构化 Goal 状态和
`RunInfo` receipt 的 `ApplicationRunResult`：

```python
from src.runner import execute_app

result = execute_app("applications/release_review/workflows/release_review_agent.yaml")
print(result.output, result.run.run_id)
```

存储分配后的失败携带同一 receipt；若预检阶段拒绝运行，会在分配运行存储前发出
`run.rejected` 事件。详见[结构化 Run API](run_observability.md)。

持久化 Schedule 复用相同的 Application 契约与 Run 生命周期。自动触发由单独的
前台服务负责，关闭 TUI 不会留下隐藏 daemon：

```bash
agentloom schedules --project /path/to/project serve
```

## 示例 Application

| Application | 展示能力 |
|---|---|
| `ai_quality_analysis` | 十二个专业 Worker 协作完成分阶段代码审查 |
| `unit_test_studio` | 通过确定性 Python 入口执行严格的 pytest 生成流程 |
| `repo_map` | 确定性预处理、自底向上 Agent 分析、批处理和进度持久化 |
| `codex_exec_demo` | 将本地 `codex exec` 作为带固定参数的普通 Agent Tool |
| `goal_mode_validation` | Goal 显式完成、预算统计和可恢复终态 |
| `self_learning_smoke` | Session 历史、记忆提案、证据和审核边界 |

## 文档

| 文档 | 内容 |
|---|---|
| [配置总览](config-overview.md) | 配置分层、合并与隔离 |
| [Agent 配置](agent_config.md) | Supervisor 和 Worker YAML 字段 |
| [Tool Catalog](tool_catalog.md) | 延迟实现加载、Toolset、元数据和扩展规则 |
| [Skills](skills_config.md) | 发现、按需激活与权限边界 |
| [Hooks](hooks.md) | 显式授权、事件、输入转换和失败语义 |
| [Goal Mode](goal_mode.md) | continuation、完成责任、预算、恢复与调度 |
| [Checkpoint 与 Runtime 存储](checkpoint.md) | Run/task 身份、证据、恢复和保留策略 |
| [Self-Learning v6](self_learning.md) | 历史、候选、审核、审批与提升 |
| [结构化 Run API](run_observability.md) | Python receipt、类型化失败、JSON 与 JSONL |

## 开发与支持

```bash
# Framework
uv run pytest tests -q

# TUI
cd agentloom-tui
bun test
bun run typecheck
```

- Issue：[github.com/linora-u/AgentLoom/issues](https://github.com/linora-u/AgentLoom/issues)
- 联系方式：[raine_walker@163.com](mailto:raine_walker@163.com?subject=AgentLoom%20Collaboration)
- TUI 来源与声明：[agentloom-tui/upstream/README.md](../../agentloom-tui/upstream/README.md)

如果 AgentLoom 对你的项目有帮助，欢迎 Star，或贡献一个边界清晰的 Application、
修复或验证用例。
