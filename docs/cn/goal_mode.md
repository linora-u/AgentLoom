# Goal Mode

Goal Mode 让根 Supervisor 跨多个 runtime segment 持续推进同一个目标，保留对话、
Worker 状态和 checkpoint。普通 `final_answer` 或 `max_steps` 边界会触发下一段
continuation；只有显式完成才结束 Goal。中断和真实错误仍会停止当前运行。

## 配置

仅顶层 Supervisor 可以启用：

```yaml
goal: true
# 等价于 goal: {enabled: true}
```

省略该字段或使用 `goal: false` 即禁用；mapping 必须包含布尔 `enabled`。
旧 `token_budget` 无论取值都静默忽略，Goal 不再有成本/token 预算上限。
Worker YAML 不能配置 Goal Mode。

Goal objective 是 Agent YAML 必填 `task` 中当前执行的一项。若 `task` 是列表，
每项在同一 Agent 会话中分别建立一个 Goal；当前项显式调用
`update_goal(status="complete")` 并提交后，才发送下一项。可选的
`system_prompt` 作为 Runtime instruction；`description` 仍只用于展示和 Tool 元数据。

## 生命周期与工具

- `active`：继续工作，普通 final 不代表完成。
- `complete`：根 Supervisor 调用 `update_goal(status="complete", evidence="...")`。

只有根 Supervisor 可见并可调用 `get_goal` 和 `update_goal`。完成需要非空证据，
且不可回滚、重复调用不覆盖原证据。Worker 仍受工具身份校验约束。Todo 不改变 Goal 状态。

续跑使用同一个 runtime 和对话，后续提示只携带 Goal 身份、状态与继续指令，不重启已完成
工作。显式完成后，根 Supervisor 可使用一次进程内请求提交最终回复，该请求只提供
`final_answer`；planning 和 smart summary 不消耗这次许可。完成证据属于 Goal 状态，
Agent 没有最终回复时，不用证据合成回复。

## 恢复与可观测性

启用 checkpoint 后，`<application_id>/<task_id>/goal.json` 保存身份、目标指纹、状态、
`goal_started`、证据与时间戳。恢复保留 Goal，并校验 YAML task 与定义一致。
损坏状态或禁用活动 Goal 仍沿用原有错误语义。

每项完成时，checkpoint 一起记录 Goal 阶段、下一任务序号与 Agent 会话状态。
恢复从首个未提交的任务项继续，不重发已经提交的用户消息。

旧 checkpoint 的预算和 Goal 用量字段静默忽略；旧 `budget_limited` Goal 按 `active`
恢复，已完成 Goal 仍保持完成。无需提高预算或迁移配置。

```bash
uv run loom run applications/<app>/workflows/<agent>.yaml --resume <task_id>
```

Run manifest 和生命周期事件携带 canonical Goal 状态；成功清理 checkpoint 前会复制到
`audit/goal.json`。CLI 文本显示 Goal 状态，Studio 显示目标、状态和完成证据。普通模型
用量仍保留在运行时审计记录中，独立于 Goal 状态。

Schedule 使用同一 YAML 和相同的续跑、完成与恢复语义，直到显式完成、中断或错误。
