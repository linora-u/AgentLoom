# YAML Agent 任务、连续会话与结构化 Subagent 契约

规格日期：2026-09-22；修订日期：2026-09-26。本修订取代本文旧版关于 `workflow`、运行时 `task` 参数、默认 Worker `task` 参数和 Markdown Agent 定义的决定。本文描述目标契约，不声称当前代码已经实现。

## Problem Statement

作为 AgentLoom 的维护者，用户希望 Agent 的长期指令与用户任务各有明确位置：可选的 `system_prompt` 进入 system / instructions 通道；每个 Agent YAML 必须写 `task`，作为 user message。`task` 可以是一条文字，也可以是预先排好的多条文字。多条任务在同一个 Agent 会话中依次送入，上一条完成后才送下一条。

早期实现曾把 `workflow`、本轮任务和 Tool 参数包装成 `<task_spec>`、`<workflow>`、`<task_request>` 等文本。原版规格已要求删除这套 Prompt Protocol，现有代码也已不再使用这些标签。本修订保留该要求，不把它重新引入。

迁移前的实现把必填 `workflow` 当作系统指令，并允许 `--task` / Python 调用参数在 YAML 外提供本轮任务。更早的版本还曾用 `workflow` 列表表达多轮、以 `description` 代替缺失任务，并使用 `agent_function_schema`；这些历史语义不得成为新配置的兼容规则。Markdown Agent 定义曾把正文隐式写入 `workflow`，也必须退出定义入口。

用户希望任务来源收敛到 YAML，同时保留多 Agent 能力：Supervisor 显式选择允许调用的 Worker；Worker 自动注册成 Tool，但它的任务仍来自自己的 YAML。Supervisor 只可通过 Worker 声明的 `input_schema` 传入本次调用的数据。既有 `output_schema` 继续由 Runtime 真正约束、校验和纠正结构化结果。

## Solution

删除整套 Prompt Protocol，让 AgentLoom 使用 Runtime 原生语义：

- Agent 定义只使用 YAML。每份 Supervisor 和 Worker YAML 都必须有非空 `task: string | list[string]`；列表非空，且每项都是非空字符串。字符串是一条 user message；列表按顺序成为多条 user message，不拼成一个 prompt。
- 可选 `system_prompt` 直写字符串，或写成仅含 `path` 的对象。路径相对当前 Agent YAML 解析；读取的内容进入 system / instructions 通道。缺失 `system_prompt` 时不伪造一条系统指令，由 runtime 使用自身默认行为。
- 所有 Application 入口都读取 YAML `task`。移除 CLI `--task` 和公开 Python Application / Agent 调用中的任务覆盖参数；`--resume` 只恢复同一份定义，不接受新的任务文字。runtime adapter 内部仍须把 YAML 任务送入 pi / smolagents 原生消息接口。
- 一个列表属于一个逻辑 Task；一次执行尝试是一个 Run。各项共用该 Agent 会话与模型历史，每项的实际回复独立展示、记录；公开 Run 结果直接取最后一项的实际 Runtime 输出，允许无最终回复时为 `null`，不拼接回复或用 Goal 证据补位。恢复可创建新 Run，但沿用 Task 与会话状态。
- 启用 `goal: true` 时，列表中的每项分别成为一个 Goal。只有该项显式调用 `update_goal(status="complete")` 后才送下一项；普通最终回复不能推进列表。Goal 证据仍属于 Goal 状态，不代替 Agent 回复。
- `description` 只描述 Agent 的能力，用于展示和 Subagent Tool 描述。
- Supervisor 继续通过 `worker_agents` 显式列出允许注册的 Subagent。
- Subagent 不再配置 `agent_function_schema` 或额外的 `tool` 块。框架直接复用 Subagent 的 `name` 和 `description` 生成 Tool。
- 未配置 `input_schema` 时，Worker Tool 无参数；不自动生成 `task` 参数。配置 `input_schema` 时，使用 JSON Schema Draft 2020-12 描述调用数据。Tool 参数不能覆盖、替换或追加 Worker YAML 任务。
- 未配置 `output_schema` 时，Agent 的实际最终回复保持普通文本；没有最终回复时返回 null，不从 Goal 证据合成回复。
- 配置 `output_schema` 时，Runtime 在 Agent 的最终输出阶段执行真实结构化约束和本地校验。校验失败不能离开 Agent，而是进入同一会话的下一步纠正，统一受 Agent 既有执行预算约束。
- canonical Runtime、Tool 和 Application 结果保留原始 JSON 类型。只有投影给模型或终端展示时才序列化；大结果继续使用现有 Context Engine 和 ContextRef。
- Pi 和 smolagents 分别在自己的适配层实现结构化输出，不支持该能力的 Runtime 或 Provider 在执行前明确失败，不退化成 prompt 技巧。

## User Stories

1. As an Application author, I want every Agent's YAML `task` to be the only configured source of user tasks, so that CLI and Python callers cannot silently change the request.
2. As an Application author, I want one `task` key to accept a string or ordered string list, so that successive user turns need no second configuration field.
3. As an Application author, I want an optional inline or file-backed `system_prompt` to remain separate from `task`, so that its message role is predictable.
4. As an Application author, I want `description` to remain Agent metadata, so that it can describe capability without changing execution input.
5. As an Application author, I want ordinary Markdown and Mermaid text in `system_prompt` or `task` to reach the intended message channel without framework-specific extraction.
6. As an operator, I want each configured task item to become a new user turn in one Agent session, so that the next item can use the prior conversation.
7. As an operator, I want a failed item to stop the list and a resumed Run to continue from the last safe committed position, so that completed work is not repeated.
8. As a Supervisor author, I want to list allowed Subagents explicitly with `worker_agents`, so that adding a file to a directory cannot silently expand Agent authority.
9. As a Supervisor author, I want each selected Subagent automatically exposed as a Tool, so that I do not maintain duplicate wrapper code.
10. As a Supervisor author, I want the Subagent's `name` to become the Tool name, so that execution traces and configuration use one identity.
11. As a Supervisor author, I want the Subagent's `description` to become the Tool description, so that capability text is written once.
12. As a Subagent author, I want a simple Agent to work without schema configuration, so that basic delegation stays concise.
13. As a Subagent author, I want an omitted `input_schema` to produce a zero-argument Tool, so that the Worker performs its YAML task without invented parameters.
14. As a Subagent author, I want an omitted `output_schema` to produce ordinary text, so that structured output remains optional.
15. As a Subagent author, I want to declare a standard JSON Schema for complex input, so that integers, numbers, booleans, objects and arrays are not coerced into strings.
16. As a Subagent author, I want to declare a standard JSON Schema for output, so that downstream Agents receive validated data rather than prose promises.
17. As a Subagent author, I want output schemas to allow any legal JSON root type, so that lists and primitive results do not need meaningless wrapper objects.
18. As a Subagent author, I want local schema references to work, so that repeated structures can be defined once.
19. As a security-conscious operator, I want remote schema references rejected, so that validation never performs unapproved network resolution.
20. As a Supervisor, I want Tool arguments validated before Subagent execution, so that malformed model calls do not start partial work.
21. As a Supervisor, I want Worker Tool arguments to carry only schema-declared call data, so that they do not replace the Worker's YAML task.
22. As a Supervisor, I want multi-field Tool data preserved as typed JSON, so that the Worker does not reverse-engineer numbered prose.
23. As a Supervisor, I want a structured Subagent result returned as its original JSON value, so that I can reason over fields without parsing Python representations.
24. As a Supervisor, I want a text Subagent result returned as plain text, so that simple collaboration remains natural.
25. As a Supervisor, I want an invalid Subagent final result reported as an `output_validation` Tool failure, so that I can retry, choose another Tool or explain the failure.
26. As an Agent, I want schema validation feedback delivered inside my current session, so that I can correct my answer with the context I already built.
27. As an Agent, I want output correction governed by my existing execution budget, so that there is no second hidden retry counter.
28. As an operator, I want a run to fail when its execution budget ends with an invalid output, so that an output schema remains an enforceable contract.
29. As an operator, I want unsupported structured-output combinations rejected before model or Tool execution, so that the framework never silently weakens a contract.
30. As an operator, I want Provider transport retries kept separate from schema correction, so that identical invalid output is not blindly resent as a network retry.
31. As an operator, I want each Subagent invocation to retain an isolated Runtime and model history, so that repeated or concurrent Tool calls cannot leak state.
32. As an operator, I want Supervisor and Subagents to share the task-scoped Context Engine, so that compressed evidence remains retrievable across Agent boundaries.
33. As an operator, I want canonical records to retain the original structured result, so that compression cannot destroy durable data.
34. As an operator, I want only the model-visible projection compressed, so that large Tool results can use ContextRef without changing the actual Tool outcome.
35. As an operator, I want ContextRef used only when the receiving Agent can retrieve it, so that compression never replaces data with an unusable pointer.
36. As an operator, I want existing context-budget compression to govern large results, so that the feature does not introduce an arbitrary output-size business limit.
37. As a Python API consumer, I want the final item's actual result returned as a native JSON-compatible value, including null when there is no final reply, so that the framework does not fabricate an answer.
38. As a CLI user, I want text mode to render structured results as readable JSON, so that terminal output remains understandable.
39. As an automation author, I want JSON and JSONL CLI modes to retain native JSON values, so that output is not double encoded.
40. As an observability consumer, I want lifecycle events to carry the same native result as the public API, so that logs and return values cannot disagree.
41. As a checkpoint consumer, I want structured results stored without lossy string conversion, so that resume and audit preserve the original value.
42. As a Pi user, I want structured output mapped to the selected OpenAI wire protocol correctly, so that Chat Completions and Responses use their native contracts.
43. As a smolagents user, I want structured output enforced through the terminal answer Tool, so that the existing ReAct loop can correct invalid answers.
44. As a contributor, I want the obsolete prompt asset, Markdown Agent definition parser, dependency and tests removed together, so that dead compatibility code does not survive the migration.
45. As a contributor, I want every repository-owned Supervisor and Worker migrated in one change, so that the codebase has one active YAML definition contract.
46. As a contributor, I want one high-level cross-runtime acceptance seam, so that Pi and smolagents prove the same public behavior.
47. As a reviewer, I want tests to assert messages, Tool schemas, results and failure states rather than private helper calls, so that refactoring does not invalidate behavior tests.
48. As a reviewer, I want unsupported Provider and invalid-schema cases to fail without model or Tool side effects, so that preflight guarantees are auditable.
49. As a maintainer, I want the new contract documented in Chinese and English examples, so that copied definitions use the supported design.
50. As a maintainer, I want the destructive migration to have no legacy parser or deprecation mode, so that future work does not carry both architectures.

## Implementation Decisions

### Prompt and task ownership

- Remove the prompt asset directory and every loader, variable expander, required-key list, template constant and import-time dependency used by Prompt Protocol.
- Remove all framework-generated task-spec, workflow, task-request, inputs and output tags, headings, guidance, bridge instructions, indentation rules and fixed output rules.
- Remove Mermaid detection, validation, warning injection and special wrapping from Application prompt assembly. Mermaid source in a configured prompt remains ordinary text.
- Remove the Mermaid parser dependency when no other production consumer remains.
- Remove `workflow` from the definition contract. Require a non-empty `task` string or a non-empty list of non-empty strings in every Supervisor and Worker YAML. A list is an explicit sequence of separate user turns, not one combined prompt or a list of input messages for one model call.
- `system_prompt` is optional. A non-empty string is inline instructions; `{path: <relative file>}` loads a non-empty UTF-8 file relative to the defining YAML. Long-lived prompt files may live under the owning Application's `config/prompts/` directory: a root Agent under `workflows/` uses `../config/prompts/...`, while a Worker under `workflows/worker_agents/` uses `../../config/prompts/...`. Reject other mappings and a missing or unreadable path during preflight. Capture the resolved content and its source in the immutable definition snapshot so resume cannot silently use changed instructions. With no `system_prompt`, do not synthesize one from task or description; runtime defaults and independently configured environment / Skill instructions still apply.
- Application inspection, direct YAML Agent factory calls and Worker loading use the same definition-preparation path. They all resolve and validate `system_prompt.path` before constructing an Agent or Tool; the Runtime receives the resolved text, never the mapping rendered as text. A caller supplying an in-memory definition without a source YAML path may use inline `system_prompt`, but a path-backed prompt requires a source path and fails preflight otherwise.
- Place `system_prompt` in Runtime system / instructions through the existing deterministic composition rule; send each `task` item in the user channel. Do not repeat one in the other or interpolate Tool parameters into either.
- Agent definitions are YAML only. Remove the fenced-YAML-plus-body `.md` definition parser, `.md` discovery and Worker references. A Markdown file may still be the content file named by `system_prompt.path`; it is not itself an Agent definition.
- Remove the public CLI `--task` option and Python Application / Agent task override arguments. CLI, Studio Application execution, Schedule and Python Application execution resolve the same task from the YAML snapshot. A run without a valid YAML task fails preflight before model, Tool or Run allocation. Native pi / smolagents message methods remain internal adapter details.
- Agent `description` remains metadata and never becomes a default task.

```yaml
name: reviewer
agent_runtime: pi
system_prompt:
  path: ../config/prompts/reviewer.md
task:
  - 先检查变更。
  - 根据刚才的检查结果提出修改建议。
```

`system_prompt: |` may instead hold inline instructions; `task: 先检查变更。` is the single-turn form. No `tasks`, `system_prompt_path` or implicit Markdown-body field is added.

同一规则也适用于 Worker。下面的 `file_path` 是 Supervisor 调用 Tool 时提供的数据；Worker 执行的任务仍是 YAML 中的 `task`：

```yaml
name: inspect_note
description: 检查指定文件并返回发现。
task: 读取传入的文件路径，检查内容并报告发现。
input_schema:
  type: object
  properties:
    file_path:
      type: string
  required: [file_path]
  additionalProperties: false
```

不写 `input_schema` 时，Tool 调用参数是空对象。`file_path` 不会变成另一条用户任务，也不会覆盖 YAML `task`。

### Task sequence, Goal and resume

- The Application sends list items in order to one Agent session. It waits for the current item's actual completion before sending the next; the model history, context handling and Agent identity continue across items. A Worker gets a fresh isolated session for each Tool invocation, but its own YAML list shares that session within the invocation.
- The root Agent's configured list is one logical Task. Items in a live execution belong to one Run; after interruption, resume creates a new Run under the same Task ID. Record the item index, its actual output if any, definition fingerprint and runtime checkpoint at each safe boundary. The checkpoint coordinator owns the committed next index and restores it from the same checkpoint as the Runtime state; the invocation layer only requests the next item. Commit completion and the next index before delivering the next user message. Do not resend a committed item or repeat an already settled Tool effect.
- Without Goal mode, an item's ordinary runtime terminal result is its completion boundary. An item failure stops the sequence; later items do not run. Resume from the most recent safe checkpoint and the first uncommitted item, preserving the Agent session. Do not claim replay from an arbitrary historical Step.
- With `goal: true`, each list item is a distinct Goal phase in the same root Supervisor session. An ordinary final reply or `max_steps` does not complete that phase. Only its explicit `update_goal(status="complete")` commit permits the next item. Persist the completed phase, its evidence and the next index before starting the next Goal; allocate a new Goal identity/state for that item without resetting the Agent conversation. Worker Goal mode remains unsupported.
- Goal 完成状态、当前 Runtime checkpoint 与下一任务项序号必须形成同一个可恢复的安全边界；在该边界提交前不能发送下一项。checkpoint coordinator 在恢复时同时读取并校验这三种状态，再向调用层交付可继续的位置；不能让调用层从 Goal 文件单独推断序号。若进程停在跨文件写入的中间状态，恢复时先按已提交事实协调三者，再决定继续当前 Goal 还是创建下一 Goal，不能凭单个文件的状态重发已完成的用户轮次或工具调用。仍处于 active 状态的 Goal 沿用原 Goal ID。
- The existing Supervisor checkpoint is the phase commit marker: one atomic checkpoint write carries the completed Goal identity, Runtime snapshot, item output and next index. Goal state and settled Tool receipts retain their existing stores. On resume, compare those records with the checkpoint before advancing; if the Goal state is newer than its matching checkpoint, a committed item has no restorable Runtime snapshot, or a Tool effect cannot be proved settled, stop with an explicit unsafe-resume error instead of replaying it. A completed Goal alone does not authorize the next item. For an active Goal that has started, the checkpoint must also identify that phase and contain its Runtime snapshot before using a continuation prompt.
- A Goal's evidence is state metadata, not a synthetic final reply. Record each item's actual terminal output separately, including an explicit null when the Runtime ended without a reply; associate it with the item index, terminal status, commit identity and whether a final reply was actually produced. Preserve this presence bit in the checkpoint: an explicit structured JSON `null` is an answer, while no reply is not. Only present an answer when that bit is true. The public Run result is the last item's actual Runtime result in its native JSON-compatible type, or null when no final reply exists; do not concatenate outputs or substitute Goal evidence. Explicit Goal completion permits an item with no reply when it has no `output_schema`; with a schema, validate that item's terminal value before committing or advancing, even if the Runtime reports budget exhaustion after the Goal update. Null is valid only when the schema permits it. Any other Runtime failure still fails the item, regardless of Goal state.
- The durable item-terminal fact must distinguish an attempted end from a committed end. For the root sequence, publish a committed `task_item_end` only after the checkpoint containing its output and next index is durable, and include the checkpoint/commit identity. If the process stops after checkpoint commit but before that fact is written, recovery emits or reconciles the missing fact idempotently; a precommit trace entry never authorizes skipping an item. The Step display uses this committed fact so a failed checkpoint cannot appear as a completed root task item. A Worker list still records and displays each item in its own invocation; its parent ToolCallRecord is the settlement boundary, and this specification does not add a separate cross-Run Worker checkpoint.
- Resume uses the definition snapshot/fingerprint that started the Task. It covers the selected Agent and Worker YAML definitions and referenced prompt contents, but excludes Run outputs and other generated Application files. Changed YAML, referenced prompt content, task order or task text cannot silently alter an in-progress sequence; mismatches fail explicitly. The `--resume` selector stays available but carries no replacement task.

### Subagent registration and input

- `worker_agents` remains the explicit Supervisor allowlist. Directory scanning does not authorize Subagents.
- A referenced Worker automatically becomes a Tool using its top-level `name` and `description`.
- Remove `agent_function_schema` from the supported definition contract and migrate every repository-owned Worker. Do not retain aliases, compatibility parsing or deprecation warnings.
- Every Worker has its own required YAML `task`. `input_schema` is optional and belongs to the Worker definition. When absent, the generated Tool has an empty object argument schema; it does not generate a `task` field.
- When present, `input_schema` uses JSON Schema Draft 2020-12 and must have an object root because model function-call arguments are JSON objects. Nested properties may use string, integer, number, boolean, object or array types.
- Supported schema behavior includes properties, items, required, enum, additional properties and local references. Schema validity is checked before execution.
- Remote references and any reference requiring network retrieval are rejected.
- Model-generated arguments are strictly decoded and validated before a Worker is created. Hook transformations continue to pass through the existing final strict-decoding and Tool governance order.
- Tool arguments are call data, never a task override or additional task item. Preserve their canonical typed object; make it available with the Worker's first YAML task turn, using runtime-native data fields where possible or a deterministic JSON content part when text is required. Do not enqueue the data as another task turn or promote it into system instructions. A property named `task` in an explicit schema is still data; it has no special scheduling or prompt role. Parameter descriptions are not repeated in the Worker prompt.
- Each Tool invocation creates a fresh Worker owner and Runtime. Sharing a model binding does not share conversation history or Runtime state.

### Structured output

- `output_schema` is optional on every Agent, not only Workers. Absence means the final output contract is string.
- `output_schema` uses JSON Schema Draft 2020-12 and may describe any legal JSON root value.
- Add one Runtime-neutral output contract derived from the Agent definition. It contains the validated schema and stable identity needed by adapters; it is part of the immutable Runtime definition rather than a per-call override.
- Add explicit structured-output capability and requirement reporting. An Agent with `output_schema` cannot run on a Runtime or Provider combination that lacks semantic support. Pi requires both its adapter capability and the selected model configuration's `supports_structured_output: true`; parse and retain that field as a boolean rather than silently dropping it. Missing or false means unsupported and fails preflight with a named reason. smolagents uses its validated terminal Tool contract, so it does not require Pi's Provider-native response-format flag.
- Provider or Runtime incompatibility fails during preparation, before model or Tool side effects. There is no prompt-based JSON fallback and no silent string fallback.
- Validation occurs at the Agent terminal-answer boundary, before the Runtime reports success. Post-run validation is insufficient because the Worker session has already closed.
- Schema validation errors remain inside the same Agent session as actionable model feedback. There is no output-specific retry counter; correction consumes the Agent's existing execution steps.
- Exhausting the existing execution budget with an invalid result fails the run. For a Subagent Tool, the failure is represented as an output-validation Tool error.
- Transport/provider retry policies remain limited to retryable transport failures and do not handle schema mismatch.
- Successful structured output remains a raw JSON-compatible value through Runtime result, ToolCallRecord, checkpoint, lifecycle and public Application result.

### Runtime adapters

- smolagents enforces the output contract through the terminal answer Tool. Its answer parameter reflects the Agent output schema, host validation uses a complete Draft 2020-12 validator, and a successful call returns the raw JSON-compatible answer.
- Invalid smolagents terminal answers remain recoverable within the current ReAct loop and consume the existing step budget.
- Pi extends its Runtime wire contract with the optional output contract.
- Pi maps the contract to the native Chat Completions structured response format or Responses text format according to the selected model protocol. It parses and validates the final value with its existing Draft 2020-12 validator stack.
- Pi schema correction continues the same session and prevents repeated side-effecting Tool execution during final correction.
- The Studio chat implementation is not an Application Runtime and is not modified by this feature.

### Context, compression and records

- Supervisor and each Worker keep independent model sessions and context windows.
- They inherit the same Application task-scoped Context Engine and ContextRef store through the existing execution context.
- The canonical structured output is stored before projection or compression.
- Tool model content serializes non-string JSON deterministically. A string remains ordinary text.
- The model-visible Tool result may be compressed into a ContextRef using the existing Context Engine only when the receiving Agent has the retrieval capability. Otherwise the complete projection is delivered.
- A ContextRef never replaces the canonical ToolCallRecord, Runtime result, checkpoint or public result.
- Do not add a 256 KiB output-contract limit. Existing context budgets, reversible compression and infrastructure frame protections remain responsible for presentation and transport safety.
- Structured results are not compressed into a string before output validation and are not passed through text-only result compression as canonical values.

### Public result and CLI

- Application results and completed lifecycle events use the Runtime's JSON-compatible result type and carry the final-reply presence bit. For a task list they expose the last item's actual result, with prior item results retained in the execution record rather than aggregated into a synthetic list. The lifecycle event and JSON/JSONL projection distinguish explicit JSON `null` from no reply even though both have a null `output` value.
- Python execution APIs preserve dictionaries, lists, strings, numbers, booleans and null as their native JSON-compatible values; their Application entry points do not accept a task override.
- CLI text mode prints strings directly and renders non-string output as indented, readable JSON.
- CLI JSON and JSONL modes place the native value in `output` without double encoding.
- Each committed task item and its actual final reply, if any, appears in the existing Step presentation and durable record. Failed terminal attempts remain inspectable with their failure state, but are not printed as successful final answers. Lifecycle events, the public result and durable records describe the same last result; no Goal evidence is substituted for absent output.
- For a root sequence, after an item's checkpoint commits and before the next item starts, emit or reconcile its committed terminal record and print its actual reply once through the shared Step presentation. Persist that record in the existing Run trace with its commit identity; the final Application result remains the last item only. A Worker list also displays and records each item before advancing within its Tool invocation, while the parent ToolCallRecord settles the complete Worker result. Recording only the final Application answer loses the earlier replies and does not satisfy this contract.

### Migration and documentation

- Migrate every repository Agent definition to YAML with required `task` and optional `system_prompt`; remove `workflow` and all runtime task override entry points. Keep previously established typed `input_schema` / `output_schema` contracts.
- Keep both inline and path-backed prompts in repository-owned examples. Move representative longer Supervisor, Worker and single-Agent instructions into their owning Application's `config/prompts/` directory without changing the resolved prompt text or message role; retain concise inline examples to show both supported forms.
- Remove the implicit default Worker Tool `task` parameter; an absent `input_schema` means an empty object argument schema. Existing Workers that genuinely need per-call data declare it explicitly as `input_schema` properties, without changing their YAML task.
- Workers whose former schema only described a textual output omit `output_schema`.
- Workers requiring actual structured values receive executable schemas matching their observed consumers.
- Move old system-style `workflow` text to inline or file-backed `system_prompt`; move actual user requests to `task`. Old `workflow` lists are reviewed item by item: instructions become system text, sequential requests become a `task` list. Do not mechanically concatenate a former list into one prompt.
- Review every migrated YAML by message role. Long-lived identity, policy and working method belong in `system_prompt`; a concrete action belongs in `task`. In particular, a `task` that begins with a role declaration such as “You are ...” needs manual review rather than an automatic `workflow`-to-`task` rename.
- Migrate `applications/architecture_contract_validation/workflows/worker_agents/change_planner.md` to YAML and update its Supervisor reference; remove the Markdown Agent definition parser, discovery, Worker reference and Studio branches. Plain Markdown prompt files referenced by `system_prompt.path` remain supported.
- Migrate CLI examples, Python consumers, Studio Application execution and schedules away from runtime task overrides. `--resume` uses the captured YAML definition and the recorded task index.
- Update Chinese and English configuration documentation, examples, scaffold guidance, framework Skill references and validation messages.
- Remove packaging expectations for the deleted prompt resource.
- Keep the first-party multi-Agent and [task-sequence research](../research/agent-prompt-sequence-patterns.md) as design provenance; implementation follows this revised specification when earlier exploratory notes conflict.

## Testing Decisions

- The primary, highest test seam is complete Application execution. One canonical multi-Agent fixture is executed through both Pi and smolagents and observed through the public Application result and lifecycle events.
- The primary acceptance fixture contains a Supervisor and at least one explicitly selected Worker. Both have YAML `task`; it covers a zero-argument Worker, typed call data, default text output and structured object/array output.
- Tests assert externally visible Runtime instructions and user-task separation where the adapter exposes captured requests. They do not assert private prompt-building helper calls.
- A good test proves that inline and path-backed `system_prompt` reach system/instructions, each YAML `task` item reaches the user channel in order, Tool data follows the declared schema without overriding Worker task, the Worker result is validated, and the Supervisor receives the correct Tool result.
- The same high-level scenario must prove that no task-spec, inputs, output or workflow wrapper tags and no fixed bridge instructions remain in model-visible content.
- Definition tests cover required non-empty string/list tasks on both Agent roles; optional inline/path-backed system prompt, including root and Worker paths into their Application's `config/prompts/` directory; invalid, missing or changed referenced files; rejection of `workflow`, `tasks`, `.md` Agent definitions and runtime task overrides; and absence of fabricated description tasks.
- Exercise both Application inspection and direct YAML Agent-as-Tool creation with a path-backed prompt; both must send the same resolved instructions, and an unanchored in-memory path must fail before Agent creation.
- Sequence tests cover one Run with distinct ordered user turns and shared Agent history in both runtimes, including a Worker YAML list within one Tool invocation. They assert each item is displayed and recorded, only the last actual result is returned, and no reply is synthesized when absent.
- Goal sequence tests prove each item needs its own explicit `update_goal` completion, ordinary final replies cannot advance, Goal identities/evidence do not leak into the next item, and the same Agent conversation persists. A completed Goal with no reply preserves null without printing an answer; a non-nullable output schema rejects that terminal item. An Agent that explicitly returns schema-valid JSON `null` records and displays that answer, including after resume.
- Recovery tests stop between items and within an item, then resume from the last safe checkpoint under a new Run ID without resending committed items or Tool effects. Inject failure after each constituent Goal/checkpoint/sequence/trace write to prove partial writes cannot authorize the next item, regress the restored next index, create a false committed item record or cause unsafe replay. A changed YAML task, item order or prompt file fails explicitly; a failed item prevents later items from starting.
- Schema tests cover all supported primitive and composite property types, object-root input enforcement, arbitrary output roots, required fields, enums, array items, additional-property behavior, local references, invalid schemas and rejected remote references.
- Tool registration tests cover explicit `worker_agents`, no directory auto-registration, reused Agent name/description, missing Worker, duplicate Tool name and fresh Worker isolation.
- Input tests cover zero-argument Worker calls, typed multi-field JSON data, missing required fields, unexpected fields, Hook-transformed arguments and strict post-Hook validation.
- Output tests cover default text, valid structured values, invalid root type, missing fields, invalid nested values, local-reference validation and null where permitted.
- Correction tests use deterministic model fixtures to emit an invalid terminal value followed by a valid value. They verify continuation in the same session, no schema-specific retry counter and termination through the existing execution budget.
- Exhaustion tests verify that an invalid final value cannot become success and that Subagent failure becomes an output-validation Tool record visible to the Supervisor.
- Capability tests verify preflight rejection for unsupported or undeclared Runtime/Provider combinations, including a Pi model whose Provider does not support structured output, and prove that no model request or Tool side effect occurred.
- smolagents adapter tests verify the terminal answer Tool schema, raw JSON-compatible result and recoverable validation feedback.
- Pi protocol tests verify both Chat Completions and Responses wire projections, final parsing, host validation, same-session correction and Tool suppression during correction.
- Context Engine tests verify that large structured Worker output is stored canonically, projected to ContextRef only for a retriever-capable Supervisor, retrievable without loss, and delivered in full when retrieval is unavailable.
- Checkpoint tests verify that item index, Agent session state, Goal phase and structured values retain their types, and that resume does not replace them with serialized or compressed display text.
- Public API tests verify native JSON-compatible results for direct Agents and Subagent Tools.
- CLI tests verify readable text-mode JSON and native JSON/JSONL event output without double encoding.
- Lifecycle tests verify that completed events carry the same value as the public result.
- Packaging tests verify that deleted prompt and Mermaid resources are absent and that installed Pi and smol profiles still run the same definition contract.
- Migration inventory tests verify that active definitions, documentation and code no longer accept `workflow`, Markdown Agent definitions, CLI/Python task overrides, the implicit Worker `task` parameter, `agent_function_schema` or Prompt Protocol tags.
- Existing Agent-as-Tool isolation, Tool Gateway output-validation, Application lifecycle, CLI observability, Context Engine, Pi protocol, smolagents Runtime and installation-profile tests are the prior art to extend.
- Full repository tests and installation-profile validation run after focused contracts pass. Retire tests for removed definition and task APIs, and consolidate duplicate assertions until collection is at most 2,000 cases. Keep representative cross-runtime Application, security, recovery, schema, unsupported-capability, MCP partial-failure/disconnect and shell audit/concurrent-write coverage; do not use skips to hide failures or remove a unique behavior contract merely to meet the count.
- Use ordinary tests and direct Runtime runs to verify the contract; do not invoke the repository's evaluation tools or evaluation Applications.

## Out of Scope

- Changing Studio's independent chat protocol or adding structured output to Studio.
- Sharing one model conversation or context window between Supervisor and Workers.
- Automatically registering every Worker found in a directory.
- Keeping `agent_function_schema`, Prompt Protocol, `workflow`, Markdown Agent definitions or runtime task override compatibility modes.
- Remote JSON Schema reference resolution.
- A second output-specific retry counter.
- Prompt-only JSON enforcement or silent degradation on unsupported Providers.
- Replacing the existing Context Engine, ContextRef store or context-budget algorithms.
- Adding an arbitrary business-level maximum size for structured output.
- Cross-Runtime checkpoint conversion.
- Introducing a new handoff protocol distinct from Subagent-as-Tool.
- Broad Provider or Agent Runtime upgrades unrelated to the required structured-output capability.
- Guaranteeing resume of an in-flight checkpoint created under the removed prompt-assembly semantics. Historical evidence remains inspectable under existing storage rules.
- Adding interactive task editing or a second `tasks` configuration field. The configured sequence is fixed by the YAML snapshot for its Task.

## Further Notes

- The structured-output and Tool contract was compared against first-party sources for OpenAI Agents SDK, LangGraph, Microsoft AutoGen, CrewAI, Google ADK and PydanticAI. Task-sequence research also checked smolagents and pi: they add subsequent user turns through the same session, but do not provide AgentLoom's YAML string/list scheduling contract. That sequencing belongs to the Application layer.
- CrewAI demonstrates that formatted task prose can work, but it does not justify a framework-wide XML Prompt Protocol or mixing Agent identity, Tool schema and output contract into one string.
- AgentLoom already has the correct high-level ownership seams: Application definition, AgentRuntime, Tool Gateway, ToolCallRecord, fresh Worker invocation, Runtime result, Context Engine and lifecycle receipts. This work reconnects those seams instead of creating another prompt abstraction.
- The Hook Runtime ADR remains authoritative. Final Tool arguments still pass configured transformations, strict decoding, CoreToolGuard and audit in the established order.
- The migration is deliberately destructive at the definition level. Repository-owned definitions move together, and old fields are rejected rather than interpreted.
- This document defines the target contract. Implementation status is established by code review and verification results, not inferred from this specification.
