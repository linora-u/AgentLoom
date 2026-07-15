# Hermes Memory 设计与 AgentLoom 简化结论

日期：2026-07-14

Hermes 基线：`NousResearch/hermes-agent@2d0f2185cf3bbf996128dfd5341eea1395b3aca7`

AgentLoom 基线：当前 `dev`

## 结论

Hermes 把“会话历史”和“精选长期记忆”分成两个系统：

- 会话历史完整落库并通过 FTS 搜索，不会自动晋升为长期记忆。
- 长期记忆容量很小，最终只能通过同一个 `memory` 工具修改。
- Hermes 的可选后台 review 是长生命周期进程中的 best-effort daemon
  线程；进程退出会丢任务，没有 outbox、lease 或重试。

AgentLoom 是一次性 `loom run`，因此不复制 daemon 线程，也不保留此前的
proposal/evidence/trust/revision/outbox 状态机。最终采用一个更窄的可选机制：

```text
review_model 未配置
  SessionEnd 写 history -> loom run 返回

review_model 已配置
  成功 SessionEnd 写 history -> 同步隔离 reviewer -> 最多执行受证据约束的 add
                            -> reviewer 完成后 loom run 返回
```

这不是后台任务。配置了 review 的用户明确接受一次额外模型调用及相应退出
延迟；不配置时没有额外调用。

## 1. Hermes 的真实设计

### 1.1 History 与 Memory 分离

Hermes 把真实消息写入 SQLite/FTS，供 `session_search` 等工具按需读取。
搜索结果不会生成摘要，也不会自动写入长期 memory。

长期记忆是小型 `MEMORY.md` / `USER.md` 文件，在 session 开始时形成冻结
快照并注入 Prompt。session 中途写入只影响下一次快照。默认容量很小，满后
由 Agent 主动删除或压缩旧内容。

### 1.2 两个入口，共用一个写工具

Hermes 的前台 Agent 可以显式调用 memory；可选 background review Agent
也只能调用同一个 memory 工具。工具提示模型只保存稳定偏好、项目约定、
环境事实和真正可复用的信息，明确跳过 progress、完成日志和临时 TODO。

Hermes 没有代码层的“长期价值证明器”。它依赖模型判断、小容量、安全扫描
和可选人工审批来控制质量。

### 1.3 Background review 不是可靠队列

Hermes 默认按累计用户 turn 触发 review，而不是在每次 SessionEnd 强制运行。
它在同一进程中创建隔离 Agent，只开放 memory/skill 等白名单工具，然后放入
`daemon=True` 线程：

- 不保存线程句柄，也不 `join()`；
- 进程退出时未完成的 review 会丢失；
- 没有 job、attempt、lease、fencing 或 dead letter；
- 失败只记录日志，不重新入队。

这适合长生命周期 CLI/gateway 的 best-effort 优化，不适合直接复制到一次性
`loom run`。

### 1.4 可选审批不会阻塞等待人

Hermes 的 `write_approval=false` 默认直接写；`true` 时把精确操作持久化为
pending，之后用 pending/approve/reject 命令处理。所谓“等待人工”是操作保持
pending，不是让模型线程挂起。

## 2. 原 AgentLoom 方案为什么复杂

旧路径试图从任意运行状态自动推导长期记忆：

```text
session note/final/error
  -> digest -> model proposal -> evidence -> conflict -> auto apply
  -> trust/outcome -> revision -> durable outbox/retry
```

根因不是缺少一个 `progress:` 正则，而是边界错误。两个 root run 都出现
`finished step 3 of 5`，只能证明临时进度重复，不能证明它值得长期保存。继续
增加相似度、投票或状态只会让这个不可证明的判断更复杂。

## 3. AgentLoom 最终设计

### 3.1 History 永不自动晋升

```text
Hook events -> runs/events -> FTS -> session_search/session_scroll
```

task、progress、TODO、工具错误和 final answer 都属于 history。它们可被搜索，
但框架没有 fallback 或投票路径把它们直接提升为 memory。

### 3.2 Memory 只有一个写入边界

```text
前台 Agent ──> memory(list/add/replace/remove) ─┐
可选 completed-run reviewer ──> memory(add) ───┴─> active 或 exact pending
```

规则如下：

- 只支持 `project` 和当前 `application` scope；没有 session memory。
- 写入前统一递归脱敏并扫描 prompt injection。
- reviewer 只能新增事实；代码直接拒绝 reviewer 的 replace/remove，前台 Agent
  的显式 replace/remove 不受影响。
- 普通工具结果默认不构成写入证据。只有工具实现绑定的代码侧 extractor 能把
  原文标注为 `kind="durable_fact"`、显式声明 `scope="project"|"application"`
  并放入带进程内标记的 envelope；
  SessionRecorder 将它与事件原子写入独立表，普通 event JSON 不保存该
  字段，因此结果数据和 JSONL 导入都无法伪造。原始 progress/完成声明也不会
  自动获得资格；框架不使用 progress 关键词或语义正则猜测内容是否值得长期保存。
- reviewer 最多完整照抄当前 root 中一条未阻断的 trusted evidence；
  截短、paraphrase、跨 fragment 拼接和 final summary 独立声明都不能授权写入。
- add 先在进程内暂存，reviewer 正常结束后才提交；同一 root 原子 claim
  一次，模型失败不会留下半条 active/pending memory。
- `write_approval=false`：操作直接 active。
- `write_approval=true`：原操作精确持久化为 pending，由 CLI approve/reject。
- 只有 active memory 会进入下一次 run 的冻结 Prompt snapshot。
- 没有 evidence 投票、trust、feedback、conflict、revision、auto-apply 或 batch API。

### 3.3 Review 是显式配置的同步尾阶段

配置为空时：

```yaml
self_learning:
  memory:
    review_model: ""
```

SessionEnd 后不会构造 completed-run digest、解析 proposal 或调用蒸馏/reviewer
模型，前台显式 memory 仍然可用，因此“关闭 review”不等于“关闭 memory”。

配置模型时：

```yaml
self_learning:
  memory:
    review_model: summary
```

root owner 在 SessionEnd history 已提交后、`loom run` 返回前运行一次 reviewer：

- reviewer 使用独立执行上下文，不继承父 Agent 的工具集合；
- 唯一持久化工具是生产 `memory` 工具；
- 输入只来自当前 root 的有界 ledger fragment；
- fragment 先递归脱敏，再做 Unicode/injection 扫描，命中整段变为
  `[BLOCKED]`；
- 工具输出仍可作为上下文，但只有工具实现通过 `trusted_memory_evidence`
  绑定的 extractor 明确产生 `kind="durable_fact"` 且显式声明
  `scope="project"|"application"` 的 envelope 才是候选证据；application ID
  只从原 event 获取，模型不能扩大或缩小 scope；未声明 extractor、kind 或
  scope 的工具输出永远不能授权自动写入；
- reviewer 最多完整照抄一条未阻断的 trusted evidence；即使只有 final
  summary，配置后的 reviewer 仍会运行，但生产写入会被拒绝；
- reviewer 只能 add，replace/remove 在工具调用到达存储层前被拒绝；
- add 先暂存，通过校验的单次 add 是受控终止动作；提交时重新核对原 evidence，
  memory effect 与 completed audit 在同一个 SQLite 事务提交。同一 root 用
  进程锁和 OS 文件锁串行化，不写持久 running claim；失败不留下部分 effect；
- 不重试，不创建后台 job，失败不改变主任务结果；
- 只记录无正文 telemetry，例如模型名、调用次数、token 数和 memory action 数。

这个同步选择回答了“一次性进程如何保证 review 不丢”：进程必须等 review
结束。同时把复杂度限制在一次明确的函数调用，而不是重新引入 outbox。

### 3.4 配置面

```yaml
self_learning:
  enabled: true
  root_dir: .agentloom
  events_retention_days: 90
  memory:
    prompt_max_chars: 12000
    max_item_chars: 4000
    scope_budgets:
      project: 8000
      application: 6000
    review_model: ""       # 空 = 不 review；summary = 同步 review
    write_approval: false  # true = exact pending
```

不再存在 `distill_enabled`、`distill_model`、`auto_apply`、session TTL 或 memory
job/lease/retry 配置。

### 3.5 v5 数据模型

`runs/events/FTS` 继续保存 history。memory 只保留：

```text
memory_items
  id, scope_type, scope_id, content, content_hash, created_at, updated_at

memory_pending_writes
  id, status, action, scope_type, scope_id, payload_json,
  source_run_id, created_at, resolved_at

review_runs
  review_key, root_run_id, application_id, model_type, status,
  content-free result_json, created_at, finished_at

trusted_review_evidence
  event_id, root_run_id, tool_name, kind, source, text, created_at
```

`trusted_review_evidence` 只是 runtime provenance 边界，不是投票或自动生效
状态机；公开 JSONL import 不会写入该表。

升级时保留 runs/events 数量并重建 FTS；旧 session memory 丢弃；旧 auto active
降为 pending；无法唯一解析目标的 replace/remove 标为 stale；旧 evidence、job、
artifact 和正文 review 输出不进入 v5。

## 4. 验证策略

确定性测试覆盖：

1. project/application 隔离和 active-only snapshot；
2. exact pending 的 approve/reject 幂等与 stale target 检测；
3. 并发写入、容量上限和 SQLite 完整性；
4. secret/injection 不进入 DB、FTS、digest、snapshot 或 artifact；
5. v4→v5 即使旧 sanitizer marker 伪造也必须重新脱敏；
6. 旧 outbox 表被旧进程重建后，v5 再初始化仍会清除；
7. `review_model=""` 的模型调用数为零；
8. `self_learning.enabled=false` 即使配置 review_model 也不调用模型。
9. 配置 review 时，final-only run 会调用模型但 memory action 数为零；
10. 只有未阻断、由工具代码明确标注 `kind="durable_fact"` 与精确 scope 的
    trusted evidence 能授权 add；原始 progress/完成声明、普通 result 字段、
    event/JSONL、无 scope 历史数据及伪造 envelope 均无效；
11. reviewer replace/remove 被拒绝，前台显式写入仍可用。

真实模型 campaign 使用生产 `loom run`：先 5 次 canary，再 100 次、最大并发
2。数据集覆盖 durable fact、纯 progress、混合噪声、secret/injection、scope、
approval 和 recall。oracle 与模型可见数据分离，语义失败不靠重试掩盖。

## 5. 决策影响

该设计保留“用户可选择任务结束后自动整理”的能力，但把默认值设为关闭：

- 默认用户不承担额外模型成本或退出延迟；
- 配置用户获得一次不丢失的同步 review；
- final summary、progress 或被阻断内容不能独立授权长期写入；
- reviewer 只新增经工具绑定的可信证据支持的事实，不改写或删除已有记忆；
- progress 不会经确定性 fallback 自动晋升；
- 不需要后台 worker、job、lease 或重试状态机；
- 不需要人工时保持 `write_approval=false`；
- 需要人工时 pending 可跨进程保留，不阻塞任务。

## 6. Hermes 一手源码依据

- [MemoryStore 顶层设计、冻结快照和单一工具](https://github.com/NousResearch/hermes-agent/blob/2d0f2185cf3bbf996128dfd5341eea1395b3aca7/tools/memory_tool.py#L3-L24)
- [memory 工具的保存和跳过规则](https://github.com/NousResearch/hermes-agent/blob/2d0f2185cf3bbf996128dfd5341eea1395b3aca7/tools/memory_tool.py#L1064-L1129)
- [写入和加载时的 threat scan](https://github.com/NousResearch/hermes-agent/blob/2d0f2185cf3bbf996128dfd5341eea1395b3aca7/tools/memory_tool.py#L168-L241)
- [文件锁和原子写入](https://github.com/NousResearch/hermes-agent/blob/2d0f2185cf3bbf996128dfd5341eea1395b3aca7/tools/memory_tool.py#L243-L278)
- [background review 的模型路由和 digest](https://github.com/NousResearch/hermes-agent/blob/2d0f2185cf3bbf996128dfd5341eea1395b3aca7/agent/background_review.py#L31-L179)
- [background review 的上下文隔离和工具白名单](https://github.com/NousResearch/hermes-agent/blob/2d0f2185cf3bbf996128dfd5341eea1395b3aca7/agent/background_review.py#L617-L861)
- [daemon thread 创建](https://github.com/NousResearch/hermes-agent/blob/2d0f2185cf3bbf996128dfd5341eea1395b3aca7/run_agent.py#L1602-L1629)
- [每 N turn 触发 memory review](https://github.com/NousResearch/hermes-agent/blob/2d0f2185cf3bbf996128dfd5341eea1395b3aca7/agent/turn_context.py#L336-L344)
- [write approval 设计](https://github.com/NousResearch/hermes-agent/blob/2d0f2185cf3bbf996128dfd5341eea1395b3aca7/tools/write_approval.py#L1-L40)
- [session_search 使用 SQLite FTS5](https://github.com/NousResearch/hermes-agent/blob/2d0f2185cf3bbf996128dfd5341eea1395b3aca7/tools/session_search_tool.py#L1-L29)
