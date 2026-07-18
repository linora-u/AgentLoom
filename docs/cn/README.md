<div align="center"><sub>
<a href="../../README.md">English</a> | 简体中文
</sub></div>

<h1 align="center">AgentLoom</h1>

<p align="center">
  <strong>创建、运行并查看由 YAML 定义的多 Agent 应用。</strong>
</p>

<p align="center">
  在终端 Builder 中通过对话设计 Agent 系统，使用 AgentLoom Python Runtime 运行，并在同一个项目视图里查看每个 Agent、Worker 和 Run。
</p>

<p align="center">
  <a href="https://github.com/linora-u/AgentLoom/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/linora-u/AgentLoom/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://www.python.org/downloads/"><img alt="python >=3.12" src="https://img.shields.io/badge/python-%3E%3D3.12-3776AB?logo=python&logoColor=white"></a>
  <a href="https://github.com/linora-u/AgentLoom/releases/tag/v1.0.1"><img alt="release v1.0.1" src="https://img.shields.io/badge/release-v1.0.1-007EC6"></a>
</p>

<p align="center">
  <img alt="AgentLoom application flow" src="../assets/agentloom-application-flow.svg">
</p>

## 安装

推荐从源码安装脚本开始。它会同时准备 TUI、Python Runtime 和所需依赖，不需要手工搭建多套环境。

```bash
git clone https://github.com/linora-u/AgentLoom.git
cd AgentLoom
./install
```

安装完成后打开一个新终端，在仓库根目录验证命令：

```bash
agentloom --version
agentloom --snapshot
```

安装脚本会：

- 通过官方安装器补齐缺少的 `uv` 和 Bun；
- 在 `~/.agentloom/venv` 创建锁定的 Python 环境；
- 构建当前平台的 TypeScript/OpenTUI 原生程序；
- 将 `agentloom` 和 `agentloom-tui` 安装到 `~/.agentloom/bin`；
- 在存在可写 Shell 配置时，把该目录加入 `PATH`。

源码安装脚本目前面向 macOS 和 Linux Shell。它需要 Git 和 Bash；仅在缺少 `uv` 或 Bun 时需要 `curl`。

需要自定义安装目录或不修改 `PATH` 时，可以使用：

```bash
AGENTLOOM_INSTALL_DIR="$HOME/tools/agentloom" ./install
./install --no-modify-path
```

### 配置模型

使用 TUI 对话或运行 Agent 前，先复制模型配置模板：

```bash
cp config/llm.example.yaml config/llm.yaml
```

编辑 `config/llm.yaml` 并替换占位内容。不要提交这个文件；它包含凭证，已经被 Git 忽略。

```yaml
model:
  default_model_type: powerful
  powerful:
    model: "openai/<model-id>"
    api_key: "<api-key>"
    base_url: "https://<openai-compatible-endpoint>"  # OpenAI 官方端点可省略
    tool_choice: "auto"
```

Agent 任务使用 AgentLoom 原有模型运行时。TUI 对话目前要求 OpenAI-compatible Chat Completions 接口；创建 YAML 还要求模型支持流式响应和原生 tool/function calling。

## 从 TUI 开始

在需要查看的项目中运行 `agentloom`，也可以显式指定项目：

```bash
cd /path/to/AgentLoom
agentloom

# 查看另一个 AgentLoom 项目
agentloom --project /path/to/project
```

TUI 是 Agent Builder 和项目控制面。它可以讨论设计、检查已有定义、暂存并校验 Agent YAML，以及读取标准运行状态。

它不会执行 Shell、Git 或 Agent 任务。是否写入 YAML 由你决定，Agent 也需要在另一个终端中运行。

### 1. 描述 Agent 系统

按 `Enter` 发送需求。一个有效的初始描述应写清应用、角色、输入、输出和验收标准：

```text
创建一个名为 release_review 的应用。
使用一个 Supervisor 和两个 Worker，分别负责 API 审查和测试审查。
模型类型从 config/llm.yaml 中选择。
写入前先展示准备创建的文件和调用契约。
```

Builder 只读取需要的项目上下文，在内存中暂存草稿并校验 YAML。可以继续对话，直到拓扑和调用契约符合预期。

### 2. 检查并写入 YAML

普通对话不会修改文件。输入 `/apply` 才会写入当前通过校验的草稿。这个命令只写 YAML，不会启动 Agent。

使用 `/models` 打开模型选择器，或用 `/model <type>` 直接切换：

```text
/models
/model summary
/apply
```

### 3. 运行 Agent

打开另一个终端，运行 Builder 显示的 Supervisor 路径：

```bash
uv run loom run applications/<app>/workflows/<agent>.yaml
```

也可以生成 Python 入口：

```bash
uv run loom create applications/<app>/workflows/<agent>.yaml \
  -o applications/<app>/<app>_app.py
uv run python applications/<app>/<app>_app.py
```

Agent 运行时可以保持 TUI 打开。`loom run` 和自动生成的入口都会发布标准生命周期，供整个项目查看状态。

直接调用底层 Agent `.run()` 会绕过该生命周期，任何 UI 都无法从中还原完整 Run。

### 4. 查看 Agent 与 Run

按 `Ctrl+P` 或 `Tab` 搜索 Application、Agent、Worker、Run、Skill、Schedule 和命令。可以使用键盘或鼠标选择条目。

详情页会区分从未运行、运行中、完成、失败、崩溃、中断和未知状态。它展示拓扑、校验、Worker 状态、结果和产物路径，不会把完整日志铺满界面。

选择失败 Run 后按 `a`，TUI 助手会分析经过清洗且严格限长的失败上下文。完整日志仍保存在磁盘，详情页会显示路径。

使用 `F6` 切换焦点。详情页聚焦后支持方向键、`PgUp` / `PgDn`、`Home` / `End` 和鼠标滚轮。按 `Esc` 或 `b` 返回。

| 操作 | 命令 / 按键 |
|---|---|
| 发送对话 | `Enter` |
| 浏览项目 | `Ctrl+P` 或 `Tab` |
| 选择模型 | `/models` 或 `/model <type>` |
| 写入已校验草稿 | `/apply` |
| 刷新完整索引 | `/refresh` 或在详情页按 `r` |
| 切换对话/详情焦点 | `F6` |
| 分析选中的失败 Run | `a` |

TUI 架构与开发说明见 [agentloom-tui/README.md](../../agentloom-tui/README.md)。

### 5. 显式运行调度服务

TUI 可以创建和管理持久化 Schedule。自动触发由独立的前台服务负责，因此关闭 TUI 后不会留下隐藏 daemon。

```bash
agentloom schedules --project /path/to/project serve
```

## 运行已有 Agent

如果想先验证框架而不创建新应用，可以运行仓库自带的代码审查示例：

```bash
uv run loom run applications/ai_quality_analysis/workflows/code_review_agent.yaml
```

一次运行会在 `.agentloom/runs/<application_id>/<run_id>/` 下生成 receipt。开启 checkpoint 后，可恢复状态位于 `.agentloom/checkpoints/<application_id>/<task_id>/`。

`run_id` 标识一次 attempt，resume 后会改变；`task_id` 标识逻辑任务，resume 时保持不变。

## AgentLoom 如何工作

AgentLoom 把多 Agent 系统视为一个应用，而不是一组松散的 Prompt。

| 概念 | 职责 |
|---|---|
| Supervisor | 拆解应用任务，并协调 Worker 和工具。 |
| Worker | 负责一个专门角色，并暴露 typed callable contract。 |
| Agent YAML | 定义角色、工作流、模型类型、工具、Skill、Worker 和运行策略。 |
| Python Runtime | 负责模型路由、Worker 工具生成、并发、日志、checkpoint 和产物。 |

编排逻辑可以复用，确定性的前处理、校验、缓存和结果写入仍然由普通 Python 代码完成。

### 核心能力

| 需求 | AgentLoom 提供 |
|---|---|
| 从配置构建应用 | 使用 `loom run` 直接执行 YAML，并可生成 Python 入口。 |
| 为不同角色选择模型 | 每个 Agent 设置 `model_type`，凭证隔离在 `config/llm.yaml`。 |
| 像工具一样调用 Agent | Worker 的 `agent_function_schema` 会转换成带校验的 callable function。 |
| 处理重复输入 | 固定或自动 Worker 并发，并支持 `.batch(tasks)`。 |
| 扩展 Agent | 内置工具、本地 Python 函数、MCP Server、Claude-style Skill 和显式 Hook。 |
| 恢复并检查任务 | Run receipt、有界日志、Shell audit、checkpoint resume、结构化事件、Web UI、dashboard 和 TUI。 |

## 手工创建应用

一个应用把 Supervisor、Worker、Prompt、可选工具和输出放在一起：

```text
applications/release_review/
├── workflows/
│   ├── release_review_agent.yaml
│   └── worker_agents/
│       ├── api_reviewer.yaml
│       └── test_reviewer.yaml
├── config/system.yaml          # 可选的应用级覆盖
├── sysprompt/                  # 可选 Prompt 模板
└── README.md
```

最小 Supervisor 会引用 Worker YAML：

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

每个 Worker 定义 Supervisor 可见的调用契约：

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

确认真实 `model_type` 存在于 `config/llm.yaml`，然后运行 Supervisor：

```bash
uv run loom run applications/release_review/workflows/release_review_agent.yaml
```

完整字段见 [Agent 配置参考](agent_config.md)。

## 使用编程助手创建应用

仓库提供 `agentloom-framework-skill/`，供 Codex、Claude Code 和其他支持 Skill 的编程助手使用。

创建文件前，先要求助手读取该 Skill：

```text
先读取 agentloom-framework-skill/SKILL.md。

为下面目标创建一个名为 <app_name> 的 AgentLoom 应用：
<任务、输入、输出和验收标准>

在 applications/<app_name>/ 下创建一个 Supervisor 和必要的 Worker。
只使用 config/llm.yaml 中存在的模型类型。
用 agent_function_schema 定义每个 Worker 的输入和输出。
校验 YAML、运行应用、检查 .agentloom 证据，
并报告通过、失败和仍有限制的内容。
```

Framework Skill 是提供给编程助手的开发说明。它不是 Runtime Skill，也不会被 Agent 应用自动加载。

## 架构

<p align="center">
  <img alt="AgentLoom runtime architecture" src="../assets/agentloom-runtime-architecture.svg">
</p>

Worker Agent 会转换成生成的工具。Supervisor 可以同时调用 Worker，以及普通 Python、MCP、文件、Shell、搜索、Git、Codex 或 Skill 工具。

Runtime 负责执行身份和证据。TUI 与其他观察端读取这套标准状态，而不是根据进程输出猜测结果。

## 示例应用

| 应用 | 展示能力 |
|---|---|
| `ai_quality_analysis` | 十二个专门 Worker 协作完成分阶段代码审查。 |
| `unit_test_studio` | 使用自定义 Python 入口的严格 pytest 生成流水线。 |
| `repo_map` | 确定性前处理、Bottom-Up Agent 分析、批处理和进度持久化。 |
| `codex_exec_demo` | 将本地 `codex exec` 注册为带固定参数的普通 Agent 工具。 |

```bash
# 生成仓库架构地图
uv run python applications/repo_map/repo_map_app.py /path/to/project \
  --output_dir /tmp/repo-map-output

# 为指定函数生成 pytest
uv run python applications/unit_test_studio/studio_runner.py \
  /path/to/project "src/utils.py:parse_config" \
  --output_dir tests/generated

# 本地 Codex 工具示例
uv run loom run applications/codex_exec_demo/workflows/use_codex_exec_demo.yaml
```

## CLI 参考

| 命令 | 用途 |
|---|---|
| `uv run loom run <workflow>` | 运行应用。 |
| `uv run loom run <workflow> --output-format jsonl` | 输出带版本的生命周期事件流。 |
| `uv run loom create <workflow>` | 生成 Python 入口。 |
| `uv run loom list-tasks` | 列出可恢复任务。 |
| `uv run loom dashboard` | 打开终端任务 dashboard。 |
| `uv run loom ui` | 打开 Web 可视化面板。 |
| `uv run loom schedules ...` | 管理持久化 Schedule。 |
| `uv run loom sessions ...` | 搜索和维护执行历史。 |
| `uv run loom learn review ...` | 触发 Application 或 Project review。 |
| `uv run loom reviews ...` | 查看、应用或回滚 review decision。 |
| `uv run loom memory ...` | 管理 Curated Memory。 |
| `uv run loom feedback submit <run_id> ...` | 为 Run 添加结果反馈。 |
| `uv run loom clean-tasks` | 删除保留的 checkpoint 任务。 |
| `uv run loom clean-runtime` | 执行已配置的 Run 和 artifact retention。 |
| `uv run loom migrate-runtime --dry-run\|--apply` | 预览或执行旧 Runtime 迁移。 |

使用 `uv run loom <command> --help` 查看完整命令契约。

## 文档

| 文档 | 内容 |
|---|---|
| [配置体系总览](config-overview.md) | 配置层级、合并和隔离。 |
| [Agent 配置](agent_config.md) | Supervisor 和 Worker YAML 字段。 |
| [LLM 配置](llm_config.md) | 模型类型、Provider、重试和缓存。 |
| [系统配置](system_config.md) | Runtime、权限、执行环境和工具。 |
| [Skill 配置](skills_config.md) | Skill 包、加载方式和策略。 |
| [Hook 参考](hooks.md) | 直接 Hook、Bundle、事件和执行方式。 |
| [Checkpoint 恢复](checkpoint.md) | Run 证据、checkpoint 布局和恢复。 |
| [Self-learning v6](self_learning.md) | History、candidate、review、审批和 promotion。 |
| [结构化 Run API](run_observability.md) | Python receipt、typed error、event sink 和 JSONL。 |

## 开发与支持

```bash
# Framework 测试
uv run pytest tests -q

# TUI 测试
cd agentloom-tui
bun test
bun run typecheck
```

- Issue：[github.com/linora-u/AgentLoom/issues](https://github.com/linora-u/AgentLoom/issues)
- 联系方式：[raine_walker@163.com](mailto:raine_walker@163.com?subject=AgentLoom%20Collaboration)
- TUI 上游来源与许可证：[agentloom-tui/upstream/README.md](../../agentloom-tui/upstream/README.md)

如果 AgentLoom 对你的项目有帮助，欢迎 Star 仓库，或贡献一个范围明确的示例、修复或文档改进。
