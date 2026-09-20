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

验证结果在本次移除完成后补入。
