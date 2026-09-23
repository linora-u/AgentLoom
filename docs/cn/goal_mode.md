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

Goal objective 只记录必填的非空 `workflow` 字符串和可选的 runtime task。Agent
`description` 仍是展示和 Tool 元数据，不进入执行输入。`workflow` 作为 Runtime
instruction；提供 task 时，它作为独立 user message，省略时不会生成替代消息。

## 生命周期与工具

- `active`：继续工作，普通 final 不代表完成。
- `complete`：根 Supervisor 调用 `update_goal(status="complete", evidence="...")`。

只有根 Supervisor 可见并可调用 `get_goal` 和 `update_goal`。完成需要非空证据，
且不可回滚、重复调用不覆盖原证据。Worker 仍受工具身份校验约束。Todo 不改变 Goal 状态。

续跑使用同一个 runtime 和对话，后续提示只携带 Goal 身份、状态与继续指令，不重启已完成
工作。显式完成后，根 Supervisor 可使用一次进程内请求提交最终回复，该请求只提供
`final_answer`；planning 和 smart summary 不消耗这次许可。恢复已完成 Goal 时直接
返回持久证据，不重新执行工作。

## 恢复与可观测性

启用 checkpoint 后，`<application_id>/<task_id>/goal.json` 保存身份、目标指纹、状态、
`goal_started`、证据与时间戳。恢复保留 Goal，并校验 workflow 和 runtime task 一致。
损坏状态或禁用活动 Goal 仍沿用原有错误语义。

旧 checkpoint 的预算和 Goal 用量字段静默忽略；旧 `budget_limited` Goal 按 `active`
恢复，已完成 Goal 仍保持完成。无需提高预算或迁移配置。

```bash
uv run loom run applications/<app>/workflows/<agent>.yaml --resume <task_id>
```

Run manifest 和生命周期事件携带 canonical Goal 状态；成功清理 checkpoint 前会复制到
`audit/goal.json`。CLI 文本显示 Goal 状态，Studio 显示目标、状态和完成证据。普通模型
用量仍保留在运行时审计记录中，独立于 Goal 状态。

Schedule 使用同一 YAML 和相同的续跑、完成与恢复语义，直到显式完成、中断或错误。
