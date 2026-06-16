# Agent YAML 契约

## 通用必填字段

每个 Agent YAML 必须有：

```yaml
name: "<agent_name>"
description: "<一两句话角色定位>"
workflow: |
  <完整执行协议>
```

`description` 只写角色定位；详细流程写进 `workflow`。

## Supervisor

```yaml
name: "<app_name>"
description: "<Supervisor 角色>"
model_type: "powerful"
tool_call_type: "tool_call"
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
- Supervisor 推荐 `tool_call`，因为 Worker 调用、参数和结果可结构化审计；只有需要 Python 控制流时才用 `code_act`。

## Worker

```yaml
name: "<worker_name>"
description: "<Worker 职责>"
model_type: "powerful"
tool_call_type: "code_act"
max_steps: 40
planning_interval: 3
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

## 模型与配置

- Agent YAML 不写 `model`、`llm`、`langfuse`。
- `model_type` 必须存在于 `config/llm.yaml`。
- 不写 `model_type` 时依赖 `model.default_model_type`，但生成新 Application 时推荐显式写出实际可用类型。
- `summary` 模型类型是 `smart_summary` 依赖；`config/llm.yaml` 中只要配置了模型类型，就必须包含 `summary`。
- `config/llm.yaml` 的模型类型支持 `supports_structured_output`；未知字段会作为 `extra_completion_params` 透传给 `litellm.completion()`。
- `supports_native_tool_calls` 已删除，不能写进 `config/llm.yaml`。`tool_choice` 如需使用只能作为 provider 参数透传，不是能力探测或文本兜底开关。
- `tool_call_type: tool_call` 使用结构化 tools schema；工具名必须来自本轮可用工具，参数只做 schema-bound 窄化转换。不要把 prose/free-text tool call 当成可执行格式。
- `execution_env.type` 只支持 `local` / `docker` / `e2b` / `wasm`；`docker` / `e2b` 时默认工具不会自动加载。
- 完整配置面、覆盖层级、system/llm/Skill/Hook/MCP/checkpoint 字段见 `configuration-surface.md`。

Agent YAML 当前可覆盖的系统配置白名单：

```text
system, smart_summary, tool_access_control, execution_env, code_agent, tools,
shell_settings, tools_mapping, default_loaded_tools, prompt, mcp_servers
```

Worker 的有效配置由 Worker YAML 自己重建，不继承 Supervisor 的权限覆盖；需要相同路径权限或 shell 权限时，Worker YAML 或应用级 `config/system.yaml` 也要写。

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
      cwd: "."
  - name: "get_module_context"
    module: "applications.<app>.agent_tools.module_context"
    function: "get_module_context"
```

## Skills 配置

AgentLoom 的 Skill 配置是 application/agent 行为配置，不是元数据登记表。配置了就启动时注册；默认按需加载。

最常用写法：

```yaml
skills:
  load-mode: on-demand
  items:
    - applications/<app_name>/skills/<skill_name>
    - applications/<app_name>/skills/<another_skill>
```

全文预加载：

```yaml
skills:
  load-mode: eager
  items:
    - applications/<app_name>/skills/strict-review
```

显式收紧脚本或网络：

```yaml
skills:
  load-mode: on-demand
  allow-scripts: false
  allow-network: false
  items:
    - applications/<app_name>/skills/safe-review
```

规则：

- `load-mode` 只支持 `on-demand` 和 `eager`，不写时默认 `on-demand`。
- `on-demand` 只把 catalogue 放进 prompt：`name`、`description`、`argument_hint`、`when_to_use`；模型需要时调用 `load_skill`。
- `eager` 把完整 Skill 正文注入系统 prompt，不再重复放 catalogue。
- `items` 推荐写应用内相对路径，例如 `applications/<app_name>/skills/tdd`。
- `skills` 支持字符串、单个字典、列表、或带共享策略的 `{load-mode, allow-scripts, allow-network, items}`。
- 单个 item 可以写字符串路径；只有需要覆盖策略或 `platform` 时才写字典。
- `allow-scripts` 和 `allow-network` 默认允许；用户明确禁止时才设置为 `false`。
- 全局/应用级有效配置 `skills: []` 会关闭全局 skills 列表和 `AGENT_ROOT/skills/` 自动发现；Agent YAML 的 `skills: []` 只影响 Agent 私有层。
- 同名 Skill 从不同路径加载会报错，不是覆盖。
- 不再使用 `invocation-control`、`force-inject`、`hidden`、`user-invocable` 这类状态。

字典 item 写法仅用于局部覆盖：

```yaml
skills:
  load-mode: on-demand
  items:
    - path: applications/<app_name>/skills/script-probe
      load-mode: eager
      allow-scripts: true
      allow-network: false
```

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
description: Test-driven development workflow.
allowed-tools: Bash, Read, Edit
argument-hint: "<task>"
arguments: [task]
when_to_use: Use when implementing or fixing behavior with tests.
model: powerful
context: fork
agent: reviewer
effort: high
shell: bash
hooks: {}
---
```

规则：

- `name` 和 `description` 必填。
- `SKILL.md` 文件发现忽略大小写，但新写文件统一使用 `SKILL.md`。
- 不加载散落 `.md`、`skills.md` 或 loose markdown。
- 未识别 frontmatter 字段静默忽略，不映射旧字段。
- `when-to-use` 不等于 `when_to_use`，`argument-names` 不等于 `arguments`。
- skill 需要脚本时，把脚本放在 `scripts/`，由 `run_skill_script` 执行并保留审计日志。
- Hook 只写在 `SKILL.md` frontmatter 的 `hooks:`；不要直接写 Agent YAML 顶层 `hooks:`。
