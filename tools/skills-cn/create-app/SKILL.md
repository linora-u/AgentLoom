---
name: create-app
description: "Use when creating a new AgentLoom-compatible Application scaffold (single-agent or supervisor+workers), including workflow YAML, worker configs, entry scripts, custom tools, and post-generation validation."
---

# 创建 AgentLoom Application

AgentLoom Application 脚手架生成 Skill。可被 **Copilot Codex / Claude Code / AgentLoom Agent** 调用，根据用户需求自动（或交互式）生成完整的 Application 目录结构和配置文件。

> **📖 配套参考文档**（按需查阅）：
> - [references/quick-reference.md](./references/quick-reference.md) — 全量预定义 Tool 表、model_type/execution_env 选型规则、配置约束清单
> - [references/full-example.md](./references/full-example.md) — **完整端到端示例**：`code_review` Application 从需求到文件生成的完整流程 + 单 Agent 模式示例
> - [references/templates.md](./references/templates.md) — 第三阶段所有文件的完整生成模板
> - [references/troubleshooting.md](./references/troubleshooting.md) — 常见配置错误排查
> - [references/agent-yaml-schema.json](./references/agent-yaml-schema.json) — Agent YAML JSON Schema，用于 IDE 补全和校验
>
> 参考路径相对于当前 Skill 根目录。

## 适用场景

- 用户说"帮我创建一个新的 Application"或"我想搭建一个新的 Agent 工作流"
- 用户有一个具体任务想通过多 Agent 协作来完成，但不知道如何配置
- 用户想快速搭建一个标准的 Supervisor + N Worker 工作流
- 用户只需要一个独立运行的 Agent（**单 Agent 模式**，不需要 Supervisor）

## 不适用场景

- 修改已有 Application 的配置（直接编辑对应 YAML 即可）
- 只需要一个单独的 Tool 函数，不需要 Agent 编排
- 非 AgentLoom 框架的项目

## 执行策略

本 Skill 支持两种执行模式：

| 环境 | 策略 |
|------|------|
| **交互式**（VS Code Copilot Chat / 终端对话） | 先补全缺失信息，然后确认计划；收到确认后再生成文件 |
| **自主式**（Copilot Codex / Claude Code / 批处理） | 从用户 Prompt 中提取信息；无法提问时，使用"可推断信息 + 默认策略"直接生成，并在输出中明确标注假设，不阻塞执行 |

> **执行原则**：
> - 交互式：严格遵循"先确认计划，再生成文件"。
> - 自主式：没有交互通道时，不等待确认——直接生成并附带"假设列表"。

## 路径策略（统一规则）

- 将"项目目录"视为本次任务的工作根目录。
- 如果用户提供了路径，则以该路径作为项目根目录。
- 如果用户只提供了项目名称，先搜索可访问的工作区中是否有同名或最近名的目录。
- 如果存在多个候选目录，优先选择包含项目标识文件（如 `config/llm.yaml`、`pyproject.toml`、`package.json`、`.git`、`src`）的目录。
- 所有输入/输出文件路径均相对于项目根目录解析。
- 修改文件前确认目标文件存在；创建新文件前确认父目录可创建。

> **AI 调用者注意**：项目根目录和 Skill 根目录都应在运行时动态获取（例如通过工作区路径或用户上下文），不应在脚本中硬编码。对于需要路径的脚本，直接将路径作为参数传入。

## 根目录前置条件（必须先满足）

- 在执行任何检测/更新命令之前，首先切换到 AgentLoom 根目录。
- 根目录判定标准：优先考虑 `config/llm.yaml` 的上一级作为项目根目录，这个最好，因为 Application 目录下也可能有自己的 `config/system.yaml` 等配置，容易混淆。
- 如果当前目录不满足条件，先切换到 AgentLoom 根目录，再进行后续阶段（需求收集、计划确认、文件生成、配置校验、执行）。

## 两种模式

| 模式 | 何时使用 | 需要什么 |
|------|----------|----------|
| **Supervisor + N Worker** | 任务可拆分为多个阶段，需要分工协作 | Supervisor YAML + N 个 Worker YAML |
| **单 Agent** | 任务足够简单，一个 Agent 即可完成 | 仅一个 YAML（无 `worker_agents`，无 `agent_function_schema`） |

> **决策标准**：如果用户描述只涉及一个职责/一个步骤，优先推荐单 Agent 模式。

---

## 第一阶段：需求收集

**必须在生成任何文件之前完成需求收集。**

### 信息提取清单

从用户的 Prompt 或对话中提取以下信息。**加粗项为必填**；其余有默认值，可跳过：

| # | 信息项 | 必填 | 默认值 | 确定方式 |
|---|--------|------|--------|----------|
| 1 | **Application 名称** | ✅ | — | 必须由用户提供；用作目录名；建议小写+下划线 |
| 2 | **一句话功能描述** | ✅ | — | 必须由用户提供；用于 Agent 描述 |
| 3 | **模式选择：单 Agent / 多 Agent** | ✅ | — | 任务可拆分为多个阶段 → 多 Agent；单一职责 → 单 Agent |
| 4 | 多 Agent：阶段名称及职责 | 多 Agent 时必填 ✅ | — | 从用户描述中提取，或根据任务性质建议拆分方案 |
| 5 | model_type | ❌ | 仅当 `config/llm.yaml` 配置了 `model.default_model_type` 时才使用该默认值 | 读取项目配置后确认：使用已配置默认 / 明确指定 |
| 6 | tool_call_type | ❌ | `code_act` | 极少需要更改 |
| 7 | 预定义 Tool 列表 | ❌ | 根据任务自动推荐 | 分析 → `read_file`+`get_file_outline`；修改 → `edit_file`；报告 → `write_markdown_file` |
| 8 | 自定义 Tool | ❌ | 无 | 用户明确提到需要自定义 Python 函数时 |
| 9 | planning_interval | ❌ | 不设置 | 复杂 Worker 建议设为 `3` |
| 10 | max_steps | ❌ | `80`（Worker 建议 `40`） | 特别大的任务可适当增加 |
| 11 | execution_env | ❌ | `local` | 用户提到隔离/Docker 时 |
| 12 | Application 级 config/system.yaml | ❌ | 不生成 | 需要覆盖全局配置时 |
| 13 | 自定义 sysprompt | ❌ | 不生成 | 用户明确要求时 |
| 14 | 私有 Skill | ❌ | 无 | 用户提到自定义 Skill 时 |

### `model_type` 发现与确认（必须执行）

在编写任何 Agent YAML 之前，先从项目根目录读取 `config/llm.yaml`：

1. 从 `model` 节点提取可用类型（排除保留键 `default_model_type` 和非 dict 值）。
2. 读取 `model.default_model_type` 作为项目默认模型类型。如果缺失，不要省略 `model_type`；应明确选择一个可用类型。
3. 在交互场景中，向用户确认：
   - 是否使用 `default_model_type`；
   - 如果不使用，展示"项目中可用的类型 + 自定义"供选择。
4. 在自主场景中：
   - 如果配置可读且设置了 `default_model_type`，默认使用它；
   - 如果缺少 `default_model_type`，明确选择一个可用类型；
   - 如果读取失败，使用"必须显式指定 model_type 或确保项目默认已配置"策略并在"假设列表"中声明。

> 约束：`model_type` 的可选值由项目配置决定，不是硬编码为 `powerful/fast/summary`。

### 智能推荐规则

当用户未明确指定 Tool 时，根据任务类型自动推荐：

| 任务类型 | 推荐 Tool |
|----------|-----------|
| 代码分析/审查 | `read_file`、`get_file_outline`、`browse_directory`、`ripgrep_search_directory` |
| 代码修改/重构 | `read_file`、`edit_file`、`get_file_outline` |
| 报告/文档生成 | `write_markdown_file`、`read_file` |
| Git/PR 审查 | `get_git_diff_content`、`read_file`、`ripgrep_search_directory` |
| 构建/测试 | `shell_tool`、`read_file` |
| 批量文件处理 | `list_files_glob`、`read_file`、`write_file` |

### 交互模式：缺失必填信息时

如果 Prompt 缺少必填信息（#1-#4），向用户提问。**一次性问完所有缺失项**，不要分多轮：

```
生成 Application 需要以下信息：
1. Application 名称？（建议小写+下划线，例如 code_review）
2. 一句话功能描述？
3. 单 Agent 还是多 Agent？如果是多 Agent，如何划分阶段？
```

如果已识别到 `config/llm.yaml`，可追加（可选但建议）：

```
4. 使用哪种 model_type 策略？
   - 使用已配置的项目默认（default_model_type）
   - 明确指定（从项目可用 model_type 列表中选择）
   - 自定义（必须已在 config/llm.yaml 中定义）
```

---

## 第二阶段：计划确认

**在生成任何文件之前**，必须向用户展示完整的生成计划。

> 💡 **完整示例**请参见 `references/full-example.md`，其中包含 `code_review` 的完整计划文本。

### 多 Agent 模式计划模板

```markdown
## 📋 Application 生成计划

**名称**：<app_name>
**描述**：<一句话描述>
**模式**：Supervisor + N Worker

### 目录结构预览
applications/<app_name>/
├── <app_name>_app.py              # 入口脚本
├── agent_tools/                    # 自定义 Tool（如有）
│   └── <tool_module>.py
├── config/                         # Application 级配置（如有）
│   └── system.yaml
└── workflows/
    ├── <app_name>_agent.yaml       # Supervisor YAML
    └── worker_agents/
        ├── <worker_name_a>.yaml
        ├── <worker_name_b>.yaml
        └── ...

### Supervisor 配置摘要
- name: <supervisor_name>
- model_type: <使用已配置的 default_model_type 或明确指定的值>
- tool_call_type: <tool_call_type>
- 自定义 Tool：<列表>
- Worker 数量：N

### Worker 配置摘要
| 阶段 | 名称 | 职责 | model_type 策略 | Tool |
|------|------|------|-----------------|------|
| 1 | <worker_a> | ... | 使用已配置默认或明确指定 | [...] |
| 2 | <worker_b> | ... | 使用已配置默认或明确指定 | [...] |
```

### 单 Agent 模式计划模板

```markdown
## 📋 Application 生成计划

**名称**：<app_name>
**描述**：<一句话描述>
**模式**：单 Agent（无 Supervisor 编排）

### 目录结构预览
applications/<app_name>/
├── <app_name>_app.py              # 入口脚本
└── workflows/
    └── <app_name>_agent.yaml      # 唯一的 Agent YAML

### Agent 配置摘要
- name: <agent_name>
- model_type: <使用已配置的 default_model_type 或明确指定的值>
- tool_call_type: <tool_call_type>
- Tool：<列表>
- max_steps: <N>
```

阶段转换规则：

- **交互式**：等待用户确认（回复"ok"/"确认"/提出修改建议）后进入第三阶段。
- **自主式**：没有交互通道时，输出"计划 + 假设列表"后直接进入第三阶段。

---

## 第三阶段：文件生成

进入第三阶段后，**按以下顺序生成所有文件**（交互模式在确认后进入；自主模式在输出计划和假设后进入）。

> **📄 完整模板**请参见 [references/templates.md](./references/templates.md)；以下仅列出文件清单和要点。
> **📐 YAML Schema** 请参见 [references/agent-yaml-schema.json](./references/agent-yaml-schema.json)，用于 IDE 补全和校验。

### 3.1 Supervisor YAML
- **路径**：`applications/<app_name>/workflows/<app_name>_agent.yaml`
- **必填字段**：`name`、`description`、`workflow`（`|` 多行文本，或用于顺序工作流的非空 `list[str]`）
- **Workflow 最佳实践**：建议采用五段式结构（① 背景与角色 ② 核心职责与约束 ③ 执行流程（使用 ````mermaid```` 代码块，框架会自动提取以注入强约束指令） ④ 各步骤详细说明 ⑤ 输出要求）。
- **关键字段**：`model_type`（仅当已配置 `model.default_model_type` 时才可省略；否则必须明确指定）、`tool_call_type`、`tools`、`worker_agents`（仅使用 `path`）、`skills`（可选）、`execution_env`
- **完整模板** → 参见 [templates.md](./references/templates.md) §3.1

### 3.2 Worker YAML（每个阶段一个文件）
- **路径**：`applications/<app_name>/workflows/worker_agents/step<N>_<name>.yaml`
- **与 Supervisor 的区别**：需要 `agent_function_schema`（description + inputs + output），不需要 `worker_agents`
- **私有 Skill**：可选添加 `skills` 字段，将私有 Skill 包绑定到此 Worker
- **建议**：`planning_interval: 3`、`max_steps: 40`
- **完整模板** → 参见 [templates.md](./references/templates.md) §3.2

### 3.3 入口脚本
- **路径**：`applications/<app_name>/<app_name>_app.py`
- **要点**：确保 `project_root` 和 `run_app()` 中的 YAML 路径与实际路径一致
- **完整模板** → 参见 [templates.md](./references/templates.md) §3.3

### 3.4 自定义 Tool（如有）
- **路径**：`applications/<app_name>/agent_tools/<module_name>.py`
- **要点**：纯 Python 函数，无需 `@tool` 装饰器；描述从 docstring 提取；YAML 中 `module` + `function` 必须成对出现
- **完整模板** → 参见 [templates.md](./references/templates.md) §3.4

### 3.5 Application 级 config/system.yaml（如有）
- **路径**：`applications/<app_name>/config/system.yaml`
- **要点**：深度合并覆盖全局 `config/system.yaml`；仅在需要覆盖全局配置时生成。
- **白名单限制**：Agent YAML 只能覆盖 7 个白名单字段（`system`、`smart_summary`、`tool_access_control`、`execution_env`、`code_agent`、`tools`、`prompt`）。如果需要覆盖白名单之外的全局配置，必须通过生成此 Application 级的 `system.yaml` 来实现。
- **完整模板** → 参见 [templates.md](./references/templates.md) §3.5

### 3.6 自定义 sysprompt（如有）
- **路径**：`applications/<app_name>/sysprompt/custom_prompt.yaml`
- 仅在用户明确要求时生成。框架默认 Prompt 适用于大多数场景。

### 3.7 单 Agent 模式 YAML（替代 3.1 和 3.2）
- **路径**：`applications/<app_name>/workflows/<app_name>_agent.yaml`
- **与多 Agent 的区别**：不需要 `worker_agents`，不需要 `agent_function_schema`
- **私有 Skill**：可选添加 `skills` 字段（支持 string / dict / list 格式）
- **完整模板** → 参见 [templates.md](./references/templates.md) §3.7
- **完整示例** → `references/full-example.md` 末尾的 `simple_scanner` 示例

### 3.8 私有 Skill（可选）
- **写入位置**：对应 Agent YAML 的顶层 `skills` 字段（Supervisor / Worker / 单 Agent 均支持）
- **支持的格式**：
  - 字符串：`skills: "skills/agent-recall-with-files"`
  - 字典：`skills: {path: "skills/agent-recall-with-files", platform: "Claude"}`
  - 列表：`skills: ["skills/a", {path: "skills/b", invocation-control: {allow-model: true, allow-hook: true}}]`
- **建议**：默认使用列表格式，便于扩展多个 Skill 和配置 invocation-control

### 3.9 Markdown 格式 Agent 配置（可选替代格式）

除了标准的 `.yaml` 格式，框架还支持使用 `.md`（Markdown）格式编写 Agent 配置文件：

- **格式规则**：文件开头用 `` ```yaml `` 代码块放置 `name`、`description` 等元数据；代码块之后的 Markdown 正文自动作为 `workflow` 字段内容
- **适用场景**：当 `workflow` 内容较复杂、包含大量 Markdown 标题/表格时，使用 `.md` 格式可获得更好的编辑体验和 IDE 高亮
- **路径**：`applications/<app_name>/workflows/<app_name>_agent.md`（后缀为 `.md`）
- **限制**：默认仍推荐 `.yaml` 格式，`.md` 格式作为可选替代

示例：

```markdown
```yaml
name: "my_agent"
description: "A demo agent"
tool_call_type: "code_act"
```

# 工作流

## 背景
你是一名...

## 执行步骤
1. ...
2. ...
```

> 注意：使用 `.md` 格式时，`workflow` 字段不应在 YAML 代码块中出现，而是由文件的 Markdown 正文自动填充。

---

## 第四阶段：配置校验（必须执行）

所有文件生成后，先运行校验脚本，默认输出 JSON。仅当校验通过（exit code = 0）时才进入运行阶段。

```bash
# <project_root>: 项目根目录（包含 config/llm.yaml 的目录的上一级），由 AI 动态获取
# <skill_root>:   当前 Skill 根目录（包含 scripts/ 的目录），由 AI 动态获取
cd <project_root>
.venv/bin/python <skill_root>/scripts/validate_application_yaml.py \
  --app-root applications/<app_name>
```

### 校验脚本约定

- 仅支持一个参数：`--app-root applications/<app_name>`（必填）
- 路径基准：
  - 校验脚本路径：AI 根据 Skill 目录动态获取绝对路径
  - `--app-root`：相对于项目根目录解析
  - 执行时当前目录必须在项目目录树内（脚本自动向上搜索 `config/llm.yaml` 定位项目根目录；测试环境通常没有 `config/llm.yaml`（含敏感信息不提交 VCS），因此 fallback 到 `config/system.yaml`）
- Python 解释器：使用项目配置的 Python 环境
- 默认输出：JSON（stdout）
- 退出码：
  - `0`：所有校验通过
  - `1`：存在配置错误
  - `2`：参数错误或脚本运行异常
- JSON 输出结构：
  - `summary`
  - `errors[]`（每项包含：`file`、`field`、`rule`、`message`、`suggestion`）

---

## 第五阶段：运行指南

校验通过后，告知用户如何运行（以下 `<project_root>` 由 AI 动态获取）：

```markdown
## 🚀 如何运行

### 方法一：使用 runner（推荐）
cd <project_root>
.venv/bin/python src/runner.py applications/<app_name>/workflows/<app_name>_agent.yaml

### 方法二：使用入口脚本
cd <project_root>
.venv/bin/python applications/<app_name>/<app_name>_app.py

### 方法三：使用 AgentLoom CLI（如已安装）
cd <project_root>
loom run applications/<app_name>/workflows/<app_name>_agent.yaml
```

> ℹ️ **断点续跑默认开启**：您创建的每个应用自动具备断点续跑/心跳监控能力，无需额外代码。如运行中断，使用相同 task ID 重新运行将从中断点继续。如需调整行为（如调试时保留产物），在 `config/system.yaml` 中配置 `checkpoint.*`。配置字段表详见 `references/quick-reference.md` 第 6 节。

---

## 预定义 Tool 快速参考（TOP 10 最常用）

> 完整 30+ Tool 列表请参见 `references/quick-reference.md`。

| Tool 名称 | 功能 | 推荐场景 |
|-----------|------|----------|
| `read_file` | 读取完整文件内容 | 几乎所有分析类 Worker |
| `get_file_outline` | 获取代码大纲 | 代码结构分析 |
| `browse_directory` | 浏览目录结构 | Supervisor 获取全局视图 |
| `ripgrep_search_directory` | 高性能正则搜索 | 关键字/模式定位 |
| `edit_file` | 查找替换编辑 | 代码修改 Worker |
| `write_markdown_file` | 写入 Markdown | 报告生成 Worker |
| `shell_tool` | 执行 Shell 命令 | 构建、测试、Git |
| `list_files_glob` | Glob 文件搜索 | 批量文件发现 |
| `get_git_diff_content` | 获取 Git 差异 | PR 审查 |
| `write_file` | 创建新文件 | 生成输出文件 |

---

## 关键约束提醒（生成文件时必须遵守）

| # | 约束 | 后果 |
|---|------|------|
| 1 | **Agent YAML 不得包含 `model`/`llm`/`langfuse`** | 框架自动过滤 + 警告 |
| 2 | **`worker_agents` 必须使用 `path`，禁止使用 `name`** | 报错：无法加载 |
| 2a | **`worker_agents.path` 简写必须包含文件后缀**（如 `scan.yaml`，不能写 `scan`） | 报错：缺少文件后缀 |
| 2b | **Supervisor YAML 必须在 `workflows/` 下，Worker YAML 必须在 `workflows/worker_agents/` 下** | 报错：目录不存在 |
| 3 | **自定义 Tool 是纯函数（无装饰器），描述来自 docstring** | YAML 中的 description 会被忽略 |
| 4 | **`module` 和 `function` 必须成对出现** | 只写一个会报错 |
| 5 | **单个 `workflow` 使用 `\|`；顺序 `workflow` 使用非空 `list[str]`，每项建议用 `\|`** | 否则格式丢失或校验失败 |
| 6 | **列表是覆盖而非追加** | 覆盖 `default_loaded_tools` 需要写完整列表 |
| 7 | **Worker 返回值始终为字符串** | `None` → `""`，其他 → `str()` |
| 8 | **`agent_function_schema.inputs` 的键必须是合法 Python 标识符** | 必须满足 `isidentifier()`，不允许连字符或数字开头 |

> 完整约束清单 + 可用值表请参见 `references/quick-reference.md`。

---

## 完整端到端示例

> **强烈建议先阅读** `references/full-example.md`。
> 该文档包含一个完整的 `code_review` Application 示例（Supervisor + 3 Worker），
> 展示了从需求收集 → 计划确认 → 6 个生成文件的完整流程，
> 以及一个 `simple_scanner` 单 Agent 模式示例。

---

## 生成后验证清单

所有文件生成后，执行以下检查以确保正确性：

- [ ] 运行校验脚本：`cd <project_root> && .venv/bin/python <skill_root>/scripts/validate_application_yaml.py --app-root applications/<app_name>`
- [ ] 校验脚本退出码为 `0` 且 JSON 输出中 `errors=[]`
- [ ] 所有 YAML 文件语法正确（`name`、`description`、`workflow` —— 三个必填字段均存在且非空）
- [ ] `workflow` 字段使用 `|` 多行文本块，或使用非空列表且每项为非空多行文本块
- [ ] `worker_agents` 中的每一项都使用 `path`（而非 `name`）
- [ ] `worker_agents.path` 简写必须包含文件后缀（如 `scan.yaml`，不能写 `scan`）
- [ ] Supervisor YAML 在 `workflows/` 下，Worker YAML 在 `workflows/worker_agents/` 下
- [ ] `worker_agents` 的 `path` 引用的文件都存在
- [ ] `worker_agents.path` 指向的是文件（而非目录）
- [ ] 自定义 Tool 的 `module` 和 `function` 成对出现
- [ ] 自定义 Tool 是纯 Python 函数（无 `@tool` 装饰器），且有完整的 docstring
- [ ] Agent YAML 中不包含 `model`/`llm`/`langfuse` 字段
- [ ] `model_type` 策略已确认：使用已配置的 `default_model_type` 或明确指定项目中可用的类型
- [ ] 如果配置了 `execution_env`，`type` 必须为 `local` / `docker` / `e2b` / `wasm` 之一
- [ ] 如果配置了 `skills`，其结构必须为 `list / dict / string`
- [ ] 入口脚本 `_app.py` 中的 YAML 路径与实际文件路径一致
- [ ] `agent_function_schema.inputs` 中的键是合法 Python 标识符
