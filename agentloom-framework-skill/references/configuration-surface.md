# AgentLoom 配置面总览

本文件回答两个问题：一个配置能不能写，以及应该写在哪里。优先以当前代码为准，`docs/en` 是说明材料；两者冲突时，以代码和测试为准，并把冲突记录为待修文档。

## 证据入口

配置相关改动至少交叉看这几类文件：

- 用户文档：`docs/en/config-overview.md`、`agent_config.md`、`system_config.md`、`llm_config.md`、`skills_config.md`、`hooks.md`、`checkpoint.md`。
- 系统配置加载：`src/lib/config/config.py`、`layered_builder.py`、`config_validation.py`。
- LLM 配置：`src/lib/config/llm_config.py`、`src/lib/smolagents/models/model_types.py`、`model_manager.py`。
- Agent YAML 校验：`src/lib/smolagents/agent/agent_validation.py`、`yaml_agent_factory.py`、`base_agent.py`。
- Skill/Hook：`src/lib/smolagents/skills/parser.py`、`skills.py`、`src/lib/smolagents/hooks/*`。
- MCP：`src/mcp/config.py`、`tests/mcp_test/*`。

## 配置文件与层级

| 配置面 | 写在哪里 | 用途 | 关键规则 |
|---|---|---|---|
| 全局系统配置 | `config/system.yaml` | 工具、权限、shell、prompt、skills、checkpoint 等系统行为 | 参与 deep merge；列表整段替换 |
| 本地模型配置 | `config/llm.yaml` | 模型类型、密钥、网关、推理参数、限流、重试 | 独立加载，不被 app/Agent YAML 覆盖；通常被 `.gitignore` 忽略 |
| 应用级系统覆盖 | `applications/<app>/config/system.yaml` | 当前应用专属的系统行为覆盖 | 从 Agent YAML 路径向上找到最近 `workflows/`，其父目录即 app root |
| Agent YAML | `applications/<app>/workflows/*.yaml` | 单个 Agent 的角色、workflow、工具、模型类型、运行模式 | 只有白名单字段会 overlay 到系统配置，其余是 Agent 自身属性 |
| Worker YAML | `applications/<app>/workflows/worker_agents/*.yaml` | 被 Supervisor 调用的 Agent 工具 | 有 `agent_function_schema` 才能导出为 callable tool |
| Skill 包 | `applications/<app>/skills/<name>/SKILL.md` 或 `skills/<name>/SKILL.md` | 可按需加载的长期能力、hooks、脚本和资源 | `SKILL.md`/`skill.md` 入口；散落 `.md` 和 `skills.md` 不加载 |
| MCP 配置 | `mcp_servers` 指向的 JSON 文件 | 外部 MCP server 工具 | `mcp_servers` 支持 string/list/dict 三种 YAML 形式 |

合并顺序：框架默认值 -> `config/system.yaml` -> `applications/<app>/config/system.yaml` -> Agent YAML 白名单字段。字典递归合并；列表和标量整体替换。

LLM 配置不参与这个链条。`model`、`llm`、`langfuse` 写进 `system.yaml` 或 Agent YAML 会被过滤并警告。

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
| `planning_interval` | 正整数或数字字符串 | 周期性 planning；设置后自动注入 `todo_write` |
| `concurrency` | 正整数或 `"auto"` | 仅影响同一 Worker 通过 `.batch()` 被多输入批量调用 |
| `execution_env` | `dict` | `code_act` 的执行环境：`local`、`docker`、`e2b`、`wasm` |
| `prompt` | `str` 或 `{path: ...}` | 自定义系统 prompt 模板路径 |
| `skills` | `str` / `dict` / `list` | 当前 Agent 私有 Skill 配置 |

`tool_call` 模式的主路径是 provider/native tool calls。只要当前 Agent 有可用工具，AgentLoom 就发送结构化 tools schema；如果 provider 返回文本 fallback，也只接受明确结构化容器，例如 `{name, arguments}`、dump 出来的 native `tool_calls/function`、XML/invoke wrapper。不要设计依赖自由文本正则兜底的 workflow。

Supervisor 专属：

```yaml
worker_agents:
  - path: "applications/<app>/workflows/worker_agents/<worker>.yaml"
```

规则：`worker_agents` item 只支持 `path`，不支持 `name`。路径可以是绝对路径、项目根相对路径、`worker_agents/` 下的文件名，或不带后缀的 worker 名。

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

## Agent YAML 白名单覆盖

当前代码里的 `_WORKFLOW_OVERLAY_KEYS` 是：

```text
system, smart_summary, context_engine, tool_access_control, execution_env,
code_agent, tools, shell_settings, tools_mapping, default_toolsets, toolsets,
prompt, mcp_servers
```

注意：

- 这不是 7 个字段；旧文档如果说只有 7 个已经过期。
- `tools` 在白名单里只有当它是 `list` 时才进入 overlay；它同时也是 Agent 的工具列表。
- `shell_settings`、`tools_mapping`、`toolsets` 可以在 Agent YAML 覆盖；`toolsets` 会整体替换全局 `default_toolsets`。
- `context_engine` 可以在 Agent YAML 覆盖，用于按应用或 Agent 调整可逆上下文压缩。
- `mcp_servers` 可以在 Agent YAML 覆盖，并支持 string/list/dict。
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
| `lsp_servers` | LSP 服务开关、重启次数、语言列表 |
| `execution_env` | 默认执行环境 |
| `code_agent` | `code_act` 可 import 模块和可调用内置函数 |
| `logging` | 日志开关、级别、文件路径和目录；以全局配置为准 |
| `default_toolsets` | 默认加载 toolset 名列表 |
| `toolsets` | Agent 级内置 toolset 覆盖，空列表表示无内置工具 |
| `shell_settings` | shell 命令白名单、安全检查、sandbox、后台任务、audit log |
| `tools_mapping` | Skill `allowed-tools` 和 Hook matcher 的平台别名映射 |
| `tool_access_control` | 工具路径访问控制 |
| `tool_metadata` | 工具输出截断、并发安全、分类、类型转换 |
| `tool_output_limits` | 上下文压缩阶段的工具输出保留上限 |
| `checkpoint` | checkpoint、resume、heartbeat 开关与保留策略 |
| `mcp_servers` | MCP server 配置入口 |
| `self_learning` | 会话记录、记忆分层（session/application/project）、蒸馏与预算 |

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

自学习基础设施：hook 驱动的会话记录（ledger + FTS）、三层持久记忆（session / application / project）、LLM 语义蒸馏与门控自动应用。

```yaml
self_learning:
  enabled: true              # 总开关：关闭后记录、注入、memory/session 工具、
                             # SessionEnd 评审（蒸馏/自动应用/自动清理）全部停用
  root_dir: ".agentloom"     # 持久状态根目录
  reviewer_enabled: true     # 是否由 SessionEnd 持久队列执行语义评审
  events_retention_days: 90  # 每日去重的 retention job 清理超龄 runs/events；0 关闭
  memory:
    prompt_max_chars: 12000   # run 开始时注入快照的总字符预算
    max_item_chars: 4000      # 单条记忆上限，超出直接拒绝
    scope_budgets:            # 各 scope 活跃记忆容量；直写/apply 超限时返回
      project: 8000           # capacity_exceeded 与现有条目，要求 batch 整合
      application: 6000
      session: 4000
    session_ttl_days: 14      # loom memory prune 清理过期 session 记忆
    distill_enabled: true     # 持久 session-review job 的蒸馏总开关
    distill_model: "summary"  # LLM 语义蒸馏用的模型档（llm.yaml 的 model type）；
                              # 空串 = 仅蒸馏显式 session note，零模型开销
    auto_apply: "safe"        # off = 提案全部等人工 apply；safe = 过全部安全门槛的
                              # add 提案自动生效（审计为 applied_by=auto）
```

运行契约：

- 记忆分层：`project` 全局共享；`application` 按应用隔离（scope_id=application_id）；`session` 按 run 隔离（scope_id=run_id），run 结束后由蒸馏转成 application/project 层 pending 提案并归档。
- 模型侧 `memory` 工具对 project/app 只能写提案；session 层直写生效。批量整合用 `action="batch"` + `operations` JSON 数组，原子应用并按最终状态校验容量。同一 run 内 `capacity_exceeded` 连续 3 次后返回终止性结果，阻止无限重试。`action="feedback"`（helpful=true/false）对已注入记忆做显式信任反馈。
- **持久异步蒸馏**：SessionEnd 的唯一同步 hook 只在一个 `BEGIN IMMEDIATE` 内写最终 event、CAS run outcome/trust，并去重插入 `session_review` 与每日 retention job；提交后 best-effort 拉起 detached worker，hook 不调用模型。`learning_jobs` 使用全局 worker lease + 单 job lease token fencing，过期 running 可恢复，模型最多 3 次 job attempt（2s、10s），第三次失败只用显式 session note 做确定性 fallback。digest 在首次 claim 后冻结并持久化，重试复用同一 hash/input；DB 结果先提交，artifact 失败不会重调模型。
- **统一安全边界**：task、final answer、event、session note、existing memory、repeated failure、curator 输入统一经过 `DigestBuilder`：递归脱敏后做 Unicode/injection 扫描，命中 fragment 整体变为 `[BLOCKED]`，模型只接收带 `ref/kind/text/blocked` 的 JSON。模型提案必须引用 digest 中有效的 `evidence_refs`，并再次经过脱敏、注入、scope、长度与 replace-target 校验。重复失败只作为模型证据，不再由模板自动生成指令式记忆。
- **自动应用（auto_apply: safe）**：仅 add 提案；须通过注入扫描、与 active 记忆无冲突（apply 写事务内复查）、**规范化完全一致且来自 ≥2 个不同 root run 的佐证**、容量检查；LLM 没有豁免，每次 job 最多应用 5 条，replace/remove 始终人工。审计保存在具体 memory revision id、memory_evidence 与 review_runs。
- **佐证语义**：`memory_evidence(item_id, root_run_id)` 只为折叠空白、忽略大小写后 hash 完全一致的内容计票；数字、路径、版本、否定、单位、标点或其他文本变化均是不同事实。同 run 的 batch/add 重复不会增加票数。Jaccard/重叠度只用于冲突提示和人工审阅，绝不作为佐证。
- **注入快照**：XML 闭合；条目按 相关性(与任务文本 Jaccard)×trust×时间衰减 排序装入；trust<0.2 的条目隔离不注入（保留在库）；超预算整条省略并注明数量；含注入模式的条目替换为 `[BLOCKED: ...]` 占位。注入过的条目记录到 memory_injections，run 成功后 trust +0.02；`loom memory feedback` 显式 ±（helpful +0.05 / unhelpful −0.10）。
- **冲突检测**：相似内容可写入 `conflicts_json` 并由 `loom memory conflicts` 报告，供人工判断“高度相似但事实不同”的条目；完全相同文本仍由 exact hash 去重。冲突是 auto-apply 门控和审阅信号，不会被当作佐证，也不会静默隐藏 active 记忆。
- **自动清理**：每日去重的 retention job 执行 session TTL prune + `events_retention_days` 清理（0 关闭）；空间回收需手动 `VACUUM`。`loom memory stats` 同时展示 job 状态和遗留库文件。
- **root-run 归属**：顶层 BaseAgent 在快照前显式 `bind_root_run()`；worker 通过 ContextVar 继承同一个 root，只有 owner 发送最终 SessionEnd。HookContext、Canonical Event v3、runs/events、snapshot 和 memory/session tools 都携带显式 root_run_id；空上下文 fail-closed 为 `missing_run_context`，绝不从全局 HookManager 猜测。
- **Unicode 加固**：注入扫描先在原始文本上检测不可见/双向控制码点，再 NFKC 折叠全角同形字后跑正则；写入 DB/FTS 前，命中注入的文本 fragment 整体替换为 `[BLOCKED]`，不保留攻击原文。
- **快照使用率**（第二轮）：各 scope 开标签携带 `used_chars="..." budget_chars="..."`（与 capacity_exceeded 同口径的 bucket 总量），前言提示模型接近预算时主动 batch 整合。
- **不可变 revision**：replace 将旧 row 标记为 `superseded`，新内容使用新 id、`generation+1` 和 `supersedes_id`；pending replace/remove 创建时固定 `target_item_id`，目标不再 active 时变为 `stale`。注入、trust、feedback 和 run outcome 均绑定具体 id，旧 revision 的资历不会传给新 revision。
- **运行摘要**：session-review job 成功后写入 `session_summary.md`；SessionEnd 返回不等待摘要模型。
- **记忆策展**：`loom memory curate [--scope] [--scope-id] [--model] [--dry-run]` —— LLM 策展一遍 active 记忆（project + 各 application bucket，session 由 TTL 管）：合并近重复、压缩冗长、按矛盾对提议清理。**全提案制**（source=curator 的 replace/remove pending 行，永不自动应用，`loom memory apply` 才生效）；代码级门槛：目标必须在 bucket 白名单、必须引用未阻断的目标 evidence、trust≥0.7 或 7 天内更新的条目受保护、内容过注入扫描、每次 ≤10 条提案。模型默认取 `memory.distill_model`。仅手动触发；未来自动化也必须使用去重的持久 outbox job。
- **导出**：`loom memory export [--out FILE] [--format json|markdown] [--include-events]` —— 全状态（active/pending/removed/archived）连同 trust/注入元数据导出，用于备份、迁移与人工审阅。
- 维护命令：`loom memory stats`（预算、job 状态、遗留文件）、`loom memory jobs [--status ...]`、`loom memory retry-job <id>`、`loom memory prune`、`loom memory distill <run_id>`、`loom memory conflicts`、`loom memory feedback <target> --helpful/--unhelpful`、`loom memory apply/reject`、`loom memory curate`、`loom memory export`。
- **信任语义**：replace 生效（人工 apply、直写、batch）后新 revision 的 `trust_score` 为 0.5、`helpful/unhelpful_count` 为 0；旧 revision 保留自身注入、outcome 与 feedback 审计，迟到的旧 run 信号只更新旧 id。凭据键先做 NFKC/casefold/camel/snake/kebab 归一，覆盖 secret/token/password/cookie/authorization/credential 与受控前缀的 `*_key`，并保留 `sort_key`/`token_count` 等安全负样本。

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

如果用户不确定 shell 权限怎么配，先按 `shell-security-audit.md` 跑真实 workflow，读 `shell_audit.log` 的 `[POLICY_SNAPSHOT]` 和拦截事件，再收敛 `allowed_commands`、`allowed_operators`、路径规则或 sandbox。

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

实际完整列表以 `src/tools/registry.py` 的 `ToolSpec` registry 为准。

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
hooks: {}
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

`load-mode` 只控制 prompt 注入：`on-demand` 进 catalogue，`eager` 注入全文。Hooks 在 skill metadata 加载时注册，不需要模型先调用 `load_skill()`。

`read_skill_resource(skill, path, offset, limit)` 读取包内资源，拒绝目录逃逸。`run_skill_script(skill, command, ...)` 在 skill 目录下运行脚本并写审计日志；`allow-scripts: false` 禁止脚本；`allow-network: false` 禁止常见网络命令。

## Hooks 配置

当前推荐且生效的声明位置是 Skill frontmatter：

```yaml
---
name: security-checker
description: Pre-tool security validation.
hooks:
  PreToolUse:
    - matcher: "Write|Edit|Bash"
      hooks:
        - type: command
          command: python ./scripts/check.py
          timeout: 10
          once: false
---
```

不要把 `hooks:` 直接写在 Agent YAML 顶层；配置桥存在，但没有接入 Agent 初始化主路径。

事件名：

```text
PreToolUse, PostToolUse, PostToolUseFailure,
SessionStart, SessionEnd,
Stop, StopFailure,
SubagentStart, SubagentStop,
TaskCreated, TaskCompleted,
PreCompact, PostCompact,
Setup, ConfigChange, Notification
```

类型：

- `command`：shell 命令；退出码 2 阻断 PreToolUse，其他非 0 记录为非阻断错误。
- `prompt`：单轮 LLM 校验，返回 `{ok, reason}`。
- `http`：HTTP POST，可白名单展开 env vars。
- `agent`：多轮 verifier agent。

`matcher` 支持省略或 `"*"`、精确/竖线分隔、正则。多个匹配 hook 并发执行；权限结果按 deny > allow > passthrough 聚合。

## checkpoint 配置

```yaml
checkpoint:
  enabled: true
  cleanup_on_success: true
  max_resume_age: 604800
  heartbeat_interval: 5
```

checkpoint 存在于每次运行的日志时间戳目录下，包含 `task_events.jsonl`、`task_tree.json`、Supervisor `checkpoint.json`、heartbeat、file-history，以及 Worker per-call checkpoint。`loom list-tasks`、`loom clean-tasks`、`loom run --resume <task_id>` 是验证入口。

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
- 需要 Hook 时优先做应用私有 Skill，通过 Skill `hooks:` 注册。
