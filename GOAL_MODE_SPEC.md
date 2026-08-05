# AgentLoom Goal Mode Specification

Status: Implemented and validated (2026-08-04)

## Problem Statement

AgentLoom 当前通过 Agent YAML 的 workflow 定义 Supervisor 的长期职责和执行流程。普通字符串 workflow 会作为一次任务上下文执行；Supervisor 的 workflow 列表则具有特殊执行语义，每个列表项都会触发一次独立的运行，后续项通过保留 memory 延续前序结果。

这种机制适合预先确定的阶段式流程，但无法表达另一类自动化任务：用户只关心一个最终目标，希望主 Agent 在单次回答或单轮执行结束后仍能持续推进，直到它明确确认目标已经完成，或者累计 token 预算达到上限。现有 final answer、Todo、max_steps 和 workflow 列表都不能提供这种生命周期保证：

- final answer 只代表一次模型运行停止，不代表最终目标已经满足；
- Todo 是执行进度状态，不是目标完成门槛；
- max_steps 是单次运行的安全边界，不是整个目标的生命周期边界；
- workflow 列表是预定义的多次运行，不会在列表结束后依据目标状态自动续跑；
- Worker 与 Supervisor 的 token 消耗没有统一汇总，预算容易被委派绕过；
- 当前运行状态没有 budget_limited，无法准确表达“目标未完成，但预算已经阻止继续运行”；
- Goal 配置如果开放给 Worker，会产生多个互相竞争的目标事实源。

用户需要一个由 Supervisor 独占的 Goal Mode。它应通过现有 Agent YAML 显式启用，将 description、workflow 和本次 task 组合为一个目标上下文，在同一个 Agent 和记忆上持续 continuation，并由主 Agent 通过明确的 Goal 工具提交完成证据。Goal Mode 必须兼容现有 workflow 字段，但启用后应采用 Codex 风格的目标驱动执行，而不是继续把 workflow 列表解释为多个独立运行。

## Solution

为 AgentLoom 增加一个与现有 Agent 角色和 workflow 配置正交的 Goal Mode。

Goal Mode 仅允许顶层 Supervisor 配置。配置支持布尔简写，也支持包含 enabled 和可选 token_budget 的完整 mapping。布尔 true 表示启用且预算无上限；布尔 false 或缺少 goal 表示关闭；mapping 中 enabled 必填，token_budget 省略时同样表示无上限。

启用后，系统根据 description、完整 workflow 和本次运行 task 自动创建唯一的 active goal。用户不需要在 YAML 中重复填写 objective。字符串 workflow 直接作为目标要求；列表 workflow 按原顺序编号并合并为一个目标上下文，只触发一次初始运行。文档推荐 Goal Mode 使用多行字符串 workflow，同时明确列表在 Goal Mode 与普通模式下具有不同语义。

主 Agent 获得 get_goal 和 update_goal 两个内部生命周期工具。get_goal 返回目标、状态、预算、累计用量和完成证据。update_goal 只接受 complete 状态和非空 evidence。普通 final answer 不会自动完成 goal；只要 goal 仍为 active，运行时就在同一 Agent 和记忆上发送 continuation prompt。max_steps 仅结束当前 continuation 分段，不终止 active goal。

Goal 工具既从 Worker 的工具目录中隐藏，也在执行时验证调用者确实是根 Supervisor。Worker 可以执行主 Agent 委派的工作，但不能读取或修改根 Goal。所有 Worker 的模型 token 仍计入根 Goal 的累计预算。

token_budget 统计整个根 Agent 树的 prompt_tokens 与 completion_tokens。限制是软边界：已经发出的模型请求不会被取消，并发 Worker 可能造成少量超额；响应完成后系统原子累计用量，达到预算时将 Goal 和 run 置为 budget_limited，保存 checkpoint，停止新的模型请求与 continuation。用户提高 YAML 预算或移除预算后可以 resume，既有用量不会清零。

Goal 状态和用量随 task checkpoint 持久化。checkpoint 记录 Goal 是否已经启动，resume 时恢复现有 memory 并发送 continuation prompt，不重新注入或重新执行初始 workflow。目标内容通过稳定指纹绑定；workflow、description 或 task 发生变化时拒绝恢复。手动中断和真实运行错误保留 active goal；完成状态一旦由 update_goal 持久化便不回滚。若完成提交后、最终回复落盘前发生崩溃，系统与 Codex 一致，不自动补写最终回复。

成功完成前，Goal 的 objective、终态、token 用量、预算和 evidence 会复制到持久化 run manifest，再按现有成功清理策略清除 checkpoint。CLI、JSON/JSONL 和 TUI 运行详情均展示 Goal 状态与用量。未启用 Goal 时，不注入工具、提示或计量逻辑，不改变现有 workflow 行为。

## User Stories

1. As an AgentLoom user, I want to enable Goal Mode with a boolean YAML value, so that a simple goal does not require verbose configuration.
2. As an AgentLoom user, I want to disable Goal Mode explicitly, so that I can preserve ordinary workflow behavior.
3. As an application author, I want a full Goal mapping with an explicit enabled field, so that configuration remains predictable and avoids implicit mapping semantics.
4. As an application author, I want token_budget to be optional, so that omitting it intentionally means unlimited execution.
5. As an application author, I want token_budget to accept only positive integers, so that invalid or ambiguous limits fail before execution.
6. As an application author, I want unknown Goal configuration fields to fail validation, so that typos cannot silently change runtime guarantees.
7. As a Supervisor author, I want Goal Mode to derive its objective from description, workflow, and the current task, so that I do not duplicate the same objective in YAML.
8. As a Supervisor author, I want a multiline string workflow to be the recommended Goal format, so that one coherent objective is easy to read and maintain.
9. As a Supervisor author, I want a workflow list to remain accepted in Goal Mode, so that existing content can be adopted without a schema rewrite.
10. As a Supervisor author, I want Goal Mode to merge workflow list items in their original order with visible numbering, so that the model retains every requirement and its boundary.
11. As an existing AgentLoom user, I want ordinary non-Goal workflow lists to retain their current sequential run behavior, so that enabling this feature elsewhere does not change my workflows.
12. As an AgentLoom user, I want one active Goal per root run, so that completion, budget, and recovery have one authoritative lifecycle.
13. As an AgentLoom user, I want a normal final answer to leave an unfinished Goal active, so that one premature model stop cannot silently terminate the objective.
14. As an AgentLoom user, I want an active Goal to continue automatically on the same Agent and memory, so that long-running work can progress across multiple model segments.
15. As an AgentLoom user, I want continuation prompts to omit the initial workflow payload, so that resume and continued execution do not duplicate the task.
16. As an AgentLoom user, I want max_steps to delimit one continuation segment rather than fail the whole Goal, so that the existing safety cap remains useful for long-running objectives.
17. As a main Agent, I want to inspect the active Goal through get_goal, so that I can reason from the canonical status and remaining budget.
18. As a main Agent, I want to complete a Goal through update_goal with evidence, so that completion is explicit and auditable.
19. As an AgentLoom user, I want completion evidence to be non-empty, so that a terminal Goal records why the Agent considered it finished.
20. As an AgentLoom user, I want the root Agent's completion assertion to be accepted without another evaluator model call, so that completion remains deterministic and cost-efficient.
21. As a Worker, I want Goal lifecycle tools to be unavailable, so that I cannot accidentally mutate the Supervisor's objective.
22. As an AgentLoom operator, I want Goal writes to validate root identity at execution time, so that hiding a tool alone is not the security boundary.
23. As a Supervisor, I want to delegate work normally while Goal Mode is active, so that Goal ownership does not prevent multi-Agent execution.
24. As an AgentLoom user, I want Worker model usage included in the root Goal budget, so that delegation cannot bypass the configured limit.
25. As an AgentLoom user, I want token usage to count provider-reported prompt and completion tokens, so that accounting uses the categories AgentLoom can observe reliably.
26. As an AgentLoom user, I want an unlimited Goal when token_budget is absent, so that budget policy remains opt-in.
27. As an AgentLoom user, I want a configured budget to apply cumulatively across initial execution, continuations, Workers, failures, and resumes, so that resume cannot reset cost.
28. As an AgentLoom user, I want in-flight requests to finish after a soft budget crossing, so that the runtime does not corrupt tool calls or partially cancel model responses.
29. As an AgentLoom operator, I want new model requests blocked after the shared budget is exhausted, so that overshoot remains bounded to already-started work.
30. As an AgentLoom user, I want budget exhaustion represented as budget_limited rather than completed, failed, or interrupted, so that the run outcome is truthful.
31. As an AgentLoom user, I want budget_limited state to preserve the checkpoint, so that I can continue after changing the budget.
32. As an AgentLoom user, I want to resume by increasing token_budget, so that unfinished work can continue without losing prior progress.
33. As an AgentLoom user, I want to resume a budget-limited Goal by removing token_budget, so that I can intentionally switch the remaining Goal to unlimited execution.
34. As an AgentLoom user, I want previously consumed tokens retained after a budget change, so that accounting remains a lifetime total.
35. As an AgentLoom user, I want a reduced or unchanged exhausted budget to remain budget_limited, so that resume cannot pretend capacity exists.
36. As an AgentLoom user, I want manual interruption to leave the Goal active, so that an intentional stop does not abandon the objective.
37. As an AgentLoom user, I want tool, model, or checkpoint failures to mark the run failed while preserving the active Goal, so that I can fix the cause and resume.
38. As an AgentLoom user, I want resume to reject changed objective content, so that old progress cannot be attached to a different workflow or task.
39. As an AgentLoom user, I want resume to reject an active Goal whose YAML has been disabled, so that configuration cannot silently cancel persisted state.
40. As an AgentLoom user, I want Goal-started state to survive process restart, so that resume sends a continuation rather than the original task again.
41. As an AgentLoom user, I want a completed Goal to remain complete after interruption or process failure, so that committed completion is never rolled back.
42. As an AgentLoom user, I want a hard crash after completion but before final delivery not to restart substantive work, so that a terminal Goal remains terminal.
43. As an AgentLoom operator, I want completed Goal evidence and usage copied into the run manifest, so that successful checkpoint cleanup does not erase audit information.
44. As a CLI user, I want to see Goal status, used tokens, and configured budget, so that I can understand why a run continued or stopped.
45. As a machine client, I want JSON and JSONL output to contain structured Goal state, so that automation does not parse human text.
46. As a TUI user, I want run details to show active, complete, and budget_limited states, so that Goal lifecycle is visible during and after execution.
47. As a scheduler user, I want scheduled workflows to support Goal Mode, so that unattended objectives can use the same lifecycle.
48. As a scheduler user, I want documentation to warn about unlimited unattended Goals, so that I can configure a budget when a human will not monitor execution.
49. As a Worker YAML author, I want Goal configuration to fail validation, so that Supervisor-only ownership is enforced before runtime.
50. As an AgentLoom user, I want Goal-disabled workflows to have no extra prompts, tools, token accounting, persistence, or status changes, so that the feature is zero-impact when unused.
51. As an AgentLoom maintainer, I want runtime, offline validation, builder validation, and documentation to share one Goal contract, so that configuration behavior cannot diverge by entry point.
52. As an AgentLoom maintainer, I want Codex used as the lifecycle reference and OpenCode used as the permission reference, so that the implementation borrows proven boundaries without copying incompatible architecture.
53. As an AgentLoom maintainer, I want the bundled reference repositories left untouched, so that AgentLoom owns its own Goal implementation.
54. As an AgentLoom maintainer, I want ordinary workflow-list resume behavior left outside this change, so that Goal Mode does not expand into an unrelated execution-cursor refactor.
55. As a documentation reader, I want the different list semantics in Goal and non-Goal modes stated explicitly, so that YAML shape alone does not create surprising execution.

## Implementation Decisions

- Goal Mode is a Supervisor-owned execution-lifecycle overlay, not a third Agent role, a Todo variant, a collaboration mode, or a new workflow field type.
- The implementation targets the AgentLoom Python runtime and its TUI/CLI surfaces. The bundled Codex checkout and the externally inspected OpenCode repository are reference implementations only and must not be modified.
- Accepted Goal configuration has exactly three forms: boolean true, boolean false, or a mapping with required enabled and optional token_budget. A missing goal field is equivalent to false.
- In mapping form, enabled must be a boolean and token_budget must be a positive integer when present. Unknown keys, null, strings, zero, negative values, floats, and booleans used as integers are rejected.
- Goal configuration is legal only on a top-level Supervisor definition. Runtime Worker construction, offline application validation, builder validation, and Worker-as-tool loading all reject Goal configuration fail-closed.
- Boolean true enables an unlimited Goal. Boolean false disables Goal Mode. A mapping with enabled true and no token_budget is unlimited. A mapping with enabled false may not include token_budget.
- Goal-disabled execution remains byte-for-byte compatible at the behavior boundary: ordinary string workflow behavior and sequential Supervisor list behavior are unchanged.
- The canonical Goal objective is a deterministic composition of the normalized Supervisor description, normalized workflow content, and the original task request.
- In Goal Mode, a string workflow remains one coherent block. A workflow list is normalized in original order into numbered Goal requirements and then handled as one initial runtime task. It never creates multiple runtime runs.
- Documentation recommends a multiline string workflow for Goal Mode and treats list support as compatibility input, not as a staged executor.
- Goal state is scoped to one root task. Only one Goal exists for that task, and a second Goal cannot be created through model tools.
- YAML activation creates the Goal automatically. There is no create_goal tool.
- The model-facing lifecycle surface contains get_goal and update_goal. update_goal accepts only status complete and a required non-empty evidence string. There are no blocked, cancelled, paused, create, clear, or arbitrary budget-mutation tools.
- get_goal returns the canonical objective, status, configured budget, cumulative used tokens, remaining budget when bounded, completion evidence when terminal, and stable Goal identity.
- The Goal status state machine contains active, budget_limited, and complete. budget_limited transitions back to active only after a valid budget increase or removal during resume. complete is terminal.
- Run status remains distinct from Goal status. Goal-aware runs can be running, completed, budget_limited, interrupted, failed, or crashed according to the existing run lifecycle plus the new resumable budget_limited outcome.
- A normal model final answer does not change Goal status. When a runtime segment returns while Goal is active, the host starts another continuation segment on the same runtime Agent with reset disabled and preserved memory.
- The first segment receives the complete Goal context. Later in-process continuation and resumed execution receive a bounded Codex-style continuation prompt containing stable Goal identity, current status, used budget, remaining budget, and instructions to continue or explicitly complete. It relies on restored conversation memory and does not reinject the workflow; the root Agent can call get_goal to reread the canonical objective when needed.
- A persisted goal_started marker distinguishes initial execution from continuation. Resume never replays the initial Goal prompt after this marker has committed.
- max_steps remains a per-segment safety cap. A max-steps result while Goal is active and budget remains is converted into the next continuation segment rather than a terminal run failure.
- True model-provider failures, tool failures that abort the run, checkpoint corruption, and unrecoverable runtime errors stop the current attempt. They preserve an active Goal and resumable checkpoint under the existing failed, interrupted, or crashed semantics.
- Root-only Goal tools are enforced twice: they are appended only to the Supervisor tool surface when Goal Mode is enabled, and their handlers validate root local-run identity, root-run identity, top-level HookRun ownership, and active Goal configuration before reading or writing state.
- Worker prompts contain no Goal lifecycle tool instructions. The Supervisor passes only the subtask context needed for delegation.
- Goal token accounting is owned by shared root-run state inherited by Supervisor and Workers, including parallel Worker execution.
- The charge unit is provider-reported prompt_tokens plus completion_tokens for every model response in the root Agent tree. AgentLoom does not invent separate cache or reasoning accounting when the provider abstraction cannot expose it consistently.
- Accounting occurs once at the model-response boundary, not by summing cumulative RunResult totals. This avoids double-counting memory-preserving workflow and continuation runs.
- A bounded Goal performs a pre-request check against already committed usage. Exact future token cost is unknown, so an allowed request may cross the budget. Concurrent in-flight calls may increase the overshoot.
- The runtime never cancels an already-started provider response solely because the Goal budget crossed. After each response, usage is atomically added to the root total. Once used tokens meet or exceed the budget, no new provider request or continuation may start.
- A budget crossing transitions both Goal and current run to budget_limited, persists all recoverable state, emits structured status, and preserves the checkpoint. It does not report complete or failed.
- token_budget is a lifetime cumulative limit. Resume does not reset used tokens. A budget-limited Goal resumes only when the new configuration raises the budget above used tokens or removes the limit. Budget decreases are rejected for an existing Goal.
- Resume validates an objective fingerprint derived from description, normalized workflow, and original task. Any objective change is rejected. Budget is validated separately so a permitted increase does not invalidate the objective fingerprint.
- Changing an active persisted Goal to disabled causes resume to fail with an actionable configuration error. Starting a new task with Goal disabled remains valid.
- Goal state uses a task-scoped, versioned, atomically persisted checkpoint record. It contains stable Goal identity, schema version, objective and fingerprint, status, goal_started, token budget, used tokens, completion evidence, timestamps, and the configuration facts needed for safe resume.
- Checkpoint-disabled execution may keep Goal state in root-run memory for the current process, but crash resume is unavailable under the existing checkpoint contract.
- update_goal completion is idempotent. Once complete has committed, later abort or failure processing cannot revert it to active.
- A tool-calling root Agent may claim one in-process completion-settlement model request after update_goal commits, provided the completing response did not exhaust the token budget. The request is bound to the same root local-run identity, exposes only final_answer when a tool schema is present, and is not persisted; scheduled planning is skipped locally, pending smart summary uses deterministic local truncation instead of a model request, a max-steps prose fallback may consume the same allowance, and a crash or resume cannot recreate it.
- Completion state and model transcript settlement are not a cross-system transaction. Following Codex semantics, a crash after completion commits but before the final assistant response persists may leave a complete Goal without a final delivery. Resume does not regenerate that response or restart substantive work; get_goal and durable audit evidence remain authoritative.
- Before successful checkpoint cleanup, the runtime copies a compact Goal summary into the run manifest or its linked audit evidence. The summary includes objective, Goal identity, final status, configured budget, used tokens, completion evidence, and relevant timestamps.
- budget_limited, interrupted, failed, and crashed attempts retain the Goal checkpoint. Existing retention and explicit task-cleanup behavior remain authoritative for abandoned runs; no cancel_goal tool is introduced.
- CLI human output displays Goal status and token usage at lifecycle boundaries. JSON and JSONL use a stable structured Goal object. TUI run details display the same canonical state rather than reconstructing it from log text.
- Scheduled runs accept the same Goal YAML. Unlimited scheduled Goals remain legal; documentation gives a strong operational warning to set token_budget for unattended execution.
- Ordinary non-Goal workflow lists retain current sequential execution and current resume behavior. A generic execution cursor is explicitly excluded from this feature.
- Agent configuration documentation, checkpoint documentation, run observability documentation, framework-skill YAML contract, examples, and validation guidance are updated together.

## Testing Decisions

- Good tests assert behavior visible at a public boundary: accepted YAML, model-visible prompt and tools, provider-call count, continuation, Goal status, token usage, run outcome, persisted resume behavior, and CLI/TUI/JSON output. They do not assert private helper names or internal object layout.
- The primary and highest test seam is a full Supervisor runtime invocation through the same runner used by loom run, backed by a deterministic fake model and fake Worker models. Most feature behavior should be proven at this seam.
- The primary runtime suite covers boolean and mapping activation, unlimited and bounded execution, single-string workflow context, list merging into one numbered context, ordinary non-Goal list regression, final-without-complete continuation, explicit completion, evidence, and completion termination.
- The same runtime seam verifies that get_goal and update_goal are visible to the root Supervisor, absent from Workers, and rejected when called under a forged or inherited non-root execution context.
- Multi-Agent runtime tests verify that Supervisor and parallel Worker prompt/completion tokens accumulate exactly once into the root Goal and that delegated calls cannot bypass budget.
- Budget integration tests verify exact-boundary usage, one-response soft overshoot, parallel overshoot, no new calls after limit, budget_limited output, preserved checkpoint, increased-budget resume, unlimited resume, unchanged exhausted budget, and retained cumulative usage.
- Continuation tests verify same-runtime memory preservation, no repeated initial workflow payload, bounded continuation prompt content, continuation after max_steps, and termination only after complete or budget_limited.
- Resume tests verify goal_started recovery, stable objective fingerprint acceptance, changed workflow/description/task rejection, enabled-to-disabled rejection, manual interruption recovery, failed-attempt recovery, and no token reset.
- Completion crash tests cover complete committed before final assistant persistence. They assert that Goal remains complete, no new substantive continuation starts, completion is not replayed, and durable evidence remains readable.
- Configuration contract tests exercise the public runtime validator and the offline application validator with the same table of valid and invalid values. Valid cases are true, false, enabled true without a budget, enabled true with a positive budget, and enabled false without a budget.
- Invalid configuration tests cover mapping without enabled, enabled false with a budget, unknown keys, null, strings, empty mappings, zero, negative, float, and boolean budget values.
- Role validation tests prove Supervisor acceptance and Worker rejection through both direct runtime loading and Worker-as-tool/application validation paths.
- A focused Goal state-store contract test covers atomic replacement, schema versioning, idempotent completion, status transitions, concurrent accounting, corrupt state handling under the existing checkpoint policy, and isolation by application/task.
- A focused model-accounting test verifies charging at each response boundary rather than cumulative RunResult totals, including planning responses when provider usage is available and nested Worker responses propagated through root-run context.
- Run lifecycle tests verify that complete is eligible for existing success cleanup only after manifest/audit persistence, while budget_limited, interrupted, failed, and crashed preserve resumable state.
- Output-contract tests verify that CLI text, JSON, JSONL, and TUI run details agree on canonical status, budget, used tokens, remaining tokens, and evidence visibility.
- Schedule integration tests verify that Goal-enabled YAML is accepted by the scheduler and that the launched run uses the same lifecycle and budget semantics as a direct run.
- Documentation examples are validated through the existing application YAML validation seam so published boolean, mapping, string-workflow, and list-workflow examples cannot drift from runtime parsing.
- Prior art should be reused from existing suites for Supervisor task transformation, sequential workflow execution, deterministic runtime construction, checkpoint resume, runner status/cleanup, runtime summary, TUI bridge contracts, schedule RPC, YAML definition validation, Worker tool isolation, and token monitoring.
- The bundled Codex and external OpenCode sources are not test dependencies. AgentLoom tests assert the decisions in this specification rather than pinning implementation details from reference repositories.

## Out of Scope

- Modifying, extending, or patching the bundled Codex checkout.
- Vendoring or modifying OpenCode.
- Multiple simultaneous Goals for one root task.
- Worker-owned, child-thread, shared-mutable, or nested Goals.
- create_goal, cancel_goal, blocked, paused, usage_limited, or arbitrary status mutation tools.
- Automatic completion evaluation by a second model, judge, rubric engine, or host-side heuristic.
- Treating Todo completion as Goal completion.
- Treating a normal final answer as Goal completion.
- A hard pre-response token limit with exact cancellation at the configured number.
- Separate billing rules for cached, reasoning, cache-write, monetary cost, wall-clock time, or provider quotas.
- Resetting token usage on resume.
- Replaying or regenerating a missing final assistant response after complete has already committed.
- Exactly-once delivery guarantees across Goal state, model transcript persistence, CLI delivery, and external side effects.
- A generic workflow-stage execution cursor or changes to ordinary workflow-list resume.
- Preserving ordinary list-as-multiple-runs semantics inside Goal Mode.
- Goal configuration in global system configuration, Worker YAML, runtime model tools, or user-created dynamic Goal instances.
- Migration from an older AgentLoom Goal schema because no prior Goal feature exists.
- Distributed multi-host Goal ownership, leases, or consensus.
- A dedicated long-term Goal database independent from the task checkpoint and run audit lifecycle.
- A new interactive approval, cancellation, or budget-editing UI.

## Further Notes

- Codex is the primary lifecycle reference. Its Goal is orthogonal to normal collaboration mode, persists status independently, uses explicit get/create/update tools, continues only active Goals, applies soft token limits, and does not roll back a committed complete state when final delivery is missing.
- AgentLoom intentionally differs from Codex by auto-creating the Goal from Supervisor YAML, omitting create and blocked operations, and charging Worker usage to the root Goal.
- OpenCode is the primary permission-boundary reference. Its child sessions receive restricted capabilities, state-changing tools perform execution-time permission checks, and persisted tool side effects are not blindly replayed after interruption.
- Neither Codex nor OpenCode has AgentLoom's sequential YAML workflow-list execution model. Goal Mode therefore changes list interpretation to one merged context rather than introducing a workflow-stage cursor.
- Goal Mode and Todo solve different problems. Todo may help the Agent plan work inside a Goal, but Todo state is not authoritative for Goal completion or continuation.
- Unlimited means exactly no Goal token ceiling. Existing provider, context, infrastructure, user interruption, and retention limits still apply.
- Soft-budget overshoot is unavoidable because response token cost is only known after a provider response and parallel Workers may already be in flight. The UI and manifest report actual used tokens, including any overshoot.
- The main implementation risk is lifecycle coordination across model accounting, checkpoint persistence, run finalization, and output surfaces. The implementation should keep one canonical Goal state provider and project that state outward rather than creating separate truths in CLI, TUI, JSON, and manifest code.
- This specification is stored locally at the project root at the user's request. No external issue or tracker item is created.
