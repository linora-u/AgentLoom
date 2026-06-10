# 验证与评审

## 必跑校验

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
