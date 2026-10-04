# Spec：AgentLoom 运行存储整体重构

状态：目标规格，非实现进度报告。日期：2026-09-28。本规格涵盖整个 `.agentloom/` 运行存储及其生产、读取和恢复路径；实施前现有目录仍按旧代码工作。

## Problem Statement

用户运行一个 Application 后，希望长期保留答案、执行经过、完整工具证据和可恢复状态，并能直接看懂这些记录在哪里。当前同一次 Task 的内容散落在 Run、trace、checkpoint、工作区、调度和自学习等多处。一次简单运行会预建许多空目录，工具回执、审计事件和日志还可能重复保存相同正文。Run、Task 与持久内容的引用关系不容易从目录中看出来。

现有自动保留期限会删除 Run 或其 `artifacts`，连最终答案也可能随之消失；成功后的 checkpoint 可能被清除，调度历史会按条数裁剪，日志会轮转丢弃旧段。Shell 等工具有输出截断、大小守卫或仅保留短预览的路径，导致 Agent 看到的内容与可追查的完整证据混淆。用户希望完整捕获工具输出，给 Agent 的内容仍按上下文预算展示，并在真正写盘失败时直接得到错误。

当前脱敏在部分 trace、模型投影和自学习路径中无条件执行，其他 Run、工具和调度记录又可能存原文，缺少一套可选择、可覆盖且统一作用于持久化边界的规则。脱敏默认开启，可显式关闭；启用时只保存脱敏处理的版本；全局配置可由 Application 覆盖。普通非 JSON 日志必须照常记录，不能因解析格式而停止 Agent。

## Solution

把 `.agentloom/` 作为一个有明确所有权的运行存储根目录，按 **Run、Task、长期 Agent 工作区、调度、自学习**组织。Task 是可跨多次 Run 续跑的逻辑任务；Run 是一次执行尝试。Task 的事件记录是一份按序追加的执行事实，内容正文通过持久引用取回；Run 保存本次尝试的结果、日志、工具输出及已提交回执。Checkpoint 只负责恢复，不从日志或事件反推。目录按实际需要创建，不预建空的功能子目录。

目标目录结构如下。尖括号表示标识符，省略号表示由该功能定义的实际文件；每个 Tool call 只产生实际用到的输出文件，没有发生的功能不产生相应目录。Checkpoint 内的具体 runtime 状态由所选适配器编码。

```text
.agentloom/
├── runs/<application-id>/<run-id>/
│   ├── manifest.json
│   ├── result.txt
│   ├── runtime.log
│   ├── outputs/<call-id>.stdout
│   ├── outputs/<call-id>.stderr
│   ├── outputs/<call-id>.result
│   ├── receipts/<call-id>.json
│   └── artifacts/...
├── tasks/<application-id>/<task-id>/
│   ├── events.jsonl
│   ├── payloads/<digest>.blob
│   └── checkpoint/
│       ├── state.json
│       ├── final.json
│       ├── workers/...
│       └── runtime/...
├── agents/<application-id>/<agent-path>/
│   ├── insights.md
│   └── tasks/<task-id>/...
├── schedules/
│   ├── jobs.json
│   └── executions/<execution-id>/
│       ├── record.json
│       ├── stdout.log
│       └── stderr.log
├── learning/
│   ├── self_learning.db
│   ├── sessions/events/...
│   └── reviews/{project,applications}/...
└── .tmp/...
```

正式记录默认永久保存，包括成功和失败的 Run、事件、完整工具证据、调度执行历史、自学习记录，以及启用 checkpoint 时的最终 checkpoint。锁、心跳、未提交临时文件、可重建索引与被替代的中间缓存不属于正式历史，可在确认其所有者已停止后清理。不按天数或条数自动删除正式记录。保留手动清理能力：先明确列出对象及其关联内容，再由操作员执行。

工具的 stdout、stderr 和其他原始结果在产生时流式写入持久证据，不设 AgentLoom 的总字节上限，也不为完整捕获一次性读入内存。模型和终端显示使用独立的有界预览与可分页的完整内容引用；预览不能被称作完整输出。任何已提供给 Agent 的引用必须已成功提交且可读取。实际磁盘写入失败直接使相应执行报错。

新增模块化脱敏策略，由全局 `system.yaml` 给出默认值，Application 的 `system.yaml` 可覆盖；未配置时默认开启。策略在 Run 开始时固定，所有 AgentLoom 管理的持久记录经同一落盘边界处理，包括 Run、Task、checkpoint、工具输出、调度执行输出和自学习。启用时不另存未脱敏副本。普通非 JSON 文本按文本规则处理，无命中时照常保存；真正无法处理的二进制段或模块故障写入缺失标记、原因和字节数，Agent 继续运行。提示注入检查与脱敏是独立能力。

实施时先在隔离运行根目录验证新版生产、读取、恢复和清理路径。切换前确认没有正在使用旧运行根目录的任务，然后删除旧 `.agentloom/` 内容，由新版重建；不提供旧格式读取、迁移或兼容分支。Application 定义、全局配置、技能和项目业务输出不在旧数据删除范围内。

## User Stories

1. As an Application operator, I want one comprehensible runtime directory layout, so that I can find a Run without tracing several unrelated roots.
2. As an Application operator, I want every Run linked to its Task, so that I can follow a resumed Task across attempts.
3. As an Application operator, I want a completed Run's actual answer to remain available, so that a one-off execution remains useful indefinitely.
4. As an Application operator, I want failed and interrupted Run records retained, so that I can investigate them later.
5. As an Application operator, I want the final checkpoint retained when checkpointing is enabled, so that a successful Task still has its final recoverable state on record.
6. As an Application operator, I want intermediate checkpoints kept while a Task may resume, so that interruption does not destroy safe continuation.
7. As an Application operator, I want formal records to have no automatic expiry, so that a quiet project does not lose history on its next run.
8. As an Application operator, I want no empty feature directories created in advance, so that the directory reflects what actually happened.
9. As an Application operator, I want one Task event sequence, so that I can inspect execution order without reconciling several partial journals.
10. As an Application operator, I want each Step associated with its Run, Agent and Tool call, so that model and tool activity can be traced together.
11. As an Application operator, I want runtime logs to remain readable, so that I can inspect a Run without decoding raw event records.
12. As an Application operator, I want full tool stdout and stderr retained, so that an Agent preview does not erase evidence.
13. As an Application operator, I want a clear marker when a source itself did not provide complete output, so that incomplete evidence is never presented as complete.
14. As an Application operator, I want a write failure to fail the execution plainly, so that a successful Run never has a silently missing record.
15. As an Agent, I want an output preview sized for my context, so that a very large tool result does not exhaust the model window.
16. As an Agent, I want to retrieve the remaining output through a stable reference, so that I can continue reading when the preview is insufficient.
17. As an Agent, I want byte-based pagination for very long lines, so that I can retrieve a large JSON line without truncation.
18. As a Task owner, I want evidence references to survive Run completion and resume, so that later attempts can read earlier evidence.
19. As a Task owner, I want resume to verify checkpoint and committed tool evidence, so that an uncertain side effect is not silently replayed.
20. As a Scheduler operator, I want definitions and execution history under one schedule area, so that I can inspect each invocation and its output.
21. As a Scheduler operator, I want every execution record retained by default, so that old scheduled outcomes remain auditable.
22. As a self-learning reviewer, I want sessions, reviews and the ledger grouped together, so that learning artifacts have a clear owner.
23. As an Agent author, I want long-lived insights kept apart from per-Task evidence, so that durable learning is not mistaken for a Run artifact.
24. As a system administrator, I want redaction enabled by default and explicitly disableable, so that Applications can choose their content policy.
25. As a system administrator, I want one global redaction setting, so that the default applies throughout AgentLoom.
26. As an Application author, I want my Application system configuration to override the global redaction setting, so that an Application can choose its own policy.
27. As a system administrator, I want a replaceable redaction module, so that detection rules can evolve without changing storage consumers.
28. As an Application operator, I want redaction applied before persistence, so that enabled Runs do not retain a second raw copy.
29. As an Application operator, I want plain text logs processed without JSON parsing, so that unfamiliar log formatting does not stop an Agent.
30. As an Application operator, I want an unmatched text log recorded unchanged, so that the redactor does not discard ordinary evidence.
31. As an Application operator, I want an unprocessable binary segment represented by a reason and byte count, so that the Agent continues without silently storing raw bytes under an enabled policy.
32. As an Application operator, I want secret redaction and prompt-injection checks to be separate, so that changing one policy does not unexpectedly disable the other.
33. As a Studio user, I want Run detail and schedule views to read the new canonical records, so that the interface agrees with the runtime.
34. As a Python or CLI user, I want the public Run result and inspection interfaces to agree, so that a storage change does not change the answer.
35. As an operator, I want a manual cleanup preview listing affected records and sizes, so that I can choose what to remove.
36. As an operator, I want manual cleanup to preserve references among retained records, so that deleting one object does not corrupt another Task.
37. As a maintainer, I want old storage readers removed at cutover, so that the codebase has one active format.
38. As a maintainer, I want the storage switch verified before old data is deleted, so that a layout mistake is caught in an isolated root.
39. As a maintainer, I want the switch to wait for active runs to finish, so that a running process does not write into a deleted runtime root.
40. As a maintainer, I want behavior tests at public Application, Scheduler and inspection entry points, so that internal reorganizations do not invalidate useful tests.

## Implementation Decisions

### Ownership and storage contract

- Application lifecycle remains the owner of Run allocation and terminal status. The runtime storage component owns path construction, safe writes, reads and cleanup planning. CLI, Studio, Scheduler, runtimes and self-learning consume that contract rather than assembling storage paths independently.
- The new manifests and events use one explicit storage schema version. Old versions are rejected rather than interpreted through compatibility readers.
- Task ID remains stable across resume; every new attempt has a new Run ID. The Run manifest records the Task ID, terminal state, result location and committed evidence references. Task events carry Run, Agent, Step and call identities.
- Consolidate Task trace and execution audit into one append-only event sequence with immutable large payloads. A Run points to its relevant Task events. Keep tool receipts as separate commit/recovery evidence, with references to the one retained output rather than a second raw copy. Human-readable runtime logs are a view for inspection and remain permanently available.
- Checkpoint remains authoritative for resume. It is not reconstructed from events or logs. A final checkpoint is kept after success when the feature is enabled; a disabled checkpoint feature does not create a fictional snapshot. Replaceable snapshots and ContextStore entries are operational caches, while referenced, committed evidence is permanent.
- Move long-lived Agent workspace state, schedule state and self-learning state into their named owners under the same root. Their producers, Studio readers, CLI commands, recovery logic and integrity checks must all use the new contract. No old root aliases or compatibility readers remain.
- Create directories lazily. Temporary files and locks may live beside their owner or in a transient area, but they are not exposed as formal history. Preserve existing symlink protection, atomic publish, integrity checks and active-run leases.

### Retention and manual cleanup

- Remove automatic age and count pruning for formal Run records, results, Task events, payloads, runtime logs, checkpoint snapshots designated final, schedule execution history and learning history. Remove log rotation that discards older formal segments. A future change to this policy requires a new explicit decision.
- Permit cleanup of inactive locks, heartbeats, incomplete temporary writes, recomputable indexes and superseded intermediate caches. Do not classify complete stdout, failed runs or final answers as cache.
- Manual cleanup offers a read-only preview before applying deletion, listing selected owners, dependent records and total size. Apply only a coherent owner unit or refuse deletion that would leave a retained reference dangling. The command does not silently select items by age.

### Output capture and inspection

- Capture command stdout and stderr at the producing process boundary as a stream. Do not route complete output through a bounded in-memory accumulator, a success-only interceptor or a fixed 100 MB watchdog. Apply the same complete-capture rule on success, failure, timeout and background execution; record the outcome separately from captured bytes.
- The model-visible Tool result is a bounded projection chosen using context and bridge limits. It includes a stable reference when more content exists. Retrieval is bounded per request, supports byte pagination and validates the Task authorization and persisted content. Terminal and Studio views identify a preview as a preview.
- If an upstream runtime or SDK has already truncated output, record that fact and do not claim complete capture. Where the upstream path lacks a complete stream, the implementation must capture earlier in the process path before declaring this requirement satisfied.
- Persist evidence before publishing its reference to an Agent. A failed required write, including disk-full, fails the execution; do not continue as though evidence were complete. Hashes and references describe the final persisted bytes.

### Optional redaction

- Add a validated redaction policy to system configuration. Missing policy means enabled. Global configuration supplies the default; an Application may override it in its system configuration. Agent YAML has no separate override. Pin the effective policy to the Run, including resumed execution; project-level work without an Application uses the global policy.
- A modular redactor receives text as a stream or structured values before any AgentLoom-managed persistent sink writes them. It returns the bytes to persist and a small processing status. The same policy covers model/tool facts, Run result, logs, receipts, Task payloads, checkpoint state, schedule execution output and self-learning records. It does not rewrite user files that a Tool explicitly edits outside AgentLoom's runtime storage.
- Ordinary text need not parse as JSON. Apply text rules to non-JSON logs, including patterns crossing output chunks. A valid text segment with no match remains unchanged and is recorded normally. This is a rule-based transformation, not a guarantee that every possible secret is recognized.
- Under an enabled policy, no raw temporary copy or raw duplicate is committed. For truly undecodable binary content or a redactor failure, persist a placeholder with reason and original byte count, and continue the Agent. The original segment is unavailable for later inspection. A disk write failure is different: it raises an execution error.
- Validate any incoming capture or protocol digest before transformation; compute stored content hashes and references after transformation. Keep prompt-injection detection separate from the configurable redaction switch. Remove existing unconditional storage redaction where it would contradict the effective policy.
- Schedule execution output resolves the owning Application policy before launching the child; project-level maintenance uses the global policy. Policy changes affect new Runs, not records already committed under an earlier policy.

### Cutover and documentation

- Implement and verify the new layout in an isolated runtime root. Before cutover, check active process ownership, leases and open runtime files. Do not terminate an unrelated active process merely to accelerate the switch.
- Once the new code and readers pass acceptance, delete the old `.agentloom/` runtime data as one unit and allow the new implementation to create it. Do not convert, archive or read old records. The deletion does not include Application definitions, global configuration, skills or unrelated project output.
- Update runtime, checkpoint, schedule, self-learning and observability documentation to reflect this policy. Prior specifications remain authoritative for Step, Hook, Task and resume semantics; this specification supersedes their conflicting storage layout, default redaction, automatic retention and successful-checkpoint cleanup statements.

## Testing Decisions

- **Primary seam:** run a real Application through its public execution interface against an isolated runtime root, then inspect its public result, retrieval interface and Studio Run detail. Assert user-observable records, references and behavior, not helper names or incidental internal call order. Existing Application, Pi recovery/receipt, Studio durable-run and acceptance fixtures provide prior art.
- Exercise Pi and smolagents with success, failure, interrupt/resume, Worker calls and background Shell execution. Confirm each Run links to one Task event sequence, output evidence can be read in full, final answers persist, final enabled checkpoints remain and no unused directories appear.
- Use deterministic producers for small, large, multiline, long-single-line, binary and chunk-split outputs. Check exact persisted bytes when redaction is disabled; with redaction enabled, check the complete transformed text, bounded Agent preview and paginated retrieval. Inject a genuine disk write error and verify the Run errors. Existing Shell artifact, Pi Tool and context retrieval tests provide prior art.
- At the same public seam, test global policy, Application override, no-policy behavior, plain text non-JSON logs, no-match text, split-match text, invalid encoding and module failure. Verify enabled storage contains no raw duplicate, the placeholder is recorded where processing fails, and prompt-injection scanning retains its separate behavior. Existing configuration and self-learning redaction tests provide prior art.
- **Additional existing seams needed for independent owners:** run scheduled jobs through the Scheduler's public command/runner and read history through its public presentation path; use the self-learning public recording/review interface for database and review persistence. These paths share the same storage policy contract. Existing schedule runner/store and self-learning review tests provide prior art.
- Verify manual cleanup by previewing coherent Task, schedule and learning selections, applying a chosen deletion, and checking that retained records still resolve. Verify no automatic history loss across simulated time passage, repeated runs or large execution counts. Existing schedule retention and runtime retention tests must be updated to the new contract.
- Verify Studio and Python inspection do not depend on legacy paths, and verify a fresh checkout creates only the new runtime root. Preserve path-security and atomic-write behavior under symlink, crash-tail and interrupted-publish cases. Use focused failure injection where the resulting behavior would otherwise be hard to observe.
- Cutover acceptance uses a disposable runtime root for the full suite, followed by one real new-format Run after the old root is removed and no active process owns it. The test reports which records were created and confirms result, events, output, receipts and checkpoint are mutually consistent.

## Out of Scope

- Migration, compatibility readers or archived copies of the old `.agentloom/` data.
- Automatic deletion of formal history by age, byte count or number of executions.
- Changes to Agent YAML task semantics, Tool authorization, Hook behavior, model choice, Step definitions or external side-effect guarantees.
- Rewriting arbitrary files that tools create or edit elsewhere in the project; the redaction policy governs AgentLoom-managed persistence.
- A remote trace service, a new analytics database, or a new user interface solely for this storage change.
- A claim that rule-based redaction recognizes every secret or that an upstream SDK's truncated payload is complete.

## Further Notes

- The existing [capability architecture](agentloom-capability-architecture.md) defines Task, Run, Agent invocation, Step and Tool call. The [Step observability specification](unified-step-observability-and-large-tool-results.md) defines model-visible previews, content references and checkpoint separation. Their storage and retention assumptions predate the decisions recorded here.
- The observed old runtime root contains only a small number of Runs and trace files, with empty checkpoint/workspace directories. Its size is not a reason to keep a second format. Whole-root deletion avoids leaving trace references to removed Runs.
- At specification time, another process was running from the project directory. Its presence is a cutover condition to recheck, not a reason to modify or stop that process while writing this specification.
