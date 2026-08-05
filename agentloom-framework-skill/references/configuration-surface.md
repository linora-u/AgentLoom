# AgentLoom 配置面总览

本文件回答两个问题：一个配置能不能写，以及应该写在哪里。优先以当前代码为准，`docs/en` 是说明材料；两者冲突时，以代码和测试为准，并把冲突记录为待修文档。

## 证据入口

配置相关改动至少交叉看这几类文件：

- 用户文档：`docs/en/config-overview.md`、`agent_config.md`、`goal_mode.md`、`system_config.md`、`llm_config.md`、`skills_config.md`、`hooks.md`、`checkpoint.md`。
- 系统配置加载：`src/lib/config/config.py`、`layered_builder.py`、`config_validation.py`。
- LLM 配置：`src/lib/config/llm_config.py`、`src/lib/smolagents/models/model_types.py`、`model_manager.py`。
- Agent YAML 校验：`src/lib/smolagents/agent/agent_validation.py`、`yaml_agent_factory.py`、`base_agent.py`。
- Skill/Hook：`src/lib/smolagents/skills/parser.py`、`skills.py`、`src/lib/smolagents/hooks/*`。
- MCP：`src/mcp/config.py`、`tests/mcp_test/*`。

## 配置文件与层级

| 配置面 | 写在哪里 | 用途 | 关键规则 |
|---|---|---|---|
| 全局系统配置 | `config/system.yaml` | runtime root、日志、工具、权限、shell、prompt、skills、checkpoint 等系统行为 | `runtime`/`logging` 只在此处生效；其他字段参与 deep merge；列表整段替换 |
| 本地模型配置 | `config/llm.yaml` | 模型类型、密钥、网关、推理参数、限流、重试 | 独立加载，不被 app/Agent YAML 覆盖；通常被 `.gitignore` 忽略 |
| 应用级系统覆盖 | `applications/<app>/config/system.yaml` | 当前应用专属的系统行为覆盖 | 从 Agent YAML 路径向上找到最近 `workflows/`，其父目录即 app root |
| Agent YAML | `applications/<app>/workflows/*.yaml` | 单个 Agent 的角色、workflow、工具、模型类型、运行模式 | 只有白名单字段会 overlay 到系统配置，其余是 Agent 自身属性 |
| Worker YAML | `applications/<app>/workflows/worker_agents/*.yaml` | 被 Supervisor 调用的 Agent 工具 | 有 `agent_function_schema` 才能导出为 callable tool |
| Skill 包 | `applications/<app>/skills/<name>/SKILL.md` 或 `skills/<name>/SKILL.md` | 可按需加载的长期能力、脚本和资源 | `SKILL.md`/`skill.md` 入口；不得声明 Hook |
| Hook Bundle | `applications/<app>/hooks/<name>/HOOK.yaml` 或 `hooks/<name>/HOOK.yaml` | 显式授权的确定性事件行为 | 只由顶层 `hooks.bundles` 引用；永不自动发现 |
| MCP 配置 | `mcp_servers` 指向的 JSON 文件 | 外部 MCP server 工具 | `mcp_servers` 支持 string/list/dict 三种 YAML 形式 |

合并顺序：框架默认值 -> `config/system.yaml` -> `applications/<app>/config/system.yaml` -> Agent YAML 白名单字段。字典递归合并；列表和标量整体替换。

LLM 配置不参与这个链条。`model`、`llm`、`langfuse` 写进 `system.yaml` 或 Agent YAML 会被过滤并警告。

`runtime` 与 `logging` 是 global-only：Application 级 `config/system.yaml` 和 Agent YAML 一旦包含任一顶层字段就会校验失败；必须删除并改到项目根 `config/system.yaml`。这样错误位置的配置不会被静默忽略。`checkpoint` 仍可由 Application 级 system overlay 调整，但不在 Agent YAML 白名单中。

需要隔离子进程时只允许使用 `AGENTLOOM_RUNTIME_ROOT` 覆盖整套 canonical runtime home；禁止恢复 self-learning 专用 root 或让日志/checkpoint/session 分根。

## Agent YAML 可配置字段

每个 Agent 必填：

```yaml
name: "<agent_name>"
description: "<一两句话角色定位>"
workflow: |
  <完整执行协议>
```

通用可选字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `tools` | `list[dict]` | Agent 额外工具列表；预定义工具只写 `name`，动态工具写 `name/module/function` |
| `model_type` | `str` | 选择 `config/llm.yaml` 中定义的模型类型；缺失时使用 `model.default_model_type` |
| `tool_call_type` | `code_act` 或 `tool_call` | `code_act` 让模型写 Python 调工具；`tool_call` 发送 provider/native tools schema，并只接受结构化 tool calls |
| `max_steps` | `int` | smolagents 最大步数；默认 80 |
| `planning_interval` | 正整数或数字字符串 | 仅控制周期性 planning，与 Todo 解耦 |
| `todo` | `{mode: auto|on|off}` | 当前任务进度跟踪；默认 `auto`，`on` 强提示多步骤任务先建 Todo，`off` 完全隐藏 |
| `concurrency` | 正整数或 `"auto"` | 仅影响同一 Worker 通过 `.batch()` 被多输入批量调用 |
| `execution_env` | `dict` | `code_act` 的执行环境：`local`、`docker`、`e2b`、`wasm` |
| `prompt` | `str` 或 `{path: ...}` | 自定义系统 prompt 模板路径 |
| `skills` | `str` / `dict` / `list` | 当前 Agent 私有 Skill 配置 |
| `goal` | `bool` 或 `{enabled: bool, token_budget?: int}` | 仅顶层 Supervisor；开启 continuation、显式完成和根 Agent 树软 token 预算 |

`tool_call` 模式的主路径是 provider/native tool calls。只要当前 Agent 有可用工具，AgentLoom 就发送结构化 tools schema；如果 provider 返回文本 fallback，也只接受明确结构化容器，例如 `{name, arguments}`、dump 出来的 native `tool_calls/function`、XML/invoke wrapper。不要设计依赖自由文本正则兜底的 workflow。

Supervisor 专属：

```yaml
worker_agents:
  - path: "applications/<app>/workflows/worker_agents/<worker>.yaml"
```

规则：`worker_agents` item 只支持 `path`，不支持 `name`。路径可以是绝对路径、项目根相对路径、`worker_agents/` 下的文件名，或不带后缀的 worker 名。

Supervisor 还可配置 `goal: true/false`，或显式 mapping。Goal mapping 不做类型宽松
转换；`token_budget` 缺失表示无限制。开启后 workflow list 合并为一个目标上下文，
并提供仅根 Supervisor 可见的 `get_goal` / `update_goal`。Schedule 可以使用同一 YAML，
但无人值守 Goal 强烈建议设置预算。

Worker 专属：

```yaml
agent_function_schema:
  description: "<Worker 作为工具时的说明>"
  inputs:
    query:
      description: "输入说明"
      required: true
  output:
    description: "输出说明"
```

规则：`inputs` 的 key 必须是合法 Python 标识符；`required` 只能是 bool；runtime 会把所有参数类型归一为 `"string"`，不要用 `Optional[...]` 或 `Union[...]` 表达可选性。

Worker YAML 如果出现任何 `goal` key 必须 fail-closed；不能用 `goal: false` 占位。

## Agent YAML 白名单覆盖

当前代码里的 `_WORKFLOW_OVERLAY_KEYS` 是：

```text
system, model_request_headers, smart_summary, context_engine,
tool_access_control, execution_env, code_agent, tools, shell_settings,
tools_mapping, default_toolsets, toolsets, prompt, mcp_servers, self_learning, hooks
```

注意：

- 这不是 7 个字段；旧文档如果说只有 7 个已经过期。
- `tools` 在白名单里只有当它是 `list` 时才进入 overlay；它同时也是 Agent 的工具列表。
- `shell_settings`、`tools_mapping`、`toolsets` 可以在 Agent YAML 覆盖；`toolsets` 会整体替换全局 `default_toolsets`。
- `context_engine` 可以在 Agent YAML 覆盖，用于按应用或 Agent 调整可逆上下文压缩。
- `self_learning` 和显式 `hooks` bundle 可以在应用或 Agent YAML 覆盖；reviewer 必须从当前 root 的最终生效配置读取，不能使用进程全局回退。
- `mcp_servers` 可以在 Agent YAML 覆盖，并支持 string/list/dict。
- `runtime`、`logging`、`checkpoint` 不在 Agent YAML 白名单；不要把存储 root、日志策略或 resume 生命周期塞进 Agent workflow。
- Worker 的有效配置由全局、应用级、Worker YAML 自己重建；不会继承 Supervisor 的运行时覆盖。Worker 需要同样权限时必须自己写。

## system.yaml 配置面

常用顶层字段：

| 字段 | 说明 |
|---|---|
| `system` | `name/version/user_agent` 元信息 |
| `smart_summary` | 是否启用智能上下文压缩 |
| `context_engine` | 可逆上下文压缩：工具原文进本地 store，模型可见压缩预览和 `ContextRef` |
| `prompt` | 顶层系统 prompt 覆盖，字符串或 `{path: ...}` |
| `skills` | 全局 Skill 列表或共享策略 |
| `hooks` | 独立直接 Shell Hook 与显式 `HOOK.yaml` Bundle |
| `lsp_servers` | LSP 服务开关、重启次数、语言列表 |
| `execution_env` | 默认执行环境 |
| `code_agent` | `code_act` 可 import 模块和可调用内置函数 |
| `runtime` | 唯一 `.agentloom` root、run/artifact 保留天数和自动清理间隔；只允许全局配置 |
| `logging` | `level/console_enabled/file_enabled/max_file_bytes/backup_count`；run-scoped 且只允许全局配置 |
| `default_toolsets` | 默认加载 toolset 名列表 |
| `toolsets` | Agent 级内置 toolset 覆盖，空列表表示无内置工具 |
| `shell_settings` | shell 命令白名单、安全检查、sandbox、后台任务、audit log |
| `tools_mapping` | Skill `allowed-tools` 的平台别名映射 |
| `tool_access_control` | 工具路径访问控制 |
| `tool_metadata` | 工具输出截断、并发安全、分类、类型转换 |
| `tool_output_limits` | 上下文压缩阶段的工具输出保留上限 |
| `checkpoint` | checkpoint、resume、heartbeat 开关与保留策略 |
| `mcp_servers` | MCP server 配置入口 |
| `self_learning` | 可搜索运行历史、project/application 记忆与可选结束评审 |

### context_engine

ContextEngine 默认开启并绑定当前 task 的 checkpoint store。配置只暴露常用阈值；不要新增“关闭开关”或第二套恢复路径。

```yaml
context_engine:
  min_chars: 2000          # 小于该长度的工具输出不压缩
  preview_max_chars: 3000  # 模型可见预览的最大字符数
```

运行契约：

- 原始 tool/worker 输出写入 task-scoped `context_store`，模型只看到压缩预览和 `ContextRef`。
- `loom_retrieve_context(ref=..., query="", offset=0, limit=200)` 是唯一取回原文的公开工具。
- user/system 原始消息、写入/编辑/删除类工具内容不能被压缩。
- 需要调阈值时优先放应用级 `applications/<app>/config/system.yaml`，不要改全局配置。
- 如果确实新增配置字段，必须同时更新本文件、`yaml-contract.md` 的白名单说明、相关测试和真实 Application 验证。

### self_learning

自学习分成三层：可搜索 History、Application 候选与记忆、Project 记忆。数据库是权威来源；Markdown review artifacts 是人类工作面，不是第二份状态。

```yaml
self_learning:
  enabled: true
  events_retention_days: 90  # 兼容性保留；prune 必须显式传 --retention-days
  memory:
    prompt_max_chars: 12000
    max_item_chars: 4000
    scope_budgets:
      project: 8000
      application: 6000
  review:
    enabled: true
    application:
      review_model: summary
      trigger: {mode: batch, min_completed_runs: 5}
      approval: {fact: auto, experience: manual}
    project:
      review_model: summary
      trigger: {mode: batch, min_candidates: 5}
      approval: {fact: manual, experience: manual}
    artifacts:
      markdown: true
      review_auto_applied: true
```

运行契约：

- History 只用于 `loom sessions search/scroll`，不会被确定性 fallback 变成 memory。
- 模型面对的 `memory` 工具只有 `list|propose`。`propose` 只能为当前 Application 提交 `fact: {text}` 或 `experience: {trigger, symptom, action, verification}`；模型不能写 Project scope，也不能 replace/remove/promote。
- reviewer 只做有界、结构化抽取，不能选择 scope 或审批策略，也不能直接改文件、Skill 或既有记忆。Application review 读取该应用已完成的 root runs 与可信证据；Project review 只读取代码标记的 Project 证据，以及至少两个不同 Application 对同一 typed memory 的交叉印证，不读取原始 Application transcript。
- trigger `manual` 只由 CLI 触发；`after_run` 在成功 root run 且存在上下文时触发；`batch` 分别按 `min_completed_runs` 或 `min_candidates` 阈值触发。
- `approval.*: auto` 仍须通过代码证据门与容量检查，写为 `active_unreviewed`，之后由人类 `acknowledge` 或 `revoke`；`manual` 写为 `pending_pre_review`，由人类编辑 scoped INBOX 后应用。
- review artifacts 位于 `.agentloom/reviews/applications/<app>/` 或 `.agentloom/reviews/project/`：`batches/<review_id>/` 不可变；`markdown: true` 编辑 `INBOX.md`，`false` 编辑 `INBOX.json`。命令是 `loom learn review`、`loom reviews status/apply/rollback`，不存在 `loom memory approve/reject`。
- Project promotion 只能由人类对 Application candidate 执行 `promote_project`；需要 activation evidence。成功后 Project 项为 `active_confirmed`，Application 项变为 `shadowed`，冲突 payload 会被拒绝。
- `loom memory list/add/replace/remove/pending/stats/export` 是管理员直接维护 active memory 的 CLI，与模型 proposal 工具不同；History 保留由 `loom sessions prune --retention-days N` 管理。
- 在 `self_learning.review` 内，Application/Agent overlay 只能改变 `application`；`review.enabled`、Project policy 与 artifact policy 必须来自项目根配置。其他 `self_learning` 字段仍按普通 overlay 合并。
- v5 字段 `self_learning.memory.review_model`、`write_approval` 会被校验器拒绝。迁移到 `review.application|project.review_model` 和各 scope 的 `approval.fact|experience`。

### execution_env

```yaml
execution_env:
  type: local       # local | docker | e2b | wasm
  executor_kwargs: {}
```

只对 `tool_call_type: code_act` 生效。`tool_call` 模式不执行 Python code，因此 executor 设置无意义。`docker` / `e2b` 默认不自动加载本地文件工具；远程 executor 会跳过 `additional_functions`，并移除 `additional_authorized_imports` 里的 `"*"`。

### code_agent

```yaml
code_agent:
  additional_authorized_imports:
    - json
    - re
  additional_functions:
    - print
    - len
```

`"*"` 表示最大权限。只在 `code_act` 有意义；生产或不可信环境不要随手开 `"*"`。

### shell_settings

可配内容包括：

- `allowed_commands`：命令白名单，`"*"` 表示全放开。
- `allowed_operators`：shell 操作符白名单，支持 `|`、`||`、`&&`、`>`、`>>`、`<`、`;`。
- `security_checks`：`command_substitution`、`process_substitution`、`env_injection`、`ifs_injection`、`control_characters`、`incomplete_commands`、`dangerous_shell_prefix`、`zsh_dangerous_commands`、`parameter_expansion`、`destructive_patterns`。
- `dangerous_paths`、`block_destructive`：破坏性操作保护。
- `sandbox`：`enabled/mode/allow_write/deny_write/network_isolation/excluded_commands`。
- `background_tasks`：`enabled/max_concurrent/auto_background_on_timeout/max_output_bytes/stall_detection/stall_threshold_seconds`。
- `audit_log`：`enabled/log_policy_snapshot/log_success`。

如果用户不确定 shell 权限怎么配，先按 `shell-security-audit.md` 跑真实 workflow，读当前 run 的 `audit/shell.jsonl` 中 `[POLICY_SNAPSHOT]` 和拦截事件，再收敛 `allowed_commands`、`allowed_operators`、路径规则或 sandbox。

### tool_access_control

当前真实 schema 只有 `path_validation` 规则列表，没有全局 `include_paths` / `exclude_paths` 顶层快捷字段。

```yaml
tool_access_control:
  path_validation:
    - tools: ["read_file", "edit_file", "grep_search", "shell_tool"]
      include_paths: ["~/shared", "/data/project"]
      exclude_paths: ["secrets", ".env"]
      path_param_patterns: ["path", "file_path", "directory"]
```

规则：exclude 优先于 include；`tools: ["*"]` 匹配所有工具；工具没有命中任何规则时不做路径检查。

### default_toolsets / toolsets 与工具名

`default_toolsets` 与 Agent 级 `toolsets` 必须写 registry toolset 名，不是工具名。可用 toolsets：

```text
core_shell, core_file, core_search, context, skills, markdown_report, code_nav
```

常见工具包括：

```text
read_file, write_file, edit_file, list_directory,
write_markdown_file, write_markdown_file_raw, append_markdown_sections,
get_file_outline, lsp_find_definition, lsp_find_references,
lsp_get_document_symbols, lsp_hover, lsp_get_workspace_symbols,
grep_search, glob_search, ast_grep_search_file, loom_retrieve_context,
shell_tool, check_background_task, kill_background_task, list_background_tasks,
load_skill, list_skills, todo_write
```

实际完整列表以 `src/tools/catalog.py` 的 `ToolSpec` catalog 为准。读取 metadata
不得导入具体工具；Runtime implementation 只通过 `src/tools/loader.py` 按需加载。

## llm.yaml 配置面

基本结构：

```yaml
model:
  default_model_type: powerful
  powerful:
    model: "anthropic/claude-..."
    base_url: "https://..."
    api_key: "..."
    temperature: 0.2
    max_tokens: 8192
    timeout: 300
  summary:
    model: "openai/gpt-..."
```

规则：

- `summary` 是必需模型类型；`smart_summary` 依赖它。只要 `model` 下存在模型类型，缺少 `summary` 会报错。
- `default_model_type` 和 `common` 是保留 key，不作为模型类型。
- 模型类型名可自定义；`powerful`、`fast` 只是约定名。
- Agent YAML 只能写 `model_type`，不能写 LLM 参数。
- 每个模型类型必须有 `model`，且应使用 LiteLLM provider prefix，例如 `openai/...`、`anthropic/...`、`gemini/...`、`vertex_ai/...`、`azure/...`、`ollama/...`。

模型类型字段：

| 字段 | 说明 |
|---|---|
| `model` | LiteLLM 模型 ID，必填 |
| `base_url` | API 网关 |
| `api_key` | API key |
| `description` | 日志/文档描述 |
| `temperature` | 温度 |
| `max_tokens` | 整数或 `"max"` |
| `timeout` | 单次请求超时 |
| `num_retries` | 框架自管重试次数 |
| `retry_delay` / `max_retry_delay` | 指数退避参数 |
| `extra_headers` | dict 或 JSON 字符串 headers |
| `context_cache` | 是否注入 cache control |
| `system_prompt_boundary` | 静态/动态系统 prompt 分割 marker |
| `requests_per_minute` | 模型类型级 RPM，用于限流与并发 auto |
| `supports_structured_output` | `"true"` / `"false"`；影响 `code_act` 结构化输出路径 |
| 其他未知字段 | 收进 `extra_completion_params` 并透传给 `litellm.completion()` |

`supports_native_tool_calls` 已删除，写入 `config/llm.yaml` 会直接报错。不要用它、也不要新增等价的“兜底开关”。`tool_choice` 只是 provider/smolagents 请求参数；如果写在模型类型里，会作为未知字段透传给 `litellm.completion()`，不参与 native tool-call 能力探测。

未知字段透传适合 provider 特性，例如 `reasoning_effort`、`tool_choice`、`extra_body`。写之前要确认目标 endpoint 支持；不要把业务配置误塞进模型类型里。

`langfuse` 目前只是配置模型预留，未接入自动 tracing；不要为了“完整”强行配置。

## Skills 配置

AgentLoom Skill 使用 Claude Code 风格包：

```text
<skill-dir>/
├── SKILL.md
├── references/
├── scripts/
└── assets/
```

`SKILL.md` frontmatter 必填：

```yaml
---
name: tdd
description: Test-driven development workflow.
---
```

支持字段：

```yaml
allowed-tools: Bash, Read, Edit
argument-hint: "<task>"
arguments: [task]
when_to_use: Use when implementing or fixing behavior with tests.
model: powerful
context: fork       # inline | fork
agent: reviewer
effort: high
shell: bash
```

规则：

- unknown frontmatter 字段静默忽略。
- `when-to-use`、`argument-names`、`requires`、`disable-model-invocation`、`user-invocable` 这类旧字段不映射。
- `name` 必须是小写 kebab-case，最长 64；`description` 最长 1024。
- 同名 Skill 从不同路径加载会报错；同一路径重复加载只更新 runtime options。
- `allowed-tools` 可写逗号、竖线或换行分隔的字符串，也可写字符串列表；有 `platform` 时会通过 `tools_mapping` 映射。

Skill 配置格式：

```yaml
skills:
  load-mode: on-demand
  allow-scripts: false
  allow-network: false
  items:
    - applications/<app>/skills/safe-review
    - path: applications/<app>/skills/strict-review
      load-mode: eager
```

也支持：

```yaml
skills: applications/<app>/skills/tdd

skills:
  - applications/<app>/skills/tdd
  - path: applications/<app>/skills/debugging
```

加载层级：

1. 有效系统配置里的 `skills`，包括应用级 system overlay。
2. `AGENT_ROOT/skills/` 自动发现，除非有效系统配置显式 `skills: []`。
3. 当前 Agent YAML 的 `skills`。

`load-mode` 只控制 prompt 注入：`on-demand` 进 catalogue，`eager` 注入全文。Skill 发现和加载都不触碰 Hook。`SKILL.md` 中的 `hooks` 与 Skill 配置中的 `enable-hooks` 会明确报迁移错误。

`read_skill_resource(skill, path, offset, limit)` 读取包内资源，拒绝目录逃逸。`run_skill_script(skill, command, ...)` 在 skill 目录下运行脚本并写审计日志；`allow-scripts: false` 禁止脚本；`allow-network: false` 禁止常见网络命令。

## Hooks 配置

Hook 只能由全局 system、应用 system 或 Agent YAML 的顶层 `hooks:` 显式声明：

```yaml
hooks:
  bundles:
    agent-visualization:
      path: hooks/agent-visualization
  PreToolUse:
    - id: security-checker.pre-tool
      matcher: "Write|Edit|Bash"
      command: python hooks/security-checker/scripts/check.py
      timeout: 10
```

Bundle 目录必须包含 `HOOK.yaml`，其 `name` 与配置 key 一致；Bundle 不自动发现且不能递归引用 Bundle。直接条目与 Bundle 条目使用同一个事件映射 schema。

事件名：

```text
PreToolUse, PostToolUse, PostToolUseFailure,
SessionStart, SessionEnd,
Stop, StopFailure,
SubagentStart, SubagentStop,
TaskCreated, TaskCompleted,
```

条目只允许 `id`、`matcher`、`command`、`timeout`、`enabled`。Shell stdin 是版本化 JSON，stdout 统一为 `decision`、`modified_input`、`agent_context`、`user_message`、`reason`、`telemetry`。`PreToolUse` 和 `Stop` 是 fail-closed gate；其余事件是 fail-open observer。

配置层级为 global system、application system、Agent；高层同事件同 ID 完整替换或用 `enabled:false` 删除，禁止字段级合并和跨事件复用 ID。`matcher` 省略或 `"*"` 表示全部，否则按完整匹配正则。所有匹配 Hook 顺序执行；`PreToolUse` 转换逐个传递，阻断后立即短路。

## checkpoint 配置

Runtime 与 checkpoint 的身份边界：`run_id` 表示一次 attempt，resume 时改变；`task_id` 表示同一逻辑任务，resume 时保持不变。

```yaml
runtime:
  root_dir: ".agentloom"
  successful_run_retention_days: 7
  failed_run_retention_days: 30
  artifact_retention_days: 3
  cleanup_interval_hours: 24

logging:
  level: "INFO"
  console_enabled: true
  file_enabled: true
  max_file_bytes: 26214400
  backup_count: 3
```

```yaml
checkpoint:
  enabled: true
  cleanup_on_success: true
  max_resume_age: 604800
  heartbeat_interval: 5
```

每次 attempt 都写入 `.agentloom/runs/<application_id>/<run_id>/manifest.json`。启用 file log 时才有 `logs/runtime.log`；有 shell/tool 证据时才有对应 artifacts；成功结果存在时才写 `artifacts/result.txt`；checkpoint 证据存在时才复制 `audit/{task_tree.json,task_events.jsonl}`。Manifest 只指向真实存在的文件。逻辑任务状态独立写入 `.agentloom/checkpoints/<application_id>/<task_id>/`，包含 Supervisor checkpoint、heartbeat、ContextStore、file-history 和 Worker per-call checkpoint。Agent 工作区位于 `.agentloom/workspaces/agents/<application_id>/<agent_path>/`，任务状态隔离在 `tasks/<task_id>/`。日志关闭、轮转和 run 清理不能影响 checkpoint、workspace 或 Application outputs。

CLI 契约：文件日志默认按配置落盘，单次关闭用 `loom run --no-file-log`；不存在 `--log-to-file`。`loom list-tasks`、`loom clean-tasks`、`loom run --resume <task_id>` 验证 checkpoint；`loom clean-runtime` 应用 run retention；`loom migrate-runtime --dry-run|--apply` 迁移/归档旧 `.logs`。真实运行必须读 manifest、runtime.log 与 shell.jsonl，不能只看退出码。

## MCP 配置

AgentLoom 支持把 MCP server 工具追加进 Agent 工具集。`mcp_servers` 可以写在系统配置、应用级 system overlay 或 Agent YAML 白名单字段中。

```yaml
mcp_servers: "config/.mcp.json"
```

```yaml
mcp_servers:
  - "config/.mcp.json"
  - "config/extra-mcp.json"
```

```yaml
mcp_servers:
  path: "config/.mcp.json"
  timeout: 30
  tool_timeout: 60
  tool_name_prefix: true
```

dict 形式也支持 `paths: [...]`。无效类型会跳过并 warning。

## 生成配置时的取舍

- 只有当前应用需要的配置，优先放 `applications/<app>/config/system.yaml` 或 Agent YAML；不要改全局 `config/system.yaml`。
- 配置越少越好。新增配置前先确认它改变用户可观察行为；能用代码默认表达的边界不要暴露成开关。
- 只有模型路由和 API 参数进 `config/llm.yaml`；不要把 endpoint、key、temperature 写进 Agent YAML。
- Worker 需要权限就写在 Worker YAML 或 app-level system；不要指望 Supervisor 传下去。
- 列表是替换，不是追加。Agent YAML 覆盖 `toolsets` 或 `skills` 时要写完整意图。
- 确定性逻辑放 `agent_tools/*.py`，推理协议放 workflow，长期领域协议才放 Skill。
- 需要 Hook 时创建应用私有 Hook Bundle，通过顶层 `hooks.bundles` 显式引用；不要创建承载 Hook 的 Skill。
