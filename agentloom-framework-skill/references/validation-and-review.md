# 验证与评审

## 必跑校验

先做环境前置检查：

```bash
pwd
test -f config/llm.yaml
git check-ignore -v config/llm.yaml || true
```

`config/llm.yaml` 通常是本机私有配置，缺失时先从同机可信工作区复制或让用户提供；不要凭空生成，也不要提交。

```bash
.venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py \
  --app-root applications/<app_name>
```

通过标准：`summary.valid == true` 且 `error_count == 0`。

```bash
.venv/bin/python -c "
import sys
sys.path.insert(0, 'agentloom-framework-skill')
from scripts.scan_tools import scan_app_structure
print(scan_app_structure('applications/<app_name>'))
"
```

检查点：

- 是否有 Supervisor。
- Worker 数量是否符合设计。
- 每个 Worker 是否有 `agent_function_schema`。
- `tools` 是否与 workflow 动作匹配。
- `model_type`、`tool_call_type`、`max_steps` 是否合理。
- Agent YAML 是否误写 LLM 参数、无效 `planning_interval`/`todo.mode`/`concurrency`、错误 `prompt`、错误 `fixed_args`、错误 `mcp_servers`。
- Goal mapping 是否显式配置 `enabled`、预算是否为正整数、Worker 是否错误配置 Goal；Goal workflow list 是否按一个编号上下文运行。

```bash
.venv/bin/python -m py_compile applications/<app_name>/<app_name>_app.py
find applications/<app_name>/agent_tools -name '*.py' -print0 2>/dev/null | xargs -0 -r .venv/bin/python -m py_compile
```

## 可选运行验证

```bash
.venv/bin/loom create applications/<app_name>/workflows/<app_name>_agent.yaml \
  -o /tmp/<app_name>_generated_app.py
.venv/bin/python -m py_compile /tmp/<app_name>_generated_app.py
```

真正运行：

```bash
.venv/bin/python applications/<app_name>/<app_name>_app.py "<用户需求>"
```

运行会触发模型调用。失败时记录真实错误，不要伪装通过。

## 代码改动后的 Application 功能验证

代码编写、配置契约、Tool/Hook/ContextEngine/checkpoint/shell 等框架能力改动后，验证不能停在单测、schema 校验或 `loom create`。必须把“真实 Application 运行”作为功能验收层。

最低要求：

- 至少选择 3 条能覆盖改动面的真实 workflow；改动影响默认工具、ToolSpec、工具权限、上下文压缩、checkpoint、shell、worker 调度时，优先跑 5 条以上。
- 现有应用无法覆盖新契约时，新增最小验证 Application，目录保留在 `applications/<validation_app>/`，README 写明运行命令、覆盖的契约和预期证据。
- 每条 Application 要使用隔离输出目录或 `/tmp/agentloom-*` 路径，避免污染用户业务产物。
- 跑完必须读取日志和关键产物。只看退出码、只看最终回答、或只看“PASS”字样都不够。

日志审计要求：

- 文件日志默认按 `logging.file_enabled` 写入当前 run 的 `logs/runtime.log`；不存在 `--log-to-file`。记录 `manifest.json` 给出的 `application_id`、`task_id`、`run_id` 与真实 run 路径；需要验证关闭文件日志时才使用 `--no-file-log`。
- 检查日志里实际出现了预期 tool 调用、worker 调用、hook 拦截/放行、checkpoint/context/audit 写入等证据。
- 检查日志中的 `ERROR`、`Traceback`、`Exception`、`WARNING`、`parse failure`、`stale`、`blocked`；非阻塞警告也要判断是否符合预期。
- 对写文件、ContextRef、checkpoint、shell audit 这类功能，必须检查落地产物内容，而不是只相信模型总结。

推荐覆盖矩阵：

| 改动类型 | 至少跑的 Application |
|---|---|
| 默认工具 / ToolSpec / toolsets / implementation loader | `applications/tool_registry_core_validation`、`applications/tool_registry_markdown_validation`、`applications/test_demo/workflows/test_tool_resolve_agent.yaml`、`applications/context_engine_text_retrieve_validation`、`applications/self_learning_smoke`；catalog/loader 改动五条都跑 |
| 文件工具 / checkpoint file history | `applications/test_demo/workflows/test_edit_file_agent.yaml`、`test_file_rewind_agent.yaml`、`test_checkpoint_agent.yaml` |
| 搜索 / 代码导航 | `applications/test_demo/workflows/test_search_tools_agent.yaml` |
| ContextEngine / 压缩 | `applications/context_engine_*_retrieve_validation` 三个应用 |
| shell 权限 / audit | `applications/test_shell_audit/*`、`applications/test_shell_allowlist_matrix/*` |
| 多 Worker 调度 | `applications/context_engine_multi_worker_validation`、`applications/test_demo/workflows/test_checkpoint_complex_supervisor.yaml` |

交付时至少列出：

- 实际运行的 Application 数量和 workflow 路径。
- 每条的退出码、日志路径、final answer 或关键输出。
- 日志审计结论：哪些错误/警告是预期，哪些需要修复。
- 新增验证 Application 的路径和覆盖目的。
- 未跑的高风险 Application 及原因。

## 框架运行时功能验证

修改 ContextEngine/CCR、checkpoint、resume、日志/维测、并发 Worker、文件回滚、任务列表或任务清理时，不能只跑单测；至少选 2 个真实 Application 或已有真实 workflow 跑功能路径。优先使用仓库里的 `applications/test_demo/*checkpoint*`、`applications/test_demo/*file_rewind*`、`applications/feature_planner_demo` 这类覆盖面明确的应用。

建议用隔离运行根，避免污染用户已有 checkpoint。隔离子进程优先用 `AGENTLOOM_RUNTIME_ROOT` 覆盖整个 canonical runtime home；它会同时移动 runs、checkpoints、sessions、learning 和 `self_learning.db`，不会只改某一类存储：

```bash
export AGENTLOOM_RUNTIME_ROOT=/tmp/agentloom-runtime-checkpoint
```

只有在专门验证配置解析时，才在隔离 checkout 的项目根 `config/system.yaml` 修改 `runtime.root_dir`。`runtime`/`logging` 仍是 global-only，Application overlay 不能替代全局配置。执行前清理 `/tmp/agentloom-runtime-checkpoint`，验收结束后取消环境变量或恢复隔离 checkout 的配置。

必须验证的证据：

- `loom run <workflow.yaml> "<task>"` 能同时创建 `.agentloom/runs/<application_id>/<run_id>/manifest.json` 与 `.agentloom/checkpoints/<application_id>/<task_id>/`（使用自定义 root 时替换 `.agentloom`）。
- Manifest 必须记录 `application_id`、`task_id`、`run_id` 和最终状态；`logs/runtime.log`、`audit/shell.jsonl`、`artifacts/{shell,background,skills}` 只能写进当前 run。
- 新 checkpoint 有 `task_events.jsonl`；`task_tree.json` 只是投影且能被 `loom list-tasks --detail` 展示。
- 多 Worker 或重复 Worker 调用场景下，`workers/<worker>/calls/<call_index>/checkpoint.json` 存在，`call_index` 不互相覆盖。
- resume 场景要实际执行 `loom run <workflow.yaml> --resume <task_id>`；若为了制造中断而提前停止，记录中断方式和恢复结果。
- Goal 改动必须真实验证：普通 final 与 `max_steps` 后 continuation、根工具完成、Worker 工具隔离、Worker token 聚合、`budget_limited` 保留 checkpoint、提高/移除预算 resume、目标指纹拒绝、manifest/JSONL/TUI canonical Goal 对象。至少一条应是 30 分钟级复杂多 Worker Application，不能用简单问答替代。
- Resume 后必须证明 `task_id` 不变、`run_id` 改变，新旧 attempt 分属两个 run 目录，但都关联同一个 task checkpoint；heartbeat 和 run event 使用新 `run_id`。
- 涉及 subagent/Worker checkpoint 时，要分别验证 Supervisor 中断恢复和 Worker 半路中断恢复；Worker 恢复必须证明没有新开重复 `call_index`，且能从 per-call memory checkpoint 继续。
- file-history 场景要检查 `file-history/snapshots.json` 和备份文件，确认早期备份没有被后续 snapshot 覆盖。
- Checkpoint 清理场景要实际跑 `loom clean-tasks --all`；run retention 要跑 `loom clean-runtime`，证明不会删除 checkpoint、`.agentloom/workspaces/`、`.agentloom/legacy/` 或 Application outputs。
- 迁移要先跑 `loom migrate-runtime --dry-run`，再在隔离副本跑 `--apply`，证明忽略坏 `.task_index.json`、幂等/失败回滚、checksum 校验、有效 task resume、旧 ContextRef retrieve 与 file-history 恢复；最后确认旧 `.logs` 整体进入 `.agentloom/legacy/logs-v1-<timestamp>/`，新运行不再写 `.logs`。

ContextEngine/CCR 额外必须验证：

- 历史消息压缩后，tool/worker 原始输出进入 task-scoped `context_store/entries/*.json`。
- 模型可见内容是 `ContextRef` 预览，不是旧 temp-file 路径或不可逆截断。
- `loom_retrieve_context` 能按 ref、query、offset/limit 取回原文中的中间隐藏内容。
- 重复压缩已带 `ContextRef` 的历史不会二次压缩。
- 写入/编辑/删除类工具和 user/system 原始消息不会被压缩。
- checkpoint/resume 后，旧历史里的 ref 仍能 retrieve。
- JSON、search、log、多 worker 至少各有一条真实 Application 或集成验证覆盖；log 必须证明错误/traceback/尾部保留。
- 如果新增或修改配置字段，验证必须覆盖默认配置和应用级覆盖，并同步 `agentloom-framework-skill/references/configuration-surface.md`。

当前仓库可用的真实 LLM 验证脚本：

```bash
PYTHONPATH=/Users/bytedance/code/data_clear/AgentLoom-checkpoint \
/Users/bytedance/code/data_clear/AgentLoom/.venv/bin/python \
  tests/agent_test/real_checkpoint_validation.py --scenario all
```

它会运行 `applications/test_demo/workflows/test_checkpoint_complex_supervisor.yaml`，分别制造 Supervisor 中断和 Worker 中断，并检查最终文件、task event、worker call 复用和 Worker memory restore。

如果真实模型调用因权限、额度或超时失败，不能标为通过；记录失败命令、错误文本、已产生的 checkpoint 证据，以及还缺哪条功能路径。

## Shell 安全与审计验证

修改 shell 权限、审计、sandbox、路径安全、后台任务或 stall 检测时，不能只跑单测。按 `shell-security-audit.md` 先确认 audit log 能记录有效策略，再用真实 Application 验证该允许的允许、该拒绝的拒绝。

建议在隔离 checkout 的全局 `config/system.yaml` 中把 `runtime.root_dir` 设为 `/tmp/agentloom-runtime-shell-security`，并在执行前清理该目录。

核心真实 LLM 验证：

```bash
.venv/bin/loom run applications/test_shell_audit/workflows/test_shell_policy_snapshot_agent.yaml
.venv/bin/loom run applications/test_shell_audit/workflows/test_shell_audit_log_agent.yaml
.venv/bin/loom run applications/test_shell_audit/workflows/test_shell_audit_signals_agent.yaml
.venv/bin/loom run applications/test_shell_allowlist_matrix/workflows/test_shell_allowlist_matrix_agent.yaml
```

补充验证：

```bash
.venv/bin/loom run applications/test_demo/workflows/test_security_transparency_agent.yaml
.venv/bin/loom run applications/test_demo/workflows/test_background_task_agent.yaml
.venv/bin/loom run applications/test_demo/workflows/test_shell_stall_detection_agent.yaml
.venv/bin/loom run applications/test_demo/workflows/test_shell_session_isolation_supervisor.yaml
```

通过标准：

- LLM final 不能只写 PASS，必须列出当前 run 的 `manifest.json`、`logs/runtime.log`、`audit/shell.jsonl` 路径和关键证据行。
- runtime log 与 Shell audit 必须属于同一个 `<application_id>/<run_id>` run 目录，不能借用其他 run。
- 全允许场景必须有 `[POLICY_SNAPSHOT]`，且记录 `allowed_commands: *` / `allowed_operators: *`。
- 白名单场景必须证明允许命令成功、未允许命令被拒绝、未允许操作符被拒绝。
- `;` 等操作符拒绝的 suggestion 必须指向 `allowed_operators`，不得建议放进 `allowed_commands`。
- timeout/stall/background 场景结束后检查无 `sleep 300` 等残留进程。
- sandbox 不可用时记录真实 unavailable reason，不伪造 sandbox PASS。

## 配置合同交叉验证

修改 `agentloom-framework-skill`、配置文档或 runtime 配置语义时，必须同时查文档与代码。不要只根据 `docs/en` 改 skill，因为文档可能落后于实现。

最小检查：

```bash
rg -n "_WORKFLOW_OVERLAY_KEYS|_LLM_ONLY_TOP_LEVEL_KEYS|extract_workflow_overlay" src/lib/config/config.py
rg -n "class RootSettings|class ToolAccessControlSettings|class LlmModelTypeSettings|extra_completion_params|supports_structured_output|supports_native_tool_calls|tool_choice" src/lib/config src/lib/smolagents/models docs/en docs/cn agentloom-framework-skill
rg -n "install_agentloom_runtime_adapters|parse_structured_tool_call|ToolCallCandidate|schema-bound|tool_call_type" src/lib/smolagents src/lib/config tests docs/en docs/cn agentloom-framework-skill
rg -n "load-mode|allow-scripts|allow-network|Duplicate skill name|hooks:" src/lib/smolagents/skills src/lib/smolagents/hooks docs/en agentloom-framework-skill
rg -n "mcp_servers|parse_mcp_servers_yaml_value" src tests docs/en agentloom-framework-skill
```

需要核对的事实：

- Agent YAML 白名单字段是否与 `_WORKFLOW_OVERLAY_KEYS` 一致。
- `model` / `llm` / `langfuse` 是否仍被 `_LLM_ONLY_TOP_LEVEL_KEYS` 过滤。
- `RootSettings`、`LLMConfig`、`LlmModelTypeSettings` 是否新增可配置字段。
- `supports_native_tool_calls` 是否仍只作为 removed-field 拒绝逻辑存在；不要在 skill/docs/example 里重新教用户配置它。
- `tool_choice` 是否仍只是模型请求透传参数；不要把它写成 native tool-call 能力探测开关。
- `tool_call` 模式是否仍只接受结构化 native/tool-call block；不要恢复自由文本猜测、fuzzy tool-name repair 或坏参数 `{}` 兜底。
- `skills` 的格式、默认值、同名处理以及 `SKILL.md` 禁止 `hooks` 是否与 `SkillsManager` 一致。
- `hooks` 是否只通过顶层直接声明或显式 `HOOK.yaml` Bundle 编译，且保留三层来源顺序。
- `mcp_servers` 的 string/list/dict 三种形式是否仍被 parser 支持。
- `docs/en/config-overview.md`、`agent_config.md`、`system_config.md` 如果和代码冲突，最终 skill 先写代码真相，并在交付里说明文档漂移。

## 多 Agent 验证

多 Agent Application 至少满足：

- `workflows/<app>_agent.yaml` 有 `worker_agents`，数量 >= 2。
- `workflows/worker_agents/*.yaml` 文件存在。
- 每个 Worker 有合法 `agent_function_schema`。
- 结构扫描能列出 Worker Agents。
- README 写清 Supervisor/Worker 分工。

## 当前仓库验证基线（2026-06-10）

- `agentloom-framework-skill/scripts/*.py` 可 `py_compile`。
- `applications/feature_planner_demo` 是按本 Skill 创建的多 Agent 示例应用。
- `validate_application_yaml.py --app-root applications/feature_planner_demo` 通过：`valid=true`，`files_checked=3`，`error_count=0`。
- `scan_app_structure('applications/feature_planner_demo')` 证明它包含 1 个 Supervisor 和 2 个 Worker，两个 Worker 都有 `agent_function_schema`。
- `.venv/bin/loom create applications/feature_planner_demo/workflows/feature_planner_demo_agent.yaml -o /tmp/feature_planner_demo_generated_app.py` 通过，生成脚本可 `py_compile`。
- 真实运行验证在 120 秒上限内完成 Supervisor 启动和 `requirement_router` 调用，并进入 `implementation_blueprint`；未得到最终回答，记录为“运行路径部分通过，端到端输出未完成”。
- 运行时发现：如果纯规划 demo 不需要内置工具，应显式设置 Agent 级 `toolsets: []`；如果要关闭全局自动发现 Skills，应在应用级 `config/system.yaml` 写 `skills: []`，不是只写 Agent YAML 的 `skills: []`。

## 架构评审维度

| 维度 | 看什么 |
|---|---|
| Workflow 流程 | 阶段职责、依赖、分支、输出约束 |
| Supervisor/Worker 协调 | 输入传递、Worker 去重、协调开销 |
| Agent/Tool 边界 | 确定性逻辑是否 Tool 化 |
| 韧性 | 错误隔离、断点、重试、并发、可观测 |

每个问题都写成：证据、判断、建议、是否阻断。
