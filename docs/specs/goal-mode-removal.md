# 删除 Goal 模式

2026-09-20。维护者认为 Goal 预算和自动持续推进没有实际工作价值，要求删除以简化框架。
本次删除整个 Goal 模式，而非保留没有预算上限的自动续跑。

范围：删除 Goal 配置模型、状态与持久化、`get_goal` / `update_goal`、自动 continuation、
模型请求前预算拦截、完成后的额外结算，以及 `budget_limited` 的运行、调度和界面分支。
专用 Application、测试与当前使用文档一并删除。历史验证报告保留，并标明其记录的是删除前行为。

保留的行为：

- 普通任务完成后直接返回；达到 `max_steps` 按原有失败语义处理，不自动另开一段。
- Supervisor 的 workflow 列表仍顺序执行，每项一次 runtime run，并保留项间会话。
- Worker、并行工具、Todo、模型用量审计、Hook、ContextEngine 和普通 checkpoint 恢复继续工作。
- Supervisor 和 Worker 配置中出现任何 `goal` 值都在预检阶段明确拒绝，包括 `false`。
- 旧 Goal checkpoint 不会按普通任务恢复；报错后应新建任务，不删除原始证据。
- CLI / Python / TUI / 调度不再提供 Goal 字段、工具、状态或预算提示。

实现提交：`cdd7d9f4` 删除功能，`9b9d015c` 将旧 checkpoint 的拒绝检查收敛到
Application 和直接 `Agent.run` 共享的恢复入口，补充直接调用回归。

## 验证

真实 Application 验证使用本机忽略的 `config/llm.yaml`，通过公开 `execute_app`
入口请求实际 LLM。Responses 组对 Supervisor、Worker 和 summary 统一选择
`openai_responses`，并逐个检查 checkpoint 中的实际 adapter。没有使用模型桩。

以下 16 个新运行在 `cdd7d9f4` 上完成，Chat 与 Responses 各 8 个，全数通过：

| 场景 | Chat Completions | Responses |
|---|---|---|
| 工具报错后重试、Hook 拦截写入 | 通过 | 通过 |
| Unit Test Studio | 通过；宿主执行生成测试 11 项 | 通过；宿主执行生成测试 11 项 |
| Repo Map | 通过 | 通过 |
| ContextEngine text / JSON / multi-worker | 三项通过 | 三项通过 |
| Core / Markdown 工具 Application | 两项通过 | 两项通过 |

16 个运行产生 24 次 Worker 启动，checkpoint 中保留 131 个不同模型响应 ID
（Chat 61、Responses 70）；该计数不是完整网络请求数。每个运行均断言没有新建
`goal.json`，Run manifest 没有 Goal 字段且状态为 `completed`。

`cdd7d9f4` 上真实 Chat 的主流程中断、Worker 中断、已完成 Worker 回放三类
checkpoint 验证全部通过。共享恢复入口修改后的 `9b9d015c` 再次通过已完成
Worker 回放：任务身份保留，新 Run 创建，旧 ContextRef 和文件历史可用，
副作用各发生一次，Worker 不重跑且用量不变。

最终实现 `9b9d015c` 的确定性检查：

- Python 完整 CI：**4078 passed，1 skipped，3 warnings，265.98 秒**。
  警告来自 LiteLLM 的 Pydantic 弃用和既有响应对象序列化测试。
- TUI：**200 passed，0 failed，752 assertions**；类型检查、生产构建均通过。
- 旧 Goal 配置拒绝和直接 Agent 恢复回归均先失败、修复后通过。
- `git diff --check` 通过。

```sh
PYTHONPATH=. .venv/bin/python -m pytest tests/ \
  applications/memory_feature_validation/scripts/test_memory_review_campaign_capsule.py \
  applications/memory_feature_validation/scripts/test_memory_review_campaign_contract.py \
  -q --tb=short --color=no

# 在 agentloom-tui 目录执行
bun run typecheck
bun test --timeout 30000
bun run build
```

Standards 审阅：无剩余硬违反或判断性代码 smell。
Spec 审阅：无剩余需求缺口；直接 Agent 恢复绕过旧 Goal 检查的问题已关闭。

本轮没有重新验收 Anthropic、Self-learning 或复杂架构任务；其此前结果及失败
仍保留在 [历史验证报告](runtime-recovery-validation.md)。

本机原始证据保留于 `/tmp/agentloom-goal-removal-validation-20260920/`：
`chat/summary.json`、`responses/summary.json` 及各场景的 assertions、日志、
checkpoint 和产物；`checkpoints/*/report.json`；`checkpoint-final/completed/report.json`。
此前生成的旧 Goal Application 已移到该目录的 `archived-goal-applications/`，
避免继续被当前 Application 列表发现。终端截图保留原始历史画面并更新说明。
