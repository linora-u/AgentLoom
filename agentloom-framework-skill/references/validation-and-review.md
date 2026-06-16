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
- Agent YAML 是否误写 LLM 参数、无效 `planning_interval`/`concurrency`、错误 `prompt`、错误 `fixed_args`、错误 `mcp_servers`。

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

## 框架运行时功能验证

修改 checkpoint、resume、日志/维测、并发 Worker、文件回滚、任务列表或任务清理时，不能只跑单测；至少选 2 个真实 Application 或已有真实 workflow 跑功能路径。优先使用仓库里的 `applications/test_demo/*checkpoint*`、`applications/test_demo/*file_rewind*`、`applications/feature_planner_demo` 这类覆盖面明确的应用。

建议用隔离运行根，避免污染用户已有 checkpoint：

```bash
export AGENT_LOOM_RUNTIME_ROOT=/tmp/agentloom-runtime-checkpoint
rm -rf "$AGENT_LOOM_RUNTIME_ROOT"
```

必须验证的证据：

- `loom run <workflow.yaml> "<task>"` 能创建 checkpoint 目录。
- 新 checkpoint 有 `task_events.jsonl`；`task_tree.json` 只是投影且能被 `loom list-tasks --detail` 展示。
- 多 Worker 或重复 Worker 调用场景下，`workers/<worker>/calls/<call_index>/checkpoint.json` 存在，`call_index` 不互相覆盖。
- resume 场景要实际执行 `loom run <workflow.yaml> --resume <task_id>`；若为了制造中断而提前停止，记录中断方式和恢复结果。
- 涉及 subagent/Worker checkpoint 时，要分别验证 Supervisor 中断恢复和 Worker 半路中断恢复；Worker 恢复必须证明没有新开重复 `call_index`，且能从 per-call memory checkpoint 继续。
- file-history 场景要检查 `file-history/snapshots.json` 和备份文件，确认早期备份没有被后续 snapshot 覆盖。
- 清理场景要实际跑 `loom clean-tasks --all`，确认新旧 worker checkpoint 布局都会被删除。

当前仓库可用的真实 LLM 验证脚本：

```bash
PYTHONPATH=/Users/bytedance/code/data_clear/AgentLoom-checkpoint \
/Users/bytedance/code/data_clear/AgentLoom/.venv/bin/python \
  tests/agent_test/real_checkpoint_validation.py --scenario all
```

它会运行 `applications/test_demo/workflows/test_checkpoint_complex_supervisor.yaml`，分别制造 Supervisor 中断和 Worker 中断，并检查最终文件、task event、worker call 复用和 Worker memory restore。

如果真实模型调用因权限、额度或超时失败，不能标为通过；记录失败命令、错误文本、已产生的 checkpoint 证据，以及还缺哪条功能路径。

## 配置合同交叉验证

修改 `agentloom-framework-skill`、配置文档或 runtime 配置语义时，必须同时查文档与代码。不要只根据 `docs/en` 改 skill，因为文档可能落后于实现。

最小检查：

```bash
rg -n "_WORKFLOW_OVERLAY_KEYS|_LLM_ONLY_TOP_LEVEL_KEYS|extract_workflow_overlay" src/lib/config/config.py
rg -n "class RootSettings|class ToolAccessControlSettings|class LlmModelTypeSettings|extra_completion_params|supports_structured_output|tool_choice" src/lib/config src/lib/smolagents/models docs/en docs/cn agentloom-framework-skill
rg -n "install_agentloom_runtime_adapters|parse_structured_tool_call|ToolCallCandidate|schema-bound|tool_call_type" src/lib/smolagents src/lib/config tests docs/en docs/cn agentloom-framework-skill
rg -n "load-mode|allow-scripts|allow-network|Duplicate skill name|hooks:" src/lib/smolagents/skills src/lib/smolagents/hooks docs/en agentloom-framework-skill
rg -n "mcp_servers|parse_mcp_servers_yaml_value" src tests docs/en agentloom-framework-skill
```

需要核对的事实：

- Agent YAML 白名单字段是否与 `_WORKFLOW_OVERLAY_KEYS` 一致。
- `model` / `llm` / `langfuse` 是否仍被 `_LLM_ONLY_TOP_LEVEL_KEYS` 过滤。
- `RootSettings`、`LLMConfig`、`LlmModelTypeSettings` 是否新增可配置字段。
- `tool_choice` 是否仍只是模型请求透传参数；不要把它写成 native tool-call 能力探测开关。
- `tool_call` 模式是否仍只接受结构化 native/tool-call block；不要恢复自由文本猜测、fuzzy tool-name repair 或坏参数 `{}` 兜底。
- `skills` 的格式、默认值、同名处理、hook 注册时机是否与 `SkillsManager` 一致。
- `hooks` 是否仍只通过 Skill frontmatter 接入 Agent 初始化。
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
- 运行时发现：如果纯规划 demo 不需要默认文件工具，应显式设置 Agent 级 `default_loaded_tools: []`；如果要关闭全局自动发现 Skills，应在应用级 `config/system.yaml` 写 `skills: []`，不是只写 Agent YAML 的 `skills: []`。

## 架构评审维度

| 维度 | 看什么 |
|---|---|
| Workflow 流程 | 阶段职责、依赖、分支、输出约束 |
| Supervisor/Worker 协调 | 输入传递、Worker 去重、协调开销 |
| Agent/Tool 边界 | 确定性逻辑是否 Tool 化 |
| 韧性 | 错误隔离、断点、重试、并发、可观测 |

每个问题都写成：证据、判断、建议、是否阻断。
