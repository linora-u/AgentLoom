# AgentLoom Todo Tool Refactor Specification

Status: Implemented and verified (2026-08-04)

## Problem Statement

AgentLoom 当前的 Todo 机制没有把 Todo 当作当前 task 的执行状态，而是形成了一套与 Agent 主循环、checkpoint 和生命周期割裂的旁路机制。

当 `planning_interval` 大于零时，运行时会自动注入 `todo_write`，并在 PlanningStep 后强制执行一个只能调用 Todo 的额外 ActionStep。这个行为不是模型根据任务复杂度自主决定的，还会增加模型调用、重试、临时修改 memory，并在 final 前引入额外检查。配置文档所描述的“复杂任务才创建 Todo”与实际强制执行行为不一致。

Todo 同时保存在进程级全局列表和 task workspace 下的 Markdown 文件中。Markdown 文件不是 checkpoint 的组成部分，缺少结构化 schema、可靠恢复、原子写入、清理、并发隔离和明确的失败语义。写入失败可能被吞掉，调用仍被当作成功；任务完成后文件也可能长期残留。因此问题不在于“使用本地文件”，而在于 Todo 有独立且不协调的生命周期，并形成多个事实源。

当前状态模型也无法准确表达自动化 Agent 的重规划。它只能表示 `pending`、`in_progress` 和 `completed`，导致不再需要的事项可能被伪装成 completed。工具没有严格保证唯一 `in_progress`，不能用空列表清除状态，也没有可靠的 task、Agent 和 resume 隔离。

用户需要一个更接近现代 Agent 框架的 Todo 工具：默认由模型自主判断复杂度；用户可以显式加强或关闭；Todo 与当前 task 的 checkpoint 生命周期一致；模型在 resume 和 context compression 后仍能可靠看到当前状态；旧的强制同步和 Markdown 机制可以直接删除，不需要兼容或迁移。

## Solution

将 Todo 重构为当前 task、当前 Agent 私有的结构化执行状态，并以新的 `todo_write` 工具作为唯一写入接口。

Todo 默认采用 `auto` 模式。工具对模型可见，模型依据统一的工具说明和简短的 Task Tracking 策略，自主决定复杂任务是否需要 Todo。用户可以配置 `on`，用跨模型一致的 `MUST` 强提示要求非平凡、多步骤执行任务在实质执行前创建 Todo；也可以配置 `off`，完全移除工具、提示和状态注入。任何模式都不通过宿主额外插入 ActionStep、重试模型调用、硬编码复杂度分类器或阻断 final。

`todo_write` 每次提交完整、有序的 Todo 快照。服务端先严格验证整个快照，再原子替换当前 Agent 的状态；任一 item 非法时整次更新失败，旧状态保持不变。空数组表示显式清除。

每个 item 仅包含执行所需的最小字段：`content`、`status`，以及仅在取消时使用的 `cancel_reason`。状态为严格枚举 `pending`、`in_progress`、`completed`、`cancelled`。列表顺序表达优先级，不增加 priority、依赖图或 item ID。列表级 `revision` 用于标识权威快照版本。

checkpoint 开启时，Todo 使用 task checkpoint 目录内独立的结构化 `todos.json`。它与 checkpoint 共用 task identity、resume、锁、删除和 retention 生命周期，但不写入 task tree 或 task event 全文，避免 Todo 高频更新影响执行树和事件读取预算。checkpoint 关闭时，Todo 只在当前进程、当前 run 内存中存在，不创建旁路持久化。

在每次模型调用组装上下文并完成 context compression 后，运行时从权威 Todo Store 读取当前 Agent 的最新快照，并以有界、可信的 system context 注入本次 model input。快照不写回 conversation memory，不增加 `todo_read` 工具，也不产生额外模型调用。这样 resume、进程重启和压缩均不依赖旧工具消息或摘要是否完整保留 Todo。

旧的 TodoSyncMixin、三阶段 Todo prompts、PlanningStep 后强制同步、final review、Markdown 读写、进程全局 Todo 状态及其专属兼容逻辑全部删除。`planning_interval` 继续作为通用 replanning cadence 配置存在，但与 Todo 完全解耦。

## User Stories

1. As an AgentLoom user, I want a complex task to create a Todo list autonomously, so that I do not need to request planning manually for every run.
2. As an AgentLoom user, I want a trivial task to skip Todo creation in `auto` mode, so that simple work does not pay unnecessary model and token overhead.
3. As an AgentLoom user, I want to set `todo.mode: on`, so that non-trivial execution tasks are strongly instructed to establish a Todo before substantive work begins.
4. As an AgentLoom user, I want `on` mode to allow only the minimum read-only discovery needed to understand scope, so that the resulting Todo is grounded in facts rather than invented prematurely.
5. As an AgentLoom user, I want pure questions and casual conversation to avoid meaningless Todo lists even when tracking is enabled, so that Todo remains an execution aid.
6. As an AgentLoom user, I want to set `todo.mode: off`, so that an Agent cannot see, call, or receive state from the Todo capability.
7. As an application author, I want a global default with application and Agent overrides, so that Todo policy can be configured at the correct ownership level.
8. As an application author, I want an Agent's Todo mode to remain independent from `planning_interval`, so that replanning cadence and task tracking do not accidentally control one another.
9. As a Supervisor, I want my Todo list to be isolated from every Worker, so that concurrent delegation cannot corrupt the top-level execution plan.
10. As a Worker, I want to create my own Todo for a complex delegated task, so that I can manage internal execution without writing into the Supervisor's list.
11. As a parent Agent, I want delegation represented as one item in my own Todo, so that child implementation details do not pollute the parent plan.
12. As an Agent, I want each Todo update to replace one complete ordered snapshot, so that there is exactly one authoritative list after a write.
13. As an Agent, I want an empty list to clear the current Todo state, so that obsolete task state can be removed explicitly.
14. As an Agent, I want exactly zero or one item to be `in_progress`, so that the current execution focus is unambiguous.
15. As an Agent, I want to mark a no-longer-needed item `cancelled` with a reason, so that replanning does not falsely report work as completed.
16. As an Agent, I want blocked or partially completed work to remain non-terminal, so that `cancelled` cannot hide an unresolved obligation.
17. As an AgentLoom user, I want Todo content and status to be strictly validated, so that malformed model output cannot silently become runtime state.
18. As an AgentLoom user, I want any invalid item to reject the entire update, so that a partial write cannot create a mixed or misleading list.
19. As an AgentLoom user, I want successful writes to return the complete committed snapshot and revision, so that the model observes the same state that was persisted.
20. As an AgentLoom user, I want Todo state to survive a crash or resume of the same task when checkpointing is enabled, so that the Agent can continue the plan it was executing.
21. As an AgentLoom user, I want Todo state to be scoped by application, task, and Agent path, so that unrelated runs and Agents never see one another's plans.
22. As an AgentLoom user, I want Todo state to remain in memory only when checkpointing is disabled, so that disabling checkpoint does not create a hidden persistence mechanism.
23. As a resumed Agent, I want the latest Todo snapshot injected before my next model call, so that I do not need to rediscover the plan from old tool messages.
24. As an Agent using context compression, I want the canonical Todo snapshot injected after compression, so that summarization cannot make me forget unfinished work.
25. As an AgentLoom operator, I want Todo persistence to use atomic writes and task-scoped locking, so that crashes and concurrent activity cannot create partial JSON.
26. As an AgentLoom operator, I want a corrupt Todo snapshot to fall back to an empty list without stopping the automated task, so that auxiliary planning state does not make the core run unavailable.
27. As an AgentLoom operator, I want corrupt Todo data preserved and a structured warning recorded, so that silent runtime recovery does not destroy diagnostic evidence.
28. As an AgentLoom user, I want final output to remain possible with unfinished Todo items, so that Todo is guidance rather than a hidden completion gate.
29. As an AgentLoom user, I want final output to avoid extra Todo-only model calls, so that completion latency and cost remain predictable.
30. As an AgentLoom operator, I want interrupted and failed tasks to retain Todo state with their checkpoint, so that resume remains possible.
31. As an AgentLoom operator, I want successful task cleanup to remove active Todo state according to the checkpoint cleanup policy, so that temporary execution state does not become long-term project data.
32. As an AgentLoom maintainer, I want completed tool calls to remain available through ordinary run observability, so that a separate Todo history system is unnecessary.
33. As an AgentLoom maintainer, I want one cross-model Todo contract, so that OpenAI, Claude, Gemini, and other providers do not receive contradictory behavior.
34. As an AgentLoom maintainer, I want the old three-stage Todo prompts removed, so that there is no second hidden planning protocol.
35. As an AgentLoom maintainer, I want `planning_interval` tests to remain valid after the refactor, so that removing Todo coupling does not remove periodic replanning.
36. As an AgentLoom maintainer, I want legacy Markdown Todo data deleted without migration or dual writes, so that the old mechanism cannot remain a competing source of truth.
37. As an AgentLoom maintainer, I want ordinary Hook observation to continue receiving the final tool input and outcome, so that the accepted Hook Runtime Contract remains intact.
38. As an AgentLoom maintainer, I want no Todo-specific UI or event transport in this refactor, so that the implementation stays focused on the Python automation runtime.

## Implementation Decisions

- Todo is an execution-planning capability, not a long-term task manager, document format, user-maintained checklist, or cross-task memory system.
- The implementation targets the AgentLoom Python runtime. The bundled OpenCode checkout and Hermes Agent checkout are reference implementations only and must not be modified.
- The canonical model-facing tool remains named `todo_write`. The name is selected because the operation is a complete write, not as a compatibility adapter for the deleted mechanism.
- Todo ownership is `(application_id, task_id, agent_path)`. A Supervisor and each Worker have separate lists. No Agent may modify another Agent's list.
- Configuration exposes `todo.mode` with `auto`, `on`, and `off`. The default is `auto`. Effective configuration follows the existing global system, application system, and Agent YAML layering rules; the most specific valid value wins.
- `off` is authoritative at final runtime tool construction. It removes `todo_write` even if a generic tools or toolset declaration would otherwise include it, and it suppresses Todo policy text and state injection.
- `auto` exposes the tool and a shared policy. The model decides whether a task has enough meaningful steps, deliverables, uncertainty, verification work, or recovery value to justify Todo. No host-side complexity classifier is added.
- `on` exposes the same tool and adds one provider-independent strong system instruction. For every non-trivial, multi-step execution task, the Agent must call `todo_write` after the task scope is understood and before mutating or otherwise substantive execution begins. Minimum necessary read-only discovery may precede the write. The runtime does not enforce a literal first-tool gate.
- Simple one-step work, pure answers, and casual conversation do not require Todo. `on` must not create ceremonial one-item lists when no execution plan is useful.
- No mode uses `toolChoice=required`, injected Todo-only ActionSteps, hidden retries, final gates, or additional LLM turns. Prompt strength is not represented as a host execution guarantee.
- The detailed Todo contract lives in one shared tool description. System prompts contain only the short mode-specific policy. Provider-specific Todo prompt variants are removed.
- Every `todo_write` call accepts the complete ordered list. A valid call atomically replaces the current Agent's entire list. Omitted items are removed. An empty list explicitly clears the state.
- A Todo item contains `content`, `status`, and optional `cancel_reason`. It has no stable item ID, priority, dependency graph, timestamps, active-form text, or item-level revision. List order is the execution order.
- `status` is a true enum: `pending`, `in_progress`, `completed`, or `cancelled`. At most one item may be `in_progress`.
- `cancel_reason` is required and non-empty only when status is `cancelled`. Non-cancelled items do not persist a cancellation reason. Cancellation means the Agent has determined the item is no longer necessary or has been superseded; it must not represent blocked, failed, partial, or unverified work.
- The service validates type, enum, trimmed non-empty content, item count, per-item content length, cancellation reason, and total serialized size. Limits are bounded implementation constants and must be documented in the tool schema. Validation is completed before mutation.
- A successful tool call returns the exact canonical full snapshot, status counts, and the new list-level `revision`. A failed validation or persistence operation returns a typed tool failure and leaves the prior snapshot unchanged.
- With checkpointing enabled, the canonical state is a versioned `todos.json` inside the task checkpoint directory. It contains task identity, a file-level schema version, and revisioned Agent-scoped snapshots.
- Todo shares checkpoint task creation, resume, lease, explicit deletion, success cleanup, and retention. Its data plane remains separate from the task tree and task event log so high-frequency full snapshots cannot enlarge or overwrite execution-tree metadata.
- Persistence uses the existing secure runtime storage primitives, an independent stable Todo lock, strict schema reads, and atomic temp-write/fsync/rename semantics. Locking a replaceable data-file inode is not sufficient; the lock identity must remain stable across atomic replacement.
- With checkpointing disabled, the same Todo service uses an Agent/run-scoped in-memory backend. It must not create checkpoint directories or durable Todo artifacts, and a new run starts empty.
- A malformed or unreadable Todo file does not block the automated task. The runtime preserves or quarantines the corrupt artifact, emits a structured warning with task and Agent scope, exposes an internal corrupt-state diagnostic, and supplies an empty list for continued execution. It must not overwrite the corrupt evidence with an empty snapshot as part of recovery.
- Model context is hydrated from the canonical store on every LLM step after conversation compression. A bounded trusted system reminder carries the current revision and snapshot only when Todo is enabled and non-empty.
- The injected reminder exists only in the current model input. It does not create a MemoryStep, re-enter compression history, or cause an extra provider call. A model-visible `todo_read` tool is not added.
- Ordinary completed `todo_write` calls remain visible through existing tool call/result observability. No new Todo-specific UI transport, SSE domain, frontend store, or panel is part of this refactor.
- Following OpenCode's execution-loop behavior, pending Todo items do not prevent a final answer. The host does not auto-complete, auto-cancel, retry, or add a final Todo reconciliation turn.
- Interrupted, failed, and crashed tasks retain checkpoint-backed Todo state. Successful cleanup removes it with the task checkpoint after terminal run evidence is committed. If checkpoint cleanup on success is disabled, the terminal snapshot may remain as inactive checkpoint state but is not long-term project management data.
- The accepted Hook Runtime Contract remains unchanged. Core Todo validation and persistence are not implemented as configurable Hooks. Existing Tool Hooks remain observers of the effective input, side effect, and typed outcome according to the ADR's fail-open/fail-closed rules.
- The entire legacy Todo synchronization mechanism is removed: the mixin, three default/specialized Todo prompts, PlanningStep injection, retry loop, history manipulation, final review, Markdown parsing/injection, process-global Todo list, and Markdown persistence.
- The prompt variants remove `planning.todo_initial`, `planning.todo_update`, and `planning.todo_final`, along with text claiming that the system will later force registration. They retain one concise mode-aware Task Tracking policy.
- `planning_interval` remains a validated smolagents replanning cadence and passes through unchanged. It no longer injects, enables, updates, or validates Todo.
- Generic `tools`, `toolsets`, and `prompt` configuration remain available for their non-Todo purposes. Todo mode is the single authoritative capability policy.
- Existing legacy Todo Markdown artifacts are deleted without import, archival migration, compatibility reads, or dual writes. Deletion targets only verified Todo files and obsolete Todo-specific code; unrelated task workspace data must remain untouched.
- Documentation, framework-skill references, configuration validation, templates, examples, and generated-facing contracts are updated in the same change so runtime behavior and documentation cannot diverge.

## Testing Decisions

- Tests assert externally observable contracts rather than private implementation details. They must not depend on a module-global list, exact internal helper names, private mixin structure, or Markdown formatting.
- The highest behavioral seam is an Agent runtime integration test with a deterministic fake model. This seam verifies effective mode, model-visible tools and policy, absence of forced extra calls, normal PlanningStep behavior, context hydration, and unblocked final output.
- A public Todo service/tool seam verifies the complete replace contract: initial write, ordered replacement, empty clear, canonical response, revision increment, status counts, and prior-state preservation after failure.
- Store contract tests verify application/task/Agent isolation, Supervisor/Worker separation, atomic writes, stable locking, concurrent replacement behavior, size limits, strict schema decoding, and absence of partial files.
- Configuration tests cover default `auto`, valid global/application/Agent inheritance, Agent override, `on`, authoritative `off`, invalid modes, and conflicts with generic explicit tool declarations.
- Prompt tests cover all supported Agent/model prompt variants through the common prompt-building seam. `auto` receives one autonomous policy, `on` receives one strong policy, and `off` receives no Todo policy. No variant may retain three-stage Todo keys or provider-specific contradictory rules.
- Runtime tests prove that `planning_interval` neither enables nor disables Todo and still reaches smolagents unchanged. A PlanningStep must not cause a second Todo-only ActionStep or extra model invocation.
- Schema tests cover the four valid statuses, unknown statuses, zero or multiple `in_progress` items, required `cancel_reason`, forbidden irrelevant cancellation reasons, empty/whitespace content, malformed list shapes, item-count limits, field-length limits, and total-size limits.
- Checkpoint integration tests use the existing Runner/checkpoint seam. They verify persistence and resume for Supervisor and Worker lists, exact round-trip of content/status/reason/order/revision, isolation when one Agent updates, and restore before the first resumed model action.
- Context tests verify that an authoritative snapshot remains present in final model messages after ordinary history restoration, observation masking, smart summary, or fallback truncation. Empty lists and `off` mode must inject nothing.
- Checkpoint-disabled tests verify in-run memory behavior, no durable Todo artifact, empty state in a new run, and unchanged rejection of resume when checkpointing is unavailable.
- Lifecycle tests verify that interrupted, failed, and crashed runs preserve Todo; successful manifest finalization precedes checkpoint/Todo cleanup; finalization failure preserves resumable state; and cleanup-disabled runs retain inactive terminal state without leaking active in-memory stores.
- Corruption tests verify strict read validation, continued execution with an empty runtime state, structured warning emission, preservation/quarantine of the corrupt file, and prevention of automatic empty overwrite.
- Hook integration tests verify that successful and failed Todo calls continue through the existing sequential Tool Hook boundary with effective input and typed outcome. No Todo validation rule is delegated to a Hook.
- Regression tests assert that no Todo Markdown file is created, an empty list is accepted, persistence failures cannot report success, and task/Agent states cannot leak through process-global memory.
- Legacy tests dedicated to forced synchronization, Todo-only retries, final review, Markdown readback, and prompt patching are removed or rewritten at the higher behavioral seams above.
- General tool-call argument coercion tests formerly co-located with Todo injection tests are retained and moved to the generic ToolCallingAgent contract suite.
- Existing `planning_interval` validation and passthrough tests remain in their current general runtime suites rather than being duplicated under Todo tests.
- One lightweight source/contract guard may assert that the retired mixin and three retired prompt keys cannot be reintroduced, but primary confidence must come from behavior tests.
- No Todo-specific frontend, TUI, SSE, or visual snapshot tests are added because UI delivery is outside this spec.

## Out of Scope

- A long-term personal or project task manager.
- User-authored Todo editing, approvals, interactive cancellation, or collaborative lists.
- Cross-task Todo memory, task templates, recurring tasks, due dates, ownership assignment, dependencies, notes, attachments, or nested subtasks.
- Priority-based scheduling; list order is sufficient for this execution-state model.
- Shared mutable Todo lists across Supervisor and Workers.
- A Todo-specific UI panel, TUI bridge protocol, API hydration endpoint, SSE event stream, or frontend cache.
- Modifications to the bundled OpenCode or Hermes Agent reference repositories.
- Provider-specific Todo prompts without evaluation evidence that a shared contract is insufficient.
- Host-side task-complexity classification, keyword triggers, forced tool choice, first-tool gates, Todo-only retries, or additional planning turns.
- Blocking final answers until all Todo items become terminal.
- A model-visible `todo_read` tool.
- Migration, parsing, import, archive conversion, or dual writing of legacy Markdown Todo data.
- Replacing AgentLoom checkpoint storage with a new central SQLite database solely for Todo.
- Expanding the accepted Hook Runtime Contract or using Hooks as Todo's source of truth.

## Further Notes

- OpenCode is the primary behavioral reference: Todo is a normal model tool, updates are full-snapshot replacements, pending items do not gate final output, and the host does not force extra Todo turns. AgentLoom deliberately improves its weak status validation and context-recovery semantics.
- Hermes Agent is a secondary reference for a single cross-model tool schema, lightweight coding posture, four-state semantics, Agent-local ownership, and avoiding a host-side complexity classifier.
- Neither reference reliably injects canonical Todo state back into the model after arbitrary compaction. AgentLoom's per-step trusted snapshot projection is an intentional reliability improvement.
- The structured file remains local because AgentLoom checkpointing is local. Coordination comes from sharing checkpoint identity, storage primitives, locks, resume, and cleanup—not from choosing a database merely because another framework uses one.
- `cancelled` is an autonomous Agent state. It means a planned item has become unnecessary or has been superseded; it does not require user interaction.
- Runtime recovery from corrupt Todo state is silent only from the Agent's execution perspective. Operators still receive durable diagnostics, and corrupt evidence is retained.
- The repository's accepted Hook Runtime ADR is preserved: Todo core validation remains fixed runtime behavior, while Hooks observe outcomes through the existing sequential contract.
- This specification is intentionally breaking. The old implementation and legacy Todo data are removed instead of carrying a compatibility layer that would preserve two competing mechanisms.
