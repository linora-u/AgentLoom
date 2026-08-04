# Goal Mode

Goal Mode 用于让一个顶层 Supervisor 持续推进一个长期目标。普通 Agent 在一次
`final_answer` 或一个 `max_steps` 段结束后就会返回；Goal Mode 则保留同一个
runtime、对话记忆、Worker 调用状态和 checkpoint，自动发送精简 continuation，
直到根 Supervisor 显式提交完成，或 token 预算阻止下一次模型请求。

## 配置

`goal` 只能写在顶层 Supervisor YAML：

```yaml
# 简写：启用且不设置 Goal token 上限
goal: true
```

```yaml
# 完整形式：mapping 必须显式包含 enabled
goal:
  enabled: true
  token_budget: 120000
```

```yaml
goal: false
```

只接受布尔值，或只包含 `enabled`、`token_budget` 的 mapping。`enabled` 必须是
布尔值；`token_budget` 必须是正整数，不能使用字符串、浮点数、0 或负数。
`enabled: false` 时不能同时设置预算。省略 `token_budget` 表示无限制，而不是使用
隐式默认值。Worker YAML 出现任何 `goal` key（包括 `false`）都会校验失败。

目标内容由框架确定性组合 `description`、`workflow` 和本次 runtime task，不需要再
配置 `objective`。推荐使用一个 `workflow: |` 多行字符串。Goal Mode 仍接受
`workflow: list[str]`，但会把列表按原顺序编号后合并成一个目标上下文，并且只执行
一次初始 run。未启用 Goal 时，Supervisor 的列表仍保持原有的逐项多次 run 语义。

## 生命周期与工具

一个根 task 只有一个 Goal，状态为：

- `active`：继续运行；普通 final 不代表完成。
- `budget_limited`：累计用量达到软预算，不再发起新模型请求；checkpoint 可恢复。
- `complete`：根 Supervisor 已调用 `update_goal(status="complete", evidence="...")`；终态不可回滚。

启用后，只有根 Supervisor 能看到 `get_goal` 和 `update_goal`。Worker 既看不到工具，
也会被工具处理器的 root-run 身份校验拒绝。`evidence` 必须非空；Todo 全部完成、
普通 final 或 host 侧猜测都不会自动完成 Goal。

Tool-calling runtime 在 `update_goal` 后可能还需要一次模型响应来提交 `final_answer`。
这次进程内结算只允许同一个根 Agent 使用；存在 tool schema 时只暴露 `final_answer`；
它只能消费一次，且不会写入 checkpoint。结算前若碰到 planning，框架会在本地跳过；
待触发的 smart summary 模型调用会改用确定性的本地截断；若刚好达到 max_steps，则无工具
的最终文本 fallback 可以消费同一许可。完成响应已经耗尽 token 预算时不创建结算许可。

初始 segment 接收完整目标。后续 segment 只接收稳定 `goal_id`、状态、已用/剩余预算和
继续指令，依靠既有对话记忆推进，不重复注入或重新执行 workflow；需要时根 Agent 可用
`get_goal` 重读 canonical objective。`max_steps` 是单个 continuation segment 的边界，
不是 Goal 失败或完成。完成已经写入 `goal.json` 后，即使最终回复尚未落盘进程就
崩溃，resume 也不会重新执行实质工作；持久化 evidence 是权威结果。

## Token 预算

`token_budget` 统计整个根 Agent 树中所有 Supervisor 和 Worker 模型响应上报的
`prompt_tokens + completion_tokens`。它是 task 生命周期累计值，失败、interrupt 和
resume 都不会清零。

预算是软边界：请求开始前只检查已经提交的用量；在途响应和已经并发启动的 Worker
允许完成，因此实际用量可能略高于预算。响应落地后原子累计；达到预算后，当前 run
和 Goal 都进入 `budget_limited`，保存 checkpoint，并阻止新请求。

恢复时可以提高 YAML 中的 `token_budget`，或删除该字段切换为无限制：

```bash
uv run loom run applications/<app>/workflows/<agent>.yaml --resume <task_id>
```

预算不能降低。保持一个已经耗尽的预算不变时，resume 仍会返回
`budget_limited`；必须把新预算提高到 `used_tokens` 之上，或移除上限。

## 持久化与可观测性

开启 checkpoint 时，canonical 状态位于：

```text
.agentloom/checkpoints/<application_id>/<task_id>/goal.json
```

它保存稳定的 `goal_id`、objective 指纹、状态、`goal_started`、预算、prompt/completion/总用量、
evidence 和时间戳。文件损坏会让恢复失败，不会像非关键缓存一样静默清空。
description、规范化 workflow 或 runtime task 改变后，旧 Goal 拒绝 resume；活动 Goal
对应的 YAML 改成禁用也会拒绝恢复。

每个 run 的 `manifest.json` 包含结构化 `goal`。终态还会复制到
`audit/goal.json`，所以成功清理 checkpoint 后仍保留审计证据。CLI 文本显示状态和
用量；`--output-format json` 输出一个携带同一 Goal 对象的终态事件；
`--output-format jsonl` 则在相关生命周期事件中携带它，包括 `run.completed`、
`run.budget_limited`、`run.failed` 和 `run.interrupted`。TUI 的运行列表和详情读取相同
状态。Python API 的预算终态抛出 `ApplicationRunBudgetLimited`，携带 run receipt、
`goal` 和可恢复 task id。

## 调度

Schedule 直接运行同一份 Agent YAML，因此支持相同的 continuation、Worker 聚合预算、
checkpoint 和 resume 语义。无限制 Goal 也允许被调度，但无人值守任务强烈建议设置
`token_budget`；否则它会一直 continuation，直到显式完成、人工中断或真实错误。调度器
使用隔离的 JSONL 生命周期通道识别预算终态；execution 和 job 的 `last_status` 会记录为
`budget_limited`（并保留 Goal 用量诊断），不会误报为普通 `failed`。提高或移除预算后，
使用该 execution 对应的 task id 执行 resume。

Goal Mode 不是 Todo、第二个评估模型、通用 workflow cursor 或 Worker Goal。Todo 可
用于内部计划，但不能改变 Goal 生命周期。
