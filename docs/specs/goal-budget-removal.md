# 删除 Goal 成本预算，保留 Goal 模式

2026-09-20，分支 `codex/remove-code-act-responses-runtime`。
维护者澄清：只删除有限成本/token 预算，Goal 模式保留。此前删除整个模式的改动已纠正。

- 保留 Goal 状态、目标指纹、自动 continuation、根 Supervisor 的 `get_goal` / `update_goal`、完成结算、checkpoint 恢复及 UI 中的目标和证据。
- 删除 Goal 预算字段、专属用量累加、请求预算拦截、预算异常、预算终态与相关调度/UI 分支。
- 旧 `goal.token_budget` 无论取值都静默忽略；旧 Goal checkpoint 的预算与用量字段不再读取或校验，旧 `budget_limited` 按 `active` 恢复。Goal 身份、证据和已完成状态保留。
- 普通模型用量审计保留。Goal 的目标一致性、根工具权限与损坏状态检查仍沿用原有规则。

验证结果在本轮测试结束后补充。
