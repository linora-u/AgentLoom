# 移除 Prompt Protocol，建立原生 Agent Prompt 与结构化 Subagent 契约

规格日期：2026-09-22。

## Problem Statement

作为 AgentLoom 的维护者，用户希望 `workflow` 回到它最直接的含义：Agent 的固定 prompt，也就是 system instructions。本轮任务和 Subagent 调用参数应该通过独立的 user / Tool 通道传递，而不是由 Application 层重新拼成一套自定义文本协议。

当前实现会在模块导入时读取一份固定 YAML，并把 `workflow`、本轮任务、Tool 参数说明、参数值、输出说明、Mermaid 内容和固定指导语包装成 `<task_spec>`、`<workflow>`、`<task_request>`、`<inputs>`、`<output>` 等文本。这套 Prompt Protocol 重复表达了 Runtime 已经拥有的 system、user、Tool schema 和 Tool result 通道，增加了 token、配置、依赖、测试和维护成本。删除 YAML 文件本身还不够；如果保留加载器、模板常量、XML 标签或改成另一份更短的文本模板，问题仍然存在。

Worker 的 `agent_function_schema` 也混合了多个职责。输入参数虽有 schema 外形，但全部会被强制归一成字符串；`output.description` 只是被拼进 prompt 和函数文档，没有形成可执行的输出契约。`workflow: list[str]` 还会暗中改变运行次数，使“prompt 的 YAML 形状”承担编排语义。直接运行 Application 且没有 `--task` 时，框架又会把 Agent 的 `description` 当成本轮 user task，混淆元数据与输入。

用户希望彻底删除旧协议和兼容层，同时保留并简化多 Agent 能力：Supervisor 显式选择允许调用的 Subagent；Subagent 自动注册成 Tool；简单 Subagent 默认接收一个字符串任务并返回文本；复杂 Subagent 可以声明标准 JSON Schema 输入和输出，并由 Runtime 真正约束、校验和纠正结构化结果。

## Solution

删除整套 Prompt Protocol，让 AgentLoom 使用 Runtime 原生语义：

- `workflow` 是单个字符串，进入 Agent 的 system / instructions 通道。
- 本轮 task 是可选的独立 user message；没有 task 时不伪造消息。
- `description` 只描述 Agent 的能力，用于展示和 Subagent Tool 描述。
- Supervisor 继续通过 `worker_agents` 显式列出允许注册的 Subagent。
- Subagent 不再配置 `agent_function_schema` 或额外的 `tool` 块。框架直接复用 Subagent 的 `name` 和 `description` 生成 Tool。
- 未配置 `input_schema` 时，Subagent Tool 默认暴露一个必填的 `task: string` 参数。
- 配置 `input_schema` 时，使用 JSON Schema Draft 2020-12 描述 Tool 参数。Tool 调用参数经过严格校验后，作为独立 user input 交给 Subagent。
- 未配置 `output_schema` 时，Agent 默认返回普通文本。
- 配置 `output_schema` 时，Runtime 在 Agent 的最终输出阶段执行真实结构化约束和本地校验。校验失败不能离开 Agent，而是进入同一会话的下一步纠正，统一受 Agent 既有执行预算约束。
- canonical Runtime、Tool 和 Application 结果保留原始 JSON 类型。只有投影给模型或终端展示时才序列化；大结果继续使用现有 Context Engine 和 ContextRef。
- Pi 和 smolagents 分别在自己的适配层实现结构化输出，不支持该能力的 Runtime 或 Provider 在执行前明确失败，不退化成 prompt 技巧。

## User Stories

1. As an Application author, I want `workflow` to mean the Agent's system instructions, so that I can predict where my prompt is sent.
2. As an Application author, I want the current task to remain separate from `workflow`, so that reusable Agent behavior is not duplicated on every invocation.
3. As an Application author, I want an Application without a task override to run without a fabricated user message, so that metadata is never mistaken for user intent.
4. As an Application author, I want `description` to remain Agent metadata, so that it can describe capability without changing execution input.
5. As an Application author, I want to author ordinary Markdown and Mermaid text inside `workflow` without framework-specific extraction, so that the model receives exactly the instructions I wrote.
6. As an Application author, I want one string form for `workflow`, so that YAML shape cannot secretly change the number of Agent runs.
7. As an Application author, I want multi-stage behavior expressed explicitly in Agent instructions or orchestration, so that execution control is visible rather than encoded in a list.
8. As a Supervisor author, I want to list allowed Subagents explicitly with `worker_agents`, so that adding a file to a directory cannot silently expand Agent authority.
9. As a Supervisor author, I want each selected Subagent automatically exposed as a Tool, so that I do not maintain duplicate wrapper code.
10. As a Supervisor author, I want the Subagent's `name` to become the Tool name, so that execution traces and configuration use one identity.
11. As a Supervisor author, I want the Subagent's `description` to become the Tool description, so that capability text is written once.
12. As a Subagent author, I want a simple Agent to work without schema configuration, so that basic delegation stays concise.
13. As a Subagent author, I want an omitted `input_schema` to produce a conventional `task: string` Tool argument, so that simple Agents have an obvious default.
14. As a Subagent author, I want an omitted `output_schema` to produce ordinary text, so that structured output remains optional.
15. As a Subagent author, I want to declare a standard JSON Schema for complex input, so that integers, numbers, booleans, objects and arrays are not coerced into strings.
16. As a Subagent author, I want to declare a standard JSON Schema for output, so that downstream Agents receive validated data rather than prose promises.
17. As a Subagent author, I want output schemas to allow any legal JSON root type, so that lists and primitive results do not need meaningless wrapper objects.
18. As a Subagent author, I want local schema references to work, so that repeated structures can be defined once.
19. As a security-conscious operator, I want remote schema references rejected, so that validation never performs unapproved network resolution.
20. As a Supervisor, I want Tool arguments validated before Subagent execution, so that malformed model calls do not start partial work.
21. As a Supervisor, I want a single string Tool argument delivered as a normal user message, so that the Subagent sees natural task text.
22. As a Supervisor, I want multi-field Tool arguments preserved as typed JSON, so that the Subagent does not reverse-engineer numbered prose.
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
37. As a Python API consumer, I want structured Application results returned as native JSON-compatible values, so that types survive the full execution path.
38. As a CLI user, I want text mode to render structured results as readable JSON, so that terminal output remains understandable.
39. As an automation author, I want JSON and JSONL CLI modes to retain native JSON values, so that output is not double encoded.
40. As an observability consumer, I want lifecycle events to carry the same native result as the public API, so that logs and return values cannot disagree.
41. As a checkpoint consumer, I want structured results stored without lossy string conversion, so that resume and audit preserve the original value.
42. As a Pi user, I want structured output mapped to the selected OpenAI wire protocol correctly, so that Chat Completions and Responses use their native contracts.
43. As a smolagents user, I want structured output enforced through the terminal answer Tool, so that the existing ReAct loop can correct invalid answers.
44. As a contributor, I want the obsolete prompt asset, parser, dependency and tests removed together, so that dead compatibility code does not survive the migration.
45. As a contributor, I want every repository-owned Worker migrated in one change, so that the codebase has one active definition contract.
46. As a contributor, I want one high-level cross-runtime acceptance seam, so that Pi and smolagents prove the same public behavior.
47. As a reviewer, I want tests to assert messages, Tool schemas, results and failure states rather than private helper calls, so that refactoring does not invalidate behavior tests.
48. As a reviewer, I want unsupported Provider and invalid-schema cases to fail without model or Tool side effects, so that preflight guarantees are auditable.
49. As a maintainer, I want the new contract documented in Chinese and English examples, so that copied definitions use the supported design.
50. As a maintainer, I want the destructive migration to have no legacy parser or deprecation mode, so that future work does not carry both architectures.

## Implementation Decisions

### Prompt and task ownership

- Remove the prompt asset directory and every loader, variable expander, required-key list, template constant and import-time dependency used by Prompt Protocol.
- Remove all framework-generated task-spec, workflow, task-request, inputs and output tags, headings, guidance, bridge instructions, indentation rules and fixed output rules.
- Remove Mermaid detection, validation, warning injection and special wrapping from Application prompt assembly. Mermaid source inside `workflow` remains ordinary instruction text.
- Remove the Mermaid parser dependency when no other production consumer remains.
- `workflow` becomes one required non-empty string. Definitions using a list are migrated to a single multiline string; list validation and sequential-run behavior are deleted.
- `workflow` is placed in Runtime instructions together with existing environment and Skill instructions through a deterministic composition rule. It is not repeated in the user task.
- The invocation task becomes optional. A supplied task becomes a separate user message; an absent task produces no invented message.
- Agent `description` remains metadata and never becomes a default task.

### Subagent registration and input

- `worker_agents` remains the explicit Supervisor allowlist. Directory scanning does not authorize Subagents.
- A referenced Worker automatically becomes a Tool using its top-level `name` and `description`.
- Remove `agent_function_schema` from the supported definition contract and migrate every repository-owned Worker. Do not retain aliases, compatibility parsing or deprecation warnings.
- `input_schema` is optional and belongs to the Agent definition. When absent on a referenced Subagent, the generated Tool uses one required string property named `task`.
- When present, `input_schema` uses JSON Schema Draft 2020-12 and must have an object root because model function-call arguments are JSON objects. Nested properties may use string, integer, number, boolean, object or array types.
- Supported schema behavior includes properties, items, required, enum, additional properties and local references. Schema validity is checked before execution.
- Remote references and any reference requiring network retrieval are rejected.
- Model-generated arguments are strictly decoded and validated before a Worker is created. Hook transformations continue to pass through the existing final strict-decoding and Tool governance order.
- A default single `task` value becomes the Worker user message. Explicit multi-field input remains a typed canonical object and is projected as a bare JSON user input only where the Runtime requires textual user content. Parameter descriptions are not repeated in the Worker prompt.
- Each Tool invocation creates a fresh Worker owner and Runtime. Sharing a model binding does not share conversation history or Runtime state.

### Structured output

- `output_schema` is optional on every Agent, not only Workers. Absence means the final output contract is string.
- `output_schema` uses JSON Schema Draft 2020-12 and may describe any legal JSON root value.
- Add one Runtime-neutral output contract derived from the Agent definition. It contains the validated schema and stable identity needed by adapters; it is part of the immutable Runtime definition rather than a per-call override.
- Add explicit structured-output capability and requirement reporting. An Agent with `output_schema` cannot run on a Runtime or Provider combination that lacks semantic support.
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

- Widen Application results and completed lifecycle events from string-only output to the Runtime's JSON-compatible result type.
- Python execution APIs preserve dictionaries, lists, strings, numbers, booleans and null as their native JSON-compatible values.
- CLI text mode prints strings directly and renders non-string output as indented, readable JSON.
- CLI JSON and JSONL modes place the native value in `output` without double encoding.
- Lifecycle events, the public result and durable records describe the same result value.

### Migration and documentation

- Migrate every repository Worker from `agent_function_schema` to top-level optional `input_schema` and `output_schema`.
- Workers whose former schema only described a textual output omit `output_schema`.
- Workers requiring actual structured values receive executable schemas matching their observed consumers.
- Migrate the two list-form workflows to single multiline instruction strings and delete all list-specific docs and tests.
- Update Chinese and English configuration documentation, examples, scaffold guidance, framework Skill references and validation messages.
- Remove packaging expectations for the deleted prompt resource.
- Keep the first-party multi-Agent research conclusions as design provenance; implementation follows the decisions in this specification when earlier exploratory notes conflict.

## Testing Decisions

- The primary, highest test seam is complete Application execution. One canonical multi-Agent fixture is executed through both Pi and smolagents and observed through the public Application result and lifecycle events.
- The primary acceptance fixture contains a Supervisor and at least one explicitly selected Subagent. It covers default `task: string`, typed multi-field input, default text output and structured object/array output.
- Tests assert externally visible Runtime instructions and user-task separation where the adapter exposes captured requests. They do not assert private prompt-building helper calls.
- A good test proves that `workflow` reaches system/instructions, the task reaches the user channel, Tool arguments follow the declared schema, the Worker result is validated, and the Supervisor receives the correct Tool result.
- The same high-level scenario must prove that no task-spec, inputs, output or workflow wrapper tags and no fixed bridge instructions remain in model-visible content.
- Definition tests cover a required string workflow, rejection of list workflow, optional task, absence of fabricated description tasks, default Subagent schemas and explicit schemas.
- Schema tests cover all supported primitive and composite property types, object-root input enforcement, arbitrary output roots, required fields, enums, array items, additional-property behavior, local references, invalid schemas and rejected remote references.
- Tool registration tests cover explicit `worker_agents`, no directory auto-registration, reused Agent name/description, missing Worker, duplicate Tool name and fresh Worker isolation.
- Input tests cover direct string tasks, typed multi-field JSON, missing required fields, unexpected fields, Hook-transformed arguments and strict post-Hook validation.
- Output tests cover default text, valid structured values, invalid root type, missing fields, invalid nested values, local-reference validation and null where permitted.
- Correction tests use deterministic model fixtures to emit an invalid terminal value followed by a valid value. They verify continuation in the same session, no schema-specific retry counter and termination through the existing execution budget.
- Exhaustion tests verify that an invalid final value cannot become success and that Subagent failure becomes an output-validation Tool record visible to the Supervisor.
- Capability tests verify preflight rejection for unsupported Runtime/Provider combinations and prove that no model request or Tool side effect occurred.
- smolagents adapter tests verify the terminal answer Tool schema, raw JSON-compatible result and recoverable validation feedback.
- Pi protocol tests verify both Chat Completions and Responses wire projections, final parsing, host validation, same-session correction and Tool suppression during correction.
- Context Engine tests verify that large structured Worker output is stored canonically, projected to ContextRef only for a retriever-capable Supervisor, retrievable without loss, and delivered in full when retrieval is unavailable.
- Checkpoint tests verify that structured values retain type and that resume does not replace them with serialized or compressed display text.
- Public API tests verify native JSON-compatible results for direct Agents and Subagent Tools.
- CLI tests verify readable text-mode JSON and native JSON/JSONL event output without double encoding.
- Lifecycle tests verify that completed events carry the same value as the public result.
- Packaging tests verify that deleted prompt and Mermaid resources are absent and that installed Pi and smol profiles still run the same definition contract.
- Migration inventory tests verify that repository definitions, documentation and active code no longer contain `agent_function_schema`, Prompt Protocol tags or list-form workflows.
- Existing Agent-as-Tool isolation, Tool Gateway output-validation, Application lifecycle, CLI observability, Context Engine, Pi protocol, smolagents Runtime and installation-profile tests are the prior art to extend.
- Full repository tests and installation-profile validation run after focused contracts pass. Test collection cannot be reduced and skips cannot replace unsupported-path assertions.

## Out of Scope

- Changing Studio's independent chat protocol or adding structured output to Studio.
- Sharing one model conversation or context window between Supervisor and Workers.
- Automatically registering every Worker found in a directory.
- Keeping `agent_function_schema`, Prompt Protocol or list-workflow compatibility modes.
- Remote JSON Schema reference resolution.
- A second output-specific retry counter.
- Prompt-only JSON enforcement or silent degradation on unsupported Providers.
- Replacing the existing Context Engine, ContextRef store or context-budget algorithms.
- Adding an arbitrary business-level maximum size for structured output.
- Cross-Runtime checkpoint conversion.
- Introducing a new handoff protocol distinct from Subagent-as-Tool.
- Broad Provider or Agent Runtime upgrades unrelated to the required structured-output capability.
- Guaranteeing resume of an in-flight checkpoint created under the removed prompt-assembly semantics. Historical evidence remains inspectable under existing storage rules.

## Further Notes

- The design was compared against current first-party sources for OpenAI Agents SDK, LangGraph, Microsoft AutoGen, CrewAI, Google ADK and PydanticAI. Their common boundary is separate Agent instructions, Tool metadata/schema, per-run input and executable output type/schema.
- CrewAI demonstrates that formatted task prose can work, but it does not justify a framework-wide XML Prompt Protocol or mixing Agent identity, Tool schema and output contract into one string.
- AgentLoom already has the correct high-level ownership seams: Application definition, AgentRuntime, Tool Gateway, ToolCallRecord, fresh Worker invocation, Runtime result, Context Engine and lifecycle receipts. This work reconnects those seams instead of creating another prompt abstraction.
- The Hook Runtime ADR remains authoritative. Final Tool arguments still pass configured transformations, strict decoding, CoreToolGuard and audit in the established order.
- The migration is deliberately destructive at the definition level. Repository-owned definitions move together, and old fields are rejected rather than interpreted.
- The implementation is ready to begin when this specification is published with the `ready-for-agent` label.
