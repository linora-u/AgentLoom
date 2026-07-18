<div align="center"><sub>
<a href="../../README.md">English</a> | 简体中文
</sub></div>

<h1 align="center">AgentLoom</h1>

<p align="center">
  <strong>用 YAML 构建应用级 multi-agent 系统的框架。</strong>
</p>

<p align="center">
  通过 YAML 构建 multi-agent 应用，为不同子 Agent 选择合适模型，加载 Skills / MCP / 工具，并发处理重复任务，并从保存的状态恢复长任务。
</p>

<p align="center">
  <a href="https://github.com/linora-u/AgentLoom/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/linora-u/AgentLoom/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://www.python.org/downloads/"><img alt="python >=3.12" src="https://img.shields.io/badge/python-%3E%3D3.12-3776AB?logo=python&logoColor=white"></a>
  <a href="https://github.com/linora-u/AgentLoom/releases/tag/v1.0.1"><img alt="release v1.0.1" src="https://img.shields.io/badge/release-v1.0.1-007EC6"></a>
</p>

<p align="center">
  <img alt="AgentLoom application flow" src="../assets/agentloom-application-flow.svg">
</p>

---

## 3 分钟快速开始

AgentLoom 面向想构建可直接运行的 Agent 应用的开发者：YAML 定义 Agent，Worker 有明确调用契约，模型可以按 Agent 路由，运行过程有日志、checkpoint 状态和可选 UI 监控。

```bash
git clone <repo-url> AgentLoom
cd AgentLoom

uv sync
# 如果 PyPI 在你的网络环境中较慢或不可用：
# UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple uv sync

cp config/llm.example.yaml config/llm.yaml
# 编辑 config/llm.yaml，填入你的模型配置。

uv run loom run applications/ai_quality_analysis/workflows/code_review_agent.yaml
```

第一次运行后，你应该能看到：

- 带 Agent 名称、Task ID、Step 耗时和 Token 统计的结构化终端日志；
- `.agentloom/runs/<application_id>/<run_id>/` 下本次执行 attempt 的 manifest、有界 runtime log、Shell audit 和原始 artifacts；
- 开启 checkpoint 后位于 `.agentloom/checkpoints/<application_id>/<task_id>/` 的可恢复任务状态；
- 可通过 `uv run loom ui` 打开的 Web 可视化面板；
- 可通过 `uv run loom dashboard` 打开的终端任务监控面板。
- 可通过 [`agentloom-tui/`](../../agentloom-tui/README.md) 打开的交互式 Agent
  Builder 和全项目 Run 目录。

`run_id` 标识一次执行 attempt，resume 时会改变；`task_id` 标识同一个逻辑任务，resume 时保持不变。因此 resume 会写入新的 run 目录，同时继续使用原 checkpoint 和 `.agentloom/workspaces/agents/<application_id>/<agent_path>/tasks/<task_id>/`。`insights.md` 位于 agent workspace 根目录并跨 task 共享。

### 打开 AgentLoom TUI

```bash
./install
# 首次安装后打开一个新终端，然后在 AgentLoom 项目中直接运行：
agentloom
```

源码安装器沿用 OpenCode 的原生二进制安装方式：为当前平台构建
TypeScript/OpenTUI 单文件程序，安装到 `~/.agentloom/bin`，并在
`~/.agentloom/venv` 创建隔离、锁定的 Python 环境。安装阶段会自动查找或安装
`uv` 和 Bun；之后运行 `agentloom` 不需要激活 venv，也不用输入 `uv run`。
如果不希望修改 Shell 配置，可用 `./install --no-modify-path`；也可通过
`AGENTLOOM_INSTALL_DIR` 指定其他安装根目录。

Builder 只查看、暂存和校验 Agent YAML，`/apply` 才是显式写入动作。右侧目录会显示当前项目全部 Agent System 和 Run，包括仅创建但从未运行的定义、实时状态、Workers、事件、日志、产物和已保留结果。

TUI 创建的定时任务会持久保存；要让任务自动触发，需要在另一个终端显式运行前台调度服务：

```bash
agentloom schedules --project /path/to/project serve
```

## 以 Codex 为例快速创建多 Agent 应用

最快的方式是让 Codex 使用仓库自带的 framework skill。Codex 不只是生成 YAML：它也可以直接运行 AgentLoom 应用，检查 `.agentloom/` 下的 run 与 checkpoint 证据，并在 Worker 卡住或配置出错时继续修改应用。

可以这样对 Codex 说：

```text
先读取 agentloom-framework-skill/SKILL.md。

为下面目标创建一个名为 <app_name> 的 AgentLoom 应用：
<说明用户任务、输入、输出和验收标准>

要求：
- 所有文件放在 applications/<app_name>/ 下。
- 使用一个 Supervisor YAML 和至少两个 Worker YAML。
- 每个 Worker 必须定义 agent_function_schema，写清输入和输出。
- model_type 只能从 config/llm.yaml 中选择。
- 只有对这个应用有帮助时，才添加 Skills 或 MCP 配置。
- 编写应用 README，包含运行命令、Worker 分工、验证记录和已知限制。
- 运行应用，观察日志和 checkpoint，并修复发现的 YAML 或工具问题。
```

你可以让 Codex 帮你运行和观察，也可以自己运行：

```bash
uv run loom run applications/<app_name>/workflows/<app_name>_agent.yaml
```

运行过程中常用的查看命令：

```bash
uv run loom list-tasks
uv run loom dashboard

manifest=$(find .agentloom/runs -name manifest.json -type f -print | sort | tail -1)
run_dir=$(dirname "$manifest")
sed -n '1,160p' "$manifest"
tail -n 80 "$run_dir/logs/runtime.log"
tail -n 80 "$run_dir/audit/shell.jsonl"
```

如果你更习惯 Claude Code，也可以用同样的思路：先读 `agentloom-framework-skill/SKILL.md`，在 `applications/<app_name>/` 下创建应用，运行它，并总结成功和失败的地方。

核心配置可以先理解成四件事：

- `model_type`：这个 Agent 用哪一类已配置好的模型。
- `worker_agents`：Supervisor 可以调用哪些 Worker YAML。
- `agent_function_schema`：Worker 接收什么输入、返回什么输出。
- `skills` / `mcp_servers`：可选的额外知识和外部工具。

## 为什么选择 AgentLoom

很多 Agent 框架提供的是组件。AgentLoom 提供的是完整应用结构。

复杂 Agent 应用经常重复写同一批基础代码：Worker 注册、参数适配、运行入口、批量并发、日志、checkpoint 和安全控制。AgentLoom 把这些可复用部分放进 YAML 和运行时里。你的应用代码只需要关注领域任务：前处理、校验、产物落盘，以及真正的 Agent 分工。

实际收益是：

| 需求 | AgentLoom 的方式 |
|---|---|
| 构建完整 multi-agent 应用 | 用 YAML 定义 Supervisor、Workers、工具、Skills 和运行时行为。 |
| 为不同角色选择不同模型 | 每个 Agent 设置自己的 `model_type`，真实密钥和端点隔离在 `config/llm.yaml`。 |
| 把子 Agent 当工具调用 | Worker 定义 `agent_function_schema`，Supervisor 像调用普通工具一样调用它。 |
| 复用编程助手知识 | 加载 Claude-style `SKILL.md` 包，支持按需加载或全文预加载。 |
| 接入外部工具 | 注册本地 Python 工具、本地 `codex exec`，并通过 `mcp_servers` 接入 MCP servers。 |
| 处理重复性工作 | 用 Worker `concurrency` 和 `tool.batch(tasks)` 并发处理独立输入。 |
| 理解长任务状态 | 读取 run manifest、有界 runtime log、Shell audit、task checkpoint，并使用 `loom ui` 和 `loom dashboard`。 |

## 架构

<p align="center">
  <img alt="AgentLoom runtime architecture" src="../assets/agentloom-runtime-architecture.svg">
</p>

关键边界是：

- Worker Agent 定义自己的角色、工具、输入和输出契约。
- AgentLoom 把 Worker 转成带真实函数签名的可调用工具。
- Supervisor 协调 Workers 和普通工具，完成应用级任务。
- Python 仍然可以负责确定性的前处理、校验、缓存和产物落盘。

## 核心能力

| 能力 | 已实现内容 |
|---|---|
| 应用优先的 YAML | `loom run` 直接执行 Agent YAML；`loom create` 生成 Python 入口脚本；`run_app()` 可嵌入 Python。 |
| 按 Agent 路由模型 | 每个 Agent 选择 `model_type`；真实 provider、key、endpoint、retry 和参数在 `config/llm.yaml` 中管理。 |
| Agent-as-Tool 协作 | Worker 通过 `agent_function_schema` 导出为可调用工具，包含必填输入校验和字符串输出。 |
| Skills、MCP 与工具 | Agent 可加载 `SKILL.md` 包、本地 Python 函数、内置文件/Shell/搜索/Git 工具、本地 Codex 工具，并通过 `mcp_servers` 接入 MCP Client 工具。 |
| 并发重复任务 | Worker `concurrency: auto` 或固定并发配合 `.batch(tasks)` 处理大量独立输入。 |
| 状态与可观测性 | Rich 终端日志、有界的 per-run 文件日志、每步耗时、Token 统计、run manifest、checkpoint resume、Web UI 和 TUI dashboard。 |

## 示例应用

`applications/` 目录包含多个可运行示例，展示 AgentLoom 的推荐使用方式。

| 应用 | 构建内容 | 证明的框架能力 |
|---|---|---|
| `ai_quality_analysis` | 多维度代码审查应用。 | 12 个 Worker Agent、分阶段审查、直接 `loom run`、长时间代码库分析。 |
| `unit_test_studio` | Python pytest 生成工作流。 | 严格多步骤流水线、自定义入口脚本、函数接收、场景规划、测试生成、精炼、交付报告。 |
| `repo_map` | 仓库架构地图生成器。 | 确定性 Python 预处理 + Agent 架构分析、Bottom-Up 目录处理、`tool.batch(tasks)`、进度持久化。 |
| `codex_exec_demo` | 本地 Codex Exec 工具调用示例。 | 直接 `loom run`、普通 function tool 注册、`fixed_args` 固定 Codex 参数、结构化 `tool_call` 顺序调用。 |

运行默认代码审查示例：

```bash
uv run loom run applications/ai_quality_analysis/workflows/code_review_agent.yaml
```

用自定义 Python 入口运行 Repo Map：

```bash
uv run python applications/repo_map/repo_map_app.py /path/to/project \
  --output_dir /tmp/repo-map-output \
  --exclude_dirs vendor \
  --exclude_dirs build
```

为指定函数生成 pytest 测试：

```bash
uv run python applications/unit_test_studio/studio_runner.py \
  /path/to/your/project \
  "src/utils.py:parse_config,src/core.py:run_pipeline" \
  --output_dir tests/generated
```

运行本地 Codex Exec 工具示例：

```bash
uv run loom run applications/codex_exec_demo/workflows/use_codex_exec_demo.yaml
```

## 核心概念

### Supervisor / Worker

Supervisor Agent 负责协调任务。Worker Agent 专注其中一个部分。多 Agent 应用中，Supervisor 通过 `worker_agents` 引用 Workers，Workers 通过 `agent_function_schema` 暴露可调用契约。

### Agent 即工具

Worker Agent 可以导出为可调用工具。AgentLoom 会生成函数签名、校验必填输入、构造任务载荷并返回字符串结果。同一个 Worker 还可以暴露 `.batch(tasks)` 用于并发执行。

### Skills 与 Hooks

Skills 通过 Claude-style `SKILL.md` 包提供可复用知识或行为。Hooks 可以挂载在任务生命周期、子 Agent 生命周期、工具调用、会话、压缩、安装和配置变更等运行时事件上。

### 本地 Codex 工具

AgentLoom 内置 `src.tools.codex.codex_tool.codex`，用于把本机 `codex exec` 作为普通 function tool 暴露给 Agent。在 Agent YAML 的 `tools` 中通过 `module/function` 注册即可。使用 `fixed_args` 可以锁定 `prompt`、`cwd`、`sandbox`、`search` 等输入；被固定的参数不会出现在 LLM 可见的 tool schema 中。

使用前需要确保 `codex` 在 `PATH` 中，并且 `codex login status` 成功。

## CLI 命令

| 命令 | 用途 |
|---|---|
| `uv run loom run <workflow>` | 运行一个 AgentLoom 应用。 |
| `uv run loom create <workflow>` | 为应用生成 Python 入口脚本。 |
| `uv run loom ui` | 打开 Web 可视化面板。 |
| `uv run loom dashboard` | 打开终端任务监控面板。 |
| `uv run loom list-tasks` | 列出可恢复的 checkpoint 任务。 |
| `uv run loom clean-tasks` | 清理旧 checkpoint 数据。 |
| `uv run loom clean-runtime` | 按配置的 retention 清理已结束 run 与 raw artifacts。 |
| `uv run loom migrate-runtime --dry-run` | 只预览旧 checkpoint 候选和未分域的 `.runtime`，不改磁盘状态。 |
| `uv run loom migrate-runtime --apply` | 迁移 checkpoint、归档 `.logs`，并将 `.runtime` 原子保存在 `.agentloom/workspaces/legacy-unscoped/`。 |

## 文档

| 文档 | 说明 |
|---|---|
| [配置体系总览](config-overview.md) | 配置层级、合并规则和模型配置隔离机制。 |
| [Agent 配置](agent_config.md) | Supervisor/Worker 字段、工具、模型选择、工作流和 Skills。 |
| [LLM 配置](llm_config.md) | 模型类型、Provider 设置、继承、重试和 Prompt 缓存。 |
| [系统配置](system_config.md) | 运行时设置、权限、日志、执行环境和工具系统。 |
| [Skills 配置](skills_config.md) | Skill 包格式、加载方式、运行时策略和内置 Skills。 |
| [Hooks 参考](hooks.md) | 生命周期事件、Hook 类型、匹配规则和执行行为。 |
| [Checkpoint 断点恢复](checkpoint.md) | Checkpoint 布局、恢复行为和长任务恢复机制。 |

## AgentLoom Framework Skill

仓库根目录提供一个给 AI 编程助手使用的框架级 Skill：`agentloom-framework-skill/`。

当你想让 Codex、Claude Code 或其他编程助手基于 AgentLoom 开发，而不是从零猜框架结构时，让助手先读取 `agentloom-framework-skill/SKILL.md`，再描述你要构建的应用能力。

这个 Skill 是开发辅助，不放在 `skills/` 运行时自动发现目录下，因此不是运行 AgentLoom 应用的必需依赖。

## 支持与参与

AgentLoom 是面向复杂 Agent 自动化任务的实用框架。欢迎提交 Issue、功能建议、文档改进和新的示例应用。

- 提 Issue：[GitHub Issues](https://github.com/linora-u/AgentLoom/issues)
- 联系方式：[raine_walker@163.com](mailto:raine_walker@163.com?subject=AgentLoom%20Collaboration)
- 如果这个项目对你有帮助，欢迎给一个 GitHub Star，让更多人看到它。
