<div align="center"><sub>
<a href="../../README.md">English</a> | 简体中文
</sub></div>

<h1 align="center">AgentLoom</h1>

<p align="center">
  <strong>用 YAML 构建多 Agent 应用，并通过可追溯证据的终端 Studio 运行和维护。</strong>
</p>

<p align="center">
  类型化 Worker、权限确认、断点恢复、Goal 显式完成和经审核的记忆，都以同一套运行时权威状态为准。
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

Supervisor 通过 `worker_agents` 显式选择 Worker；每个被选中的 Worker 会自动成为
以自身 `name` 和 `description` 命名、说明的可调用 Tool。简单 Worker 的 Tool 无参数，
任务来自自己的 YAML `task`，默认返回文本；复杂 Worker 使用 Draft 2020-12
`input_schema` / `output_schema` 声明由 Runtime 真正执行的结构化契约。

### Run 产出证据，不靠解析终端猜状态

每个已分配存储的 Run 都有独立的 `run_id`、manifest 和带版本的生命周期事件；
启用文件日志时还会生成有界日志，并保留审计记录和产物。逻辑任务使用稳定的
`task_id` 恢复。Studio、CLI JSON/JSONL 和 Python API 读取同一份权威状态。预检
拒绝发生在 Run 及其存储分配之前。

### 长任务有明确的完成责任人

Goal Mode 让根 Supervisor 跨 continuation 和 Worker 调用持续推进同一个目标。
只有根 Supervisor 能携带证据完成 Goal；启用 checkpoint 时，中断后可恢复目标和已完成工作。

### 记忆有审核边界

[Self-Learning v6](self_learning.md) 分开保存可搜索历史和经过证据门禁的记忆。
  Fact 和 Experience 候选需通过证据门禁及作用域审批策略；提升到 Project
  级别必须由人工发起。

### 扩展不会隐式获得权限

Skill 是按需加载的模型上下文包。Hook 是独立显式授权的 Runtime 代码。内置 Tool
的元数据可以在不导入实现的情况下发现；真正的 Tool、文件、Shell 和 MCP 权限
仍由 Agent 配置与权限策略决定。

## 快速开始

源码安装器会针对当前代码版本构建 Studio，并准备锁定依赖的 Python 环境：

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

Studio 是围绕 Application 设计的控制面，不是简单的日志查看器。

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
[Application Studio](../../studio/README.md)。

## 定义 Application

Application 将 Supervisor、Worker、提示词、可选 Tool 和输出放在一起：

```text
applications/release_review/
├── workflows/
│   ├── release_review_agent.yaml
│   └── worker_agents/
│       ├── api_reviewer.yaml
│       └── test_reviewer.yaml
├── config/
│   ├── system.yaml             # 可选的 Application 覆盖配置
│   └── prompts/reviewer.md     # 可选的 Agent system prompt 正文
├── skills/                     # 可选的私有 Skill
└── sysprompt/                  # 可选的 runtime 提示词模板
```

Supervisor 引用 Worker 定义：

基座执行参数只写在 `runtime_options` 中。旧顶层 smol 字段静默忽略，不转换、不拒绝。

```yaml
name: "release_review"
agent_runtime: "smolagents"
description: "Review an API release and its test evidence."
model_type: "powerful"
system_prompt:
  path: ../config/prompts/reviewer.md

worker_agents:
  - path: "applications/release_review/workflows/worker_agents/api_reviewer.yaml"
  - path: "applications/release_review/workflows/worker_agents/test_reviewer.yaml"

task: |
  Ask both Workers for evidence, reconcile conflicts, and return one release decision.

tools: []
runtime_options:
  max_steps: 12
goal:
  enabled: true
```

每个 Worker 声明 Supervisor 看到的接口。省略两个 schema 时 Tool 无参数，
Worker 执行自己的 YAML `task` 并返回文本；只有真实需要类型化 JSON 时才声明：

```yaml
name: "api_reviewer"
agent_runtime: "smolagents"
description: "Review API compatibility risks."
model_type: "fast"

input_schema:
  type: object
  properties:
    request:
      type: string
      description: "Release scope and API diff."
  required: [request]
  additionalProperties: false

output_schema:
  type: object
  properties:
    decision:
      type: string
      enum: [compatible, incompatible]
    findings:
      type: array
      items:
        type: string
  required: [decision, findings]
  additionalProperties: false

task: |
  Review the request, cite evidence, and return prioritized findings.

tools: []
worker_agents: []
runtime_options:
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
uv run --locked --extra smol --extra code loom run applications/ai_quality_analysis/workflows/code_review_agent.yaml --output-format json
uv run --locked --extra smol --extra code loom run applications/ai_quality_analysis/workflows/code_review_agent.yaml --output-format jsonl
```

在 Python 中调用 `execute_app()`，会返回包含输出、时间、结构化 Goal 状态和
`RunInfo` receipt 的 `ApplicationRunResult`：

```python
from agentloom.app.runner import execute_app

result = execute_app("applications/release_review/workflows/release_review_agent.yaml")
print(result.output, result.run.run_id)
```

框架源码直接放在 `src/app/`、`src/execution/`、`src/runtimes/`、`src/integrations/` 等职责模块中。
安装配置将 `src/` 映射为 Python 包名 `agentloom`，所以上面的导入实际加载
`src/app/runner.py`，磁盘上不需要再套一层 `agentloom` 目录。
先运行 `uv sync --python 3.12 --locked --all-groups` 安装项目，再调用 Python API。
旧的 `src.*` 导入和模块命令已移除；命令入口为 `loom` 和 `python -m agentloom`。
各模块职责与迁移方式见[架构与迁移地图](../specs/architecture-migration-inventory.md)。

存储分配后的失败携带同一 receipt；若预检阶段拒绝运行，会在分配运行存储前发出
`run.rejected` 事件。详见[结构化 Run API](run_observability.md)。

持久化 Schedule 复用相同的 Application 契约与 Run 生命周期。自动触发由单独的
前台服务负责，关闭 Studio 不会留下隐藏 daemon：

```bash
agentloom schedules --project /path/to/project serve
```

## 示例 Application

| Application | 展示能力 |
|---|---|
| `ai_quality_analysis` | 十二个专业 Worker 协作完成分阶段代码审查 |
| `unit_test_studio` | 通过确定性 Python 入口执行严格的 pytest 生成流程 |
| `repo_map` | 确定性预处理、自底向上 Agent 分析、批处理和进度持久化 |
| `goal_mode_validation` | Goal 显式完成、自动续跑和 checkpoint 恢复 |
| `self_learning_smoke` | Session 历史、记忆提案、证据和审核边界 |

## 文档

| 文档 | 内容 |
|---|---|
| [配置总览](config-overview.md) | 配置分层、合并与隔离 |
| [Agent 配置](agent_config.md) | Supervisor 和 Worker YAML 字段 |
| [Tool Catalog](tool_catalog.md) | 延迟实现加载、Toolset、元数据和扩展规则 |
| [Skills](skills_config.md) | 发现、按需激活与权限边界 |
| [Hooks](hooks.md) | 显式授权、事件、输入转换和失败语义 |
| [Goal Mode](goal_mode.md) | continuation、完成责任、恢复与调度 |
| [Checkpoint 与 Runtime 存储](checkpoint.md) | Run/task 身份、证据、恢复和保留策略 |
| [Self-Learning v6](self_learning.md) | 历史、候选、审核、审批与提升 |
| [结构化 Run API](run_observability.md) | Python receipt、类型化失败、JSON 与 JSONL |

## 开发与支持

```bash
# Framework
uv run pytest tests -q

# Studio
cd studio
bun test
bun run typecheck
```

- Issue：[github.com/linora-u/AgentLoom/issues](https://github.com/linora-u/AgentLoom/issues)
- 联系方式：[raine_walker@163.com](mailto:raine_walker@163.com?subject=AgentLoom%20Collaboration)
- Studio 来源与声明：[studio/upstream/README.md](../../studio/upstream/README.md)

如果 AgentLoom 对你的项目有帮助，欢迎 Star，或贡献一个边界清晰的 Application、
修复或验证用例。
