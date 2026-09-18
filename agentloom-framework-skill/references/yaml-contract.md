# Agent YAML 契约

## 通用必填字段

每个 Agent YAML 必须有：

```yaml
name: "<agent_name>"
agent_runtime: "smolagents"
description: "<一两句话角色定位>"
workflow: |
  <完整执行协议>
```

`description` 只写角色定位；详细流程写进 `workflow`。

Supervisor 和 Worker 的定义格式均支持 `.yaml`、`.yml`、`.md`。Markdown 使用
`yaml` 围栏代码块；其余非空正文覆盖 `workflow`。Studio 目录/详情、公开预检、
schedule 目标与执行复用同一解析语义，包含嵌套 Application 和 workflow 目录；
不要把 Markdown Worker 当成独立 Supervisor。重复 key、非法定义和越界/符号链接
引用仍须拒绝，结构读取不得构造模型或分配 Run。

## Supervisor

```yaml
name: "<app_name>"
agent_runtime: "smolagents"
description: "<Supervisor 角色>"
model_type: "powerful"
max_steps: 80
worker_agents:
  - path: "applications/<app_name>/workflows/worker_agents/<worker>.yaml"
workflow: |
  # <Workflow Name>
  ...
```

规则：

- `worker_agents` 只支持 `path`，不要写 `name`。
- `path` 支持绝对路径、AgentLoom 根目录相对路径、`worker_agents/` 下文件名、或不带后缀的 Worker 名；生成时推荐写完整项目相对路径。
- `agent_runtime` 必填；当前唯一已注册值为 `smolagents`。缺失、`langgraph` 或其他值都会在预检阶段失败，不会回退。
- Agent 只通过 provider 原生结构化工具调用执行 Tool。

长期目标可在顶层 Supervisor 配置：

```yaml
goal:
  enabled: true
  token_budget: 120000  # 可选；省略为无限制
```

只接受 `goal: true/false` 或显式包含 `enabled: bool` 的 mapping；mapping 仅允许
`enabled` 与正整数 `token_budget`。Goal 模式推荐单个多行 workflow；list 会按顺序
编号并合并为一个目标上下文。Goal 的完成、预算、resume、checkpoint 和 schedule
语义见项目 `docs/cn/goal_mode.md`。

## Worker

```yaml
name: "<worker_name>"
agent_runtime: "smolagents"
description: "<Worker 职责>"
model_type: "powerful"
max_steps: 40
todo:
  mode: "auto"
agent_function_schema:
  description: "<作为工具被 Supervisor 调用时的说明>"
  inputs:
    user_request:
      description: "用户需求"
      required: true
  output:
    description: "Markdown 文本"
workflow: |
  # <Worker Workflow>
  ...
```

规则：

- Worker 被 Supervisor 调用时必须有 `agent_function_schema`。
- `inputs` 的 key 必须是合法 Python 标识符。
- `inputs.<name>.required` 只能是布尔值；可选参数用 `required: false` 表达，不要在 type 里写 `Optional[...]`。
- runtime 会把输入类型归一为 `string`；不要依赖复杂类型声明。
- 输出应是可被下游 Worker 或 Supervisor 直接使用的文本。
- `todo.mode` 支持 `auto`、`on`、`off`；默认 `auto`。它与 `planning_interval` 独立，不要在 `tools` 中重复声明 `todo_write`。
- Worker YAML 禁止配置 `goal`，包括 `goal: false`；Goal 工具与生命周期只属于根 Supervisor。

## 模型与配置

- Agent YAML 不写 `model`、`llm`、`langfuse`。
- `model_type` 必须存在于 `config/llm.yaml`。
- 不写 `model_type` 时依赖 `model.default_model_type`，但生成新 Application 时推荐显式写出实际可用类型。
- `summary` 模型类型是 `smart_summary` 依赖；`config/llm.yaml` 中只要配置了模型类型，就必须包含 `summary`。
- 每个模型类型（包括 `summary`）必须显式声明 `adapter`，合法值为 `openai_chat`、`openai_responses`、`anthropic_messages`。
- `model` 只是传给 LiteLLM 的不透明模型名；不能根据名称或前缀推断 `adapter`。
- 未知字段会作为 `extra_completion_params` 透传给 LiteLLM；`adapter` 是已知字段，不会透传给 provider。
- Agent 使用结构化 tools schema；工具名必须来自本轮可用工具，参数只做 schema-bound 窄化转换。不要把 prose/free-text tool call 当成可执行格式。
- 完整配置面、覆盖层级、system/llm/Skill/Hook/MCP/checkpoint 字段见 `configuration-surface.md`。

Agent YAML 当前可覆盖的系统配置白名单：

```text
system, model_request_headers, smart_summary, context_engine,
tool_access_control, tools, shell_settings,
default_toolsets, toolsets, prompt, mcp_servers, self_learning, hooks
```

Worker 的有效配置由 Worker YAML 自己重建，不继承 Supervisor 的权限覆盖；需要相同路径权限或 shell 权限时，Worker YAML 或应用级 `config/system.yaml` 也要写。

`context_engine` 只建议覆盖 `min_chars` / `preview_max_chars`。ContextEngine 默认启用并使用 task-scoped store；不要在 Agent YAML 里设计关闭开关或第二套恢复路径。

## 三种 Agent-Tool 路径

| 路径 | 何时使用 | 配置方式 |
|---|---|---|
| Path A: `worker_agents` 自动注册 | Supervisor 调一次 Worker | `worker_agents: [{path: ...}]` |
| Path B: 普通动态工具 | 确定性 Python 能力 | `tools: [{name, module, function}]` |
| Path C: Python 包装 Agent | 批量、断点、错误隔离、前后置处理 | Tool 内调用 `YamlAgentFactory.create_agent_as_tool()` |

优先 Path A；需要 Python 控制流时再用 Path C。

Tool 规则：

- 预定义工具只写 `name`。
- 动态工具必须同时写 `module` 和 `function`，docstring 是工具说明来源；YAML 里的 `description` 不会覆盖函数 docstring。
- 需要锁定参数时用 `fixed_args`，必须是字典；被锁定的参数不会暴露给 LLM。

```yaml
tools:
  - name: "grep_search"
    fixed_args:
      path: "."
  - name: "get_module_context"
    module: "applications.<app>.agent_tools.module_context"
    function: "get_module_context"
```

## Skills 配置

AgentLoom 从项目和 Application 的 `skills/` 约定目录自动发现 Skill。
额外目录只使用 `paths` 配置：

最常用写法：

```yaml
skills:
  paths:
    - shared/skills
```

规则：

- `skills` 必须是只包含 `paths: list[str]` 的字典。
- prompt 只显示 `name`、`description`；模型需要时调用 `skill(name)`。
- Skill 正文没有预加载模式。
- 激活 Skill 不会授予脚本、网络、工具或 Hook 权限。
- 同层同名 Skill 报错；Agent 覆盖 Application，Application 覆盖项目。

## Skill 包结构

标准结构：

```text
applications/<app_name>/skills/<skill_name>/
├── SKILL.md
├── references/
├── scripts/
└── assets/
```

`SKILL.md` frontmatter 参考 Claude Code 风格：

```yaml
---
name: tdd
description: Use when implementing behavior with tests.
license: MIT
compatibility: Requires git.
metadata:
  owner: platform
---
```

规则：

- `name` 和 `description` 必填。
- `SKILL.md` 文件发现忽略大小写，但新写文件统一使用 `SKILL.md`。
- 不加载散落 `.md`、`skills.md` 或 loose markdown。
- 未识别 frontmatter 字段静默忽略，不映射旧字段。
- 未识别 frontmatter 字段不会进入运行时元数据。
- 包内资源和脚本通过 Agent 已有的常规文件/Shell 工具使用。
- Skill 与 Hook 是独立模块；`SKILL.md` 中出现 `hooks` 会明确报迁移错误。
- Hook 通过 Agent YAML 顶层 `hooks:` 直接声明，或显式引用带 `HOOK.yaml` 的独立 Bundle。
