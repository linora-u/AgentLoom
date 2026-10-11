# Spec: Worker 有限批处理与单次调用并发合同

规格日期：2026-10-11。研究基线：`bc5cff6e`。

状态：规格已确定，实施与验收待执行。用户已确认单次 `map()` 容量作用域，以及以
Application Run 中的 `BoundWorker.map()` 为主要测试 seam，补充 Pi/smolagents 的真实
Application runner/CLI 验证。本规格交付首版有限批处理；后续共享容量和持久消费者另行立项。

## Problem Statement

Application 开发者需要把业务已规划好的多份输入交给同一种 Worker，并用一个明确的数字
控制同时执行量。当前框架主要提供单次 Agent 调用和 Agent-as-Tool；批处理依赖动态挂在
callable 上的隐藏方法，或者由 Application 自己维护线程池、Future 和资源清理。

现有批处理会修改调用者输入，按完成顺序返回结果，默认在配置之外隐藏最多 20 个在途任务，
并可能因连续失败跳过后续工作。并发数还被用于判断 Runtime 是否支持并行 Tool calls，
使完整 Worker 数量与一次模型响应里的工具执行语义混在一起。

开发者无法稳定解释“配置 32 为什么只有 20”“临时改成 4 是否影响下一次调用”“停止返回后
是否还有 Worker 写入”“哪些结果属于哪份输入”。随着多个批次同时执行，配置记录和资源
归属也必须分清，否则可能丢失运行证据或误关其他任务的资源。

用户希望业务代码只负责任务规划和结果汇总，中间的有限并发执行由 AgentLoom 统一完成，
无需为了接入框架而新增 Supervisor Agent、通用任务队列或第二套 Run 生命周期。

## Solution

提供绑定到 Application 执行作用域的 typed Worker Interface。开发者通过
`app.worker(path).map(items)` 提交有限任务；需要临时调整时提供本次 `concurrency` 参数。
框架负责配置解析、输入快照、有界 admission、fresh Agent/Runtime、Context 传播、结构化
结果、停止和清理，最终按输入顺序返回完整批次结果。

并发上限限定单次 `map()` 直接执行的顶层 Worker invocation。两个独立批次各配置 20，
可以合计执行 40 个；Agent-as-Tool 和 Runtime 内部 Tool calls 保留各自现有行为。

首版支持项目/Application 系统默认值、逐 Worker 默认值和代码覆盖，并记录来源链。
Application 作用域退出时停止新 admission，等待已登记工作清理，再由既有生命周期关闭
根资源。内存批处理不自动重放完整 Worker，也不声称具备崩溃恢复或强制线程中断能力。

## User Stories

1. As an Application author, I want to submit a finite WorkPlan through one typed Worker call, so that I can use framework concurrency without writing a thread pool.
2. As an Application author, I want planning and reduction to remain business code, so that execution capacity does not dictate task content.
3. As an Application author, I want a Worker identified by its Application-relative definition path, so that execution policy binds to a real Worker rather than an implicit stage name.
4. As an Application author, I want batch execution to work without a Supervisor Agent, so that Python and CLI orchestration do not require a new agent layer.
5. As an Application author, I want project-wide Worker defaults, so that common execution policy is configured once.
6. As an Application author, I want Application defaults to override project defaults, so that each Application can choose its own capacity.
7. As an Application author, I want per-Worker defaults to override Application defaults, so that Workers with different resource needs can use different capacities.
8. As an Application author, I want an explicit call parameter to replace lower-priority concurrency values, so that I can adjust one batch predictably.
9. As an Application author, I want omitted or None concurrency to use the binding default, so that ordinary calls need no configuration boilerplate.
10. As an Application author, I want a temporary override to leave the binding and configuration files unchanged, so that later calls retain their defaults.
11. As an Application author, I want unconfigured Workers to use a documented default of one, so that finite execution remains available without a complete policy table.
12. As an Application author, I want invalid values rejected even when another layer overrides them, so that configuration mistakes do not disappear silently.
13. As an Application author, I want misspelled execution fields and invalid Worker paths rejected before Worker startup, so that configuration cannot appear accepted while doing nothing.
14. As an Application author, I want unsupported Supervisor batch settings rejected explicitly, so that future configuration is not silently ignored in the first release.
15. As an Application operator, I want each map call to have its own capacity, so that simultaneous batches do not unexpectedly consume a shared quota.
16. As an Application operator, I want a capacity above twenty to remain usable, so that hidden backpressure defaults do not truncate my explicit choice.
17. As an Application operator, I want capacity larger than the input count to start only existing inputs, so that the framework does not invent or resplit work.
18. As an Application author, I want all item IDs and schemas validated before execution, so that a malformed late input cannot trigger a partial batch.
19. As an Application author, I want nested JSON input owned by the batch, so that changes to my original dictionaries cannot change running work.
20. As an Application author, I want each invocation to receive its own mutable input copy, so that one Worker cannot contaminate a sibling input.
21. As an Application author, I want duplicate IDs rejected within a batch while allowing the same business ID in a different batch, so that results remain unambiguous without a global ID registry.
22. As an Application author, I want an empty input sequence to return a completed empty batch, so that edge cases require no separate execution path.
23. As an Application author, I want final Outcomes ordered like inputs, so that I do not rebuild a mapping from completion order.
24. As an Application author, I want fast items to report completion and free capacity while an earlier item is slow, so that result ordering does not reduce throughput.
25. As an Application author, I want a successful None output distinguished from failure, so that valid empty answers retain their real status.
26. As an Application operator, I want every batch to record its effective concurrency and source chain, so that I can explain the actual execution configuration.
27. As an Application operator, I want simultaneous batches to keep separate execution records, so that concurrent updates do not overwrite evidence.
28. As an Application operator, I want records linked to the parent Run and definition/config revision, so that I can trace the conditions of each invocation.
29. As an Application operator, I want provenance to omit credentials, so that execution diagnostics do not expose authentication data.
30. As an Application author, I want every input to create a fresh Agent and Runtime, so that memory, state and sessions remain isolated.
31. As an Application operator, I want Hook, trace, usage and checkpoint ownership preserved, so that batching does not bypass existing invocation semantics.
32. As an Application operator, I want a completed item to close only its owned resources, so that sibling Workers and other batches remain active.
33. As an Application operator, I want capacity released after invocation cleanup, so that unfinished resource shutdown cannot be hidden by replacement work.
34. As an Application author, I want collect mode to retain item failures and continue independent work, so that I can inspect partial success.
35. As an Application author, I want fail-fast mode to stop new admissions and return every known input's disposition, so that failure does not erase the remaining plan.
36. As an Application operator, I want a stop request to halt new work and wait for started work, so that a returned batch reflects actual execution state.
37. As an Application operator, I want unstarted cancellations to have no invocation identity, so that the record does not invent executions.
38. As an Application operator, I want started work to retain its actual final status after a stop request, so that cancellation does not falsely imply that side effects were prevented.
39. As an Application operator, I want infrastructure and cleanup errors distinguished from ordinary item failure, so that collect mode cannot conceal a broken execution mechanism.
40. As an Application author, I want observer exceptions recorded without changing Worker results, so that progress reporting cannot corrupt the batch.
41. As an Application author, I want the framework to avoid automatically replaying a failed Worker, so that completed external actions are not duplicated by an unsafe retry.
42. As an Application operator, I want Application shutdown to reject new batches and drain registered work, so that root resources outlive their invocations.
43. As an Application author, I want bindings from a closed Application rejected immediately, so that stale handles cannot launch unowned work.
44. As an Application author, I want nested child calls to avoid submitting into their parent's occupied pool, so that a capacity of one does not create an executor deadlock.
45. As an Application author, I want Worker capacity independent of Runtime Tool concurrency and Provider limits, so that changing one setting does not change unrelated execution rules.
46. As an AgentLoom integrator, I want existing batch callers to have a documented migration path, so that adopting the typed Interface does not require silent behavioral changes.
47. As an AgentLoom contributor, I want deterministic tests through the public batch Interface, so that internal scheduler refactoring does not invalidate behavior tests.
48. As an AgentLoom reviewer, I want real Pi and smolagents Application evidence, so that a passing thread-pool test cannot hide broken Runtime integration.
49. As an AgentLoom reviewer, I want the implementation verified at the supported Python minimum, so that newer local APIs do not accidentally raise the framework's requirements.
50. As a NewsAgent maintainer, I want a reusable finite execution mechanism that leaves business ledgers and stage dependencies with the Application, so that later migration can proceed without first replacing the durable queue.

## Implementation Decisions

### Scope and module ownership

1. This specification delivers finite synchronous batch execution. A batch is one map call, not an entire Run, Worker definition, process, Provider account or durable submission. Application continues to own Planner, Reducer and stage dependencies.
2. Add a small Application facade and typed BoundWorker Interface. Reuse the existing definition inspection, effective configuration snapshots, AgentInvocation, ApplicationRunLifecycle and instance resource ownership. Do not duplicate Run initialization or cleanup.
3. Preserve existing domain semantics for Application, Supervisor, Worker, Run, Hook Plan, Hook Run, Skill and checkpoint. This extends the Application ownership decisions already accepted in the architecture specification; it does not reopen package layout or Hook authorization.
4. A BoundWorker is owned by one Application execution scope and cannot outlive it. Existing Run entry points obtain a facade bound to their current owner; a standalone context-managed open allocates and finishes a Run through the same lifecycle module without constructing a fictitious Supervisor.
5. The public batch Interface accepts a finite Sequence of WorkItems, optional call concurrency, collect/fail-fast policy, stop token and event sink. Single-input run and Agent-as-Tool remain separate convenience/adaptation paths. Internal Runner receives already-resolved execution configuration and does not implement a second precedence rule.

### Configuration, identity and snapshots

6. Add typed worker_execution configuration with defaults.concurrency and Application-scoped workers entries. Worker policy keys are canonical Application-relative YAML definition identities. Project-level configuration supplies common defaults and must not contain Application-private Worker mappings.
7. Resolve concurrency in this order: framework default one; project system default; Application system default; matching Application Worker value; explicit call override. Omission and None mean no override. A provided scalar replaces the lower value without clipping, addition or rate-based inference.
8. The full future contract inserts the current Supervisor binding above system values and below the call override. Supervisor execution configuration is not implemented by this specification; reject it explicitly rather than accepting and ignoring it. No path-based lookup may borrow another Supervisor's policy.
9. Every supplied concurrency value must be a strict positive integer. Reject booleans, string numbers, auto, zero and negatives. Validate each source, including overwritten lower layers. Reject unknown worker_execution fields, invalid or missing targets, symlink escapes and duplicate canonical identities.
10. Validate policy entries at the Application composition seam. A valid Worker absent from the policy table uses defaults; another valid configured Worker being unused by this batch is not an error. Application-specific requirements for an exhaustive stage list remain business validation.
11. Remove the mapping from Worker concurrency to Runtime parallel-tool requirements. Worker count does not determine whether one model response can contain multiple Tool calls. Do not add a process hard limit, hidden pending limit, default circuit breaker or concurrency auto mode.
12. Pin project/Application system settings to the execution scope. Capture a Worker's definition and effective model/configuration snapshot at first binding; repeated binding in that scope reuses that snapshot. A new scope can observe edited defaults and definitions.
13. Each map call resolves its own override and freezes execution policy before creating a Worker. Allocate a unique batch ID and persist parent Run identity, Worker identity, definition/config revision, input fingerprint, failure mode, resolved concurrency, winning source and source chain.
14. Save each batch's record separately through canonical runtime storage interfaces. The Application lifecycle serializes aggregation of references into the root manifest. Workers must not independently read-modify-write the shared root manifest, and atomic file replacement must not be treated as atomic field update.
15. Persist only provenance needed to explain execution, using existing redaction rules. Do not serialize model credentials or complete secret-bearing configuration into diagnostics.

### Input, outcomes and event contract

16. WorkItem IDs are nonempty stable strings unique within one batch. Batch identity disambiguates the same business ID used in another batch. Normalize, deeply copy and freeze JSON input; each invocation receives its own mutable copy. A frozen dataclass alone is insufficient.
17. Validate the entire finite sequence before admission. Invalid schema, invalid JSON or duplicate IDs reject the batch with zero Worker starts. Empty inputs return a completed empty BatchOutcome without allocating invocation resources. Generators and streaming input are not accepted.
18. Store each Outcome by input index and return a complete ordered result for every known input in normal collect, fail-fast and stop returns. Outcome records preserve item identity, optional invocation identity, status, output or structured cause, usage and cleanup diagnostics. Successful None output is valid.
19. Unstarted items canceled by stop or fail-fast have no invocation identity and a recorded cancellation reason. Started items retain their actual success or failure status even if the batch is stopped.
20. Emit completion progress without waiting for earlier input indexes. Serialize observer delivery through the controller and give events an event sequence number representing observation order. Do not promise an exact physical total order among simultaneous thread completions.
21. An ordinary observer exception is recorded, disables subsequent delivery to that observer and does not alter Worker Outcomes. A control exception such as KeyboardInterrupt follows the batch interruption contract instead of being swallowed as a diagnostic.
22. Batch status precedence is: infrastructure or cleanup-consistency errors raise WorkerBatchError; otherwise any item failure makes the batch failed; otherwise a stop observed during execution makes it stopped; otherwise it is completed. Fail-fast returns a failed batch with full known-input dispositions.

### Admission, invocation and shutdown

23. Maintain a separate bounded admission window for each map call. A top-level item occupies capacity from the final startup decision until invocation and owned resource cleanup finish. Direct outstanding submissions and invocations must not exceed resolved concurrency.
24. Use an explicit submit/wait window compatible with the supported Python minimum. Do not require Executor.map buffersize, allocate all Futures or Runtimes ahead of demand, or let ordered result delivery delay replenishment after fast items finish. The scheduler's working set is bounded; complete input snapshots and results still consume memory proportional to payload size.
25. Treat the executor as an internal implementation detail. A reusable Runner or BoundWorker must not hold a shared execution pool that causes concurrent batches or synchronous nested calls to wait behind their own parents. Agent-as-Tool and child Runtime work are outside this batch's top-level capacity.
26. Each item creates a fresh Agent/Runtime and reuses the existing complete invocation path for Hooks, checkpoint, trace, usage and cleanup. Do not call a lower-level Runtime directly to bypass ownership, and do not create a new root Run for each item.
27. Capture and rebind a separate Context for each submission. Audit which existing state is shared, derived or forbidden to share. Immutable definition/config snapshots and synchronized root memory may be shared; instance identity, Agent state, runtime and tool/resource owners are per invocation. Mutable coordinator, trace, usage, budget and workspace state require explicit concurrency contracts.
28. Register each batch with its Application owner before admission. Synchronize registration with owner shutdown. Exiting the scope rejects new batches, stops further admission in registered batches, drains them and then closes root resources. A closed binding or an invocation trying to synchronously close and wait for its own owner fails immediately.
29. Collect mode continues after ordinary item failures. Fail-fast stops admission after a failure is observed. A stop token stops admission without forcibly terminating started work. Process completed failures and stop state before filling slots, and recheck stop at the invocation startup seam to cover submitted-but-unstarted races.
30. Cancel queued Futures only when they have not begun; account for startup races using actual state. Wait for started invocations and cleanup before returning. Never mark a running invocation canceled merely because its parent requested stop.
31. Runtime or resource cleanup failure stops replenishment and raises WorkerBatchError with phase, partial Outcomes, primary cause and cleanup errors. Retain ownership of handles not confirmed closed for lifecycle cleanup; do not release capacity and start replacement work, report success or claim a complete drain.
32. Executor failure, required record-persistence failure and consistency/invariant failure are batch infrastructure errors, not ordinary item failures. Stop further work, preserve evidence, drain what can finish and propagate the structured batch failure. Control exceptions also stop/drain before propagation.
33. Stop-admission-and-drain has no fixed exit deadline. Running Python threads and arbitrary synchronous Tool callbacks cannot be killed by Future cancellation. Pi process exit does not prove Python callbacks have exited. Hard thread timeouts and universal Runtime interruption are not introduced.
34. Do not automatically retry the full Worker invocation. Retryable metadata is diagnostic, not proof of safe replay after side effects. Existing Provider request retry policy remains with its owning Adapter; business attempt retry requires Application-specific idempotency and commit rules.
35. The synchronous batch Interface must not secretly switch event-loop strategies or call asyncio.run. Async callers use an explicit offload Adapter with documented cancellation behavior; a native async map is deferred.

### Migration and compatibility

36. Audit existing batch and parallel-Worker callers, then route supported legacy entry points through the new execution module during a documented deprecation period. Legacy batch and parallel-launcher adapters continue accepting lists of task dictionaries and returning input-ordered lists of TaskResult, while the typed Interface returns BatchOutcome. Generate unique per-batch index identities for adapted items without rewriting the supplied task payload or its legacy task_id receipt. Map successful, failed and unstarted-canceled Outcomes to completed, failed and skipped legacy statuses respectively, preserving the cancellation reason. Explicitly announce changes to ordering, automatic concurrency and circuit-breaker behavior.
37. Reject legacy auto and retired Worker-local concurrency with actionable migration diagnostics; do not silently translate them into a different capacity. Update affected maintained examples, documentation and caller configuration together.
38. Runtime Tool dispatch stays with Pi/smolagents. Do not introduce a common Tool Scheduler or couple Tool thread limits to Worker capacity. Any existing concurrency-safety metadata discrepancy in a Runtime Adapter is a separately scoped fix.
39. NewsAgent's durable Store, Consumer, lease recovery, StageLedger and stage seal remain under Application ownership. This specification establishes the finite execution seam and a fixed-plan integration fixture; it does not replace the long-lived consumer or change business retry policy.

## Testing Decisions

### Confirmed seam and testing principles

- The user confirmed Application Run → BoundWorker.map as the main seam. Validate observable starts, owned resources, receipts, errors, progress and persisted batch evidence through that Interface. Avoid tests of private executor queues, Future dictionaries or semaphore counts.
- Reuse the existing AgentRuntime Interface to supply controlled Runtime behavior behind the real Application composition. Use Event/Barrier to coordinate starts, completion and cleanup; deadlines and diagnostics keep a failed concurrency test from hanging.
- Additional pure resolver tests are limited to source/type edge cases that cannot be covered economically through the main seam. Do not duplicate full behavioral suites for each helper module.
- Validate Pi and smolagents with the same external batch contract through real Application runner/CLI execution. Controlled Provider fixtures make faults deterministic; bounded live-Provider smoke cases demonstrate actual model/runtime wiring separately.
- Test the delivered implementation at the project's supported Python minimum and the normal development version. Passing probes of the old executor or historical Runtime tests does not count as acceptance of the new batch module.

### Required acceptance matrix

| Scenario | Observable acceptance |
| --- | --- |
| Adequate independent work, capacity 1, 2, 20 and 32 | Top-level active work never exceeds N; gated tasks can attain N; 32 is not silently capped at 20 |
| Capacity larger than input count, including a value above 64 | Only supplied inputs start; no invented process ceiling rejects or clips the value |
| Two concurrent batches for the same Worker, each capacity 4 | Each can start four; combined work can reach eight; stopping one does not stop the other |
| Project default, Application default, per-Worker value and call override | Effective value, winning source and ordered provenance match the documented chain |
| Calls using override 4, default 20 and override 2 on one binding | Each receipt is correct; configuration and binding defaults remain unchanged |
| Every invalid concurrency type, overwritten invalid layer and unknown field | Clear rejection before any Worker starts; no silent fallback |
| Invalid policy target, duplicate canonical identity or escape | Definition error through existing preflight semantics; no Runtime construction |
| Duplicate IDs or malformed item near the end of a sequence | Whole batch rejected before any item runs |
| Empty inputs and successful None output | Empty completed receipt and valid success status respectively |
| Caller mutates nested original input after admission | Invocation payload and stored input fingerprint retain the original snapshot |
| One invocation changes its mutable input copy | Sibling payloads and batch snapshots remain unchanged |
| First input blocked while later inputs complete | Later completion is observable and slots refill; final result still follows input order |
| Ordinary failures in collect mode | Other work continues and failures retain structured causes and matching identities |
| Failure in fail-fast mode | No later admissions after observed failure; unstarted items canceled; started work finishes before return |
| Explicit stop with a started item blocked | Batch has not returned until the item and cleanup finish; started item has a real terminal status |
| Stop races with a submitted but unstarted item | Item either never starts and has no invocation ID, or starts and is drained with its actual outcome |
| Observer ordinary failure and observer control exception | Diagnostics preserve Worker results for the first; stop/drain and propagate for the second |
| Runtime creation, close or owned-resource close fails | Original and cleanup causes retained; cleanup failure stops replenishment and is not reported as success |
| Tool side effect succeeds before an invocation error | One execution only; no automatic full-Worker replay |
| Owner exit races with new batch registration | Every accepted batch is drained; every rejected batch starts zero work; stale bindings fail immediately |
| Parent invocation with capacity one calls a child Worker | Child does not wait in its parent's occupied executor or capacity domain |
| Concurrent batches, repeated calls and sibling completion | Agent identity, Hook ownership, checkpoint association, usage and resources do not cross-contaminate |
| Concurrent persistence of multiple batch records | Both records and their root references survive; no lost update or credential disclosure |
| Worker concurrency changes while Runtime emits multiple Tool calls | Worker capacity and Runtime Tool dispatch remain separate behaviors |
| Fixed business plan executed at capacities one and twenty | Item IDs, count, payloads and fingerprint unchanged; controlled Worker outputs and reduction equivalent |
| Real Pi and smolagents Application runner/CLI | Run result, batch evidence, failures and cleanup agree with the deterministic external contract |

### Prior art and completion gate

Existing tests already exercise effective-config source retention, invalid overridden values, pinned running
definitions, Worker path validation, fresh concurrent Agent-as-Tool instances, parent Context preservation,
parallel session/Hook ownership, Run receipts and real Pi process shutdown. Reuse those behavioral fixtures
and extend the high-level Application contract instead of adding a second lifecycle simulator.

Completion requires the acceptance matrix, supported-version checks and both Runtime Application validations
on the delivered revision. Report commands, receipts, relevant artifacts and independently measured resource
outcomes. Preserve failed attempts and cleanup diagnostics. Live model outputs need schema validity, item
coverage and valid evidence, not byte-for-byte equality. If required Runtime acceptance cannot run, report
it as incomplete rather than treating a mock as the missing evidence.

## Out of Scope

- Run-wide, process-wide or cross-machine shared capacity; global hard limits, fairness guarantees and dynamic autoscaling.
- Agent-as-Tool shared admission, lineage-aware cycle scheduling, and automatic aggregation of model Tool calls into batches.
- Supervisor batch-default implementation. Its eventual position in the precedence chain is fixed; first-version rejection is required.
- Generator input, streaming Outcomes, a native async map, broker/workflow engines and general durable serve interfaces.
- Forced termination of running Python threads, a fixed drain deadline, universal Tool cancellation or rollback of remote side effects.
- Framework-default full-Worker replay, automatic circuit breaking and inferred concurrency from Provider RPM.
- NewsAgent long-lived Consumer migration, queue/database redesign, lease/fencing changes, frozen-snapshot recovery and task-sizing migration. These require a separate business acceptance specification.
- Changing Pi/smolagents Tool dispatch, Provider admission policy, Hook authorization, Goal behavior, memory algorithms or package structure.

## Further Notes

- The capacity scope was explicitly selected by the user: one map call. The main testing seam and real Runtime validations were also explicitly confirmed.
- This specification carries the first deliverable from the concurrency proposal. The agreed future precedence is system values < Supervisor binding < call override; future Supervisor defaults will govern its batch calls, not the number of independent Agent-as-Tool calls.
- The repository's configured issue tracker is linora-u/AgentLoom, with the ready-for-agent label. This specification does not start implementation, create an integration branch, commit, push or publish a release.
- Reuse the accepted Application ownership and definition decisions in [architecture specification #67](https://github.com/linora-u/AgentLoom/issues/67). That issue is closed; this feature is a separate issue and does not reopen the prior directory migration or close unrelated configuration issues.
- Local investigation reproduced the old hidden twenty-item admission cap, caller-input mutation, inability to cancel running Futures, shared mutable Context values, unvalidated extra execution fields and lost root-manifest updates. These are problem evidence, not acceptance of an unimplemented replacement.
- Official [Python Executor documentation](https://docs.python.org/3.12/library/concurrent.futures.html#concurrent.futures.Executor.shutdown) describes running-thread cancellation limits; [Python 3.14 documentation](https://docs.python.org/3.14/library/concurrent.futures.html#concurrent.futures.Executor.map) records the newer buffersize parameter. The implementation must remain compatible with the project's lower supported version.
- NewsAgent's existing transactional owner/status/lease checks already reject stale database submissions. A general requirement to replace them with monotonic fencing is not introduced. External side effects and durable retries remain distinct from finite-map receipt ordering.
- Writing and labeling this specification means the feature is ready to be implemented, not that product code exists or that implementation acceptance has passed.
