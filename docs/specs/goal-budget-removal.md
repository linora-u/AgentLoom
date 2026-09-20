# 删除 Goal 成本预算，保留 Goal 模式

2026-09-20，分支 `codex/remove-code-act-responses-runtime`。
维护者澄清：只删除有限成本/token 预算，Goal 模式保留。此前删除整个模式的改动已纠正。

- 保留 Goal 状态、目标指纹、自动 continuation、根 Supervisor 的 `get_goal` / `update_goal`、完成结算、checkpoint 恢复及 UI 中的目标和证据。
- 删除 Goal 预算字段、专属用量累加、请求预算拦截、预算异常、预算终态与相关调度/UI 分支。
- 旧 `goal.token_budget` 无论取值都静默忽略；旧 Goal checkpoint 的预算与用量字段不再读取或校验，旧 `budget_limited` 按 `active` 恢复。Goal 身份、证据和已完成状态保留。
- 普通模型用量审计保留。Goal 的目标一致性、根工具权限与损坏状态检查仍沿用原有规则。

实现提交：`76e18484`。先前 Responses 协议和 checkpoint 历史恢复修复继续保留。

## 确定性验证

- Python 完整 CI：**4142 passed，1 skipped，3 warnings，275.36 秒**；警告来自既有 LiteLLM/Pydantic 测试。
- TUI：**200 passed，0 failed，752 assertions**；类型检查、生产构建通过。
- 旧预算配置与旧 checkpoint 的 17 项回归先失败、修改后通过，覆盖无效预算值、旧预算耗尽、活动和完成状态。
- 针对性 282 项通过，包含普通 final/max_steps 后续跑、完成结算、Worker 权限、恢复、CLI 和 TUI。
- TUI 的旧 manifest 与 checkpoint 两种来源均验证静默忽略预算，原始历史文件未被修改。

```sh
PYTHONPATH=. .venv/bin/python -m pytest tests/ \
  applications/memory_feature_validation/scripts/test_memory_review_campaign_capsule.py \
  applications/memory_feature_validation/scripts/test_memory_review_campaign_contract.py \
  -q --tb=short --color=no
```

## 真实 Application 与 LLM

使用本机忽略的 `config/llm.yaml`，经公开 `execute_app` 请求真实模型；Responses
组显式绑定 `openai_responses`，并从每份 runtime checkpoint 核对 adapter。

| 场景 | 执行结果 |
|---|---|
| Chat Goal workflow list | 4 个 Worker 完成，报告落盘及 marker 检查通过，根 Supervisor 显式完成 Goal；checkpoint 保留 13 个模型响应 ID |
| Responses Goal workflow list | 同样 4 个 Worker 完成、报告验证和显式完成；16 个模型响应 ID |
| Responses 旧预算恢复 | 普通 final 后 Goal 保持活动；在持久化边界以 SIGINT 中断，向真实 checkpoint 注入旧预算耗尽元数据，YAML 预算改为无效字符串后成功恢复；5 个模型响应 ID |

旧预算恢复场景的历史预算字段由验收器注入，以隔离兼容性行为；模型响应和执行均为
真实请求。恢复保持同一 Goal/task，创建新 Run，交付文件仅写入一次，最终由
`update_goal` 写入完成证据。新 Goal 快照不再输出任何预算或 Goal 用量字段。

上述四 Worker Application 的**运行与产物断言通过**，不等于其生成的代码审计结论
为 PASS：Chat 报告为 FAIL，Responses 为 PARTIAL。它们只接收每文件 4 条源码
摘录，报告列出的主要障碍是无法从这些片段证明 Goal 身份和完整状态转换；这些
结论原样保留，没有改写。Goal 身份、续跑和恢复另由确定性回归及上述真实恢复
场景验证。本轮未重新执行 30 分钟 endurance 场景。

Standards 审阅：无剩余硬违反或代码 smell。
Spec 审阅：无剩余需求缺口；发现的旧 Goal TUI 投影问题已修复并复核。

本机证据：`/tmp/agentloom-goal-no-budget-validation-20260920/` 下的
`pytest-full.log`、`tui.log`、各场景 `report.json`、日志和 checkpoint；
`evidence-sha256.json` 记录三份真实运行结果的哈希。
