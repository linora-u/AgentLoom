# AgentLoom Pluggable Agent Runtime and Model Protocol Spec

## Problem Statement

AgentLoom 当前把具体的 smolagents 实现当成主运行时接口。Application 调用链、Agent 构造、Worker 调用、checkpoint、context compression 和错误恢复会直接接触 smolagents 的 `RunResult`、`ActionStep`、`ChatMessage`、`memory.steps` 与 `reset` 语义。代码虽然已经把部分上游集成放进 smolagents adapter 目录，但仅靠目录归类并没有形成可替换的 runtime seam。

用户希望 AgentLoom 可以通过一个明确的 runtime adapter 切换不同 Agent 基座，而不要求 Application、CLI、Hook、权限、Tool、checkpoint 和结果消费方了解底层框架。目标架构用 smolagents 和 LangGraph 两个真实 adapter 证明这个 seam 不是只为 smolagents 改名的假抽象；当前开发阶段先完成 smolagents adapter，LangGraph 在第一阶段全部验收通过后进入第二阶段。

模型 wire protocol 是另一个独立变化维度。AgentLoom 同时需要支持 OpenAI-compatible Chat Completions、OpenAI Responses 和 Anthropic Messages，但切换模型协议不应更换 Agent runtime，切换 Agent runtime 也不应重写模型配置。当前 `code_act`、文本工具调用猜测和 code execution 配置会扩大两层耦合，应从新的正式运行路径中移除。

## Solution

AgentLoom 建立两个正交 seam：

1. 外层 `AgentRuntime` seam 负责一次完整 Agent invocation。它接收 runtime-neutral Agent 定义、任务、运行上下文、Tool Gateway、模型入口和可选恢复状态，产出标准化事件、结果及 runtime checkpoint。当前只注册 `smolagents`；`langgraph` 是第二阶段的真实 adapter。
2. 内层 `ModelTurnAdapter` seam 负责一次模型调用。它把 AgentLoom 模型交互项投影到 `openai_chat`、`openai_responses` 或 `anthropic_messages`，再把响应归一回 AgentLoom 模型交互项。

smolagents 不再是 AgentLoom 的永久 superclass 或公共类型系统，而是第一个 runtime adapter。因为 `agent_runtime` 必填，所以不存在隐式默认 runtime。LangGraph 是第二阶段用于验证 seam 的真实 runtime adapter；在它实现前，配置 `agent_runtime: langgraph` 必须明确失败，不能回退到 smolagents。

AgentLoom 永久拥有 Application 定义、Supervisor/Worker 拓扑、Run 和 task identity、Hook Plan/Hook Run、Tool Gateway、权限和路径验证、`ToolCallRecord`、Goal、Todo、runtime-neutral 审计事件、checkpoint envelope 和 `ApplicationRunResult`。任何 runtime adapter 的工具调用都必须经过 AgentLoom Tool Gateway，不能直接执行第三方 runtime 的原生工具实现。

runtime adapter 拥有其内部 Agent loop、conversation/session state、loop cursor、底层 step 或 graph node、pending interrupt、runtime-specific handoff 状态及无损恢复所需 payload。AgentLoom 不尝试把 smolagents `ActionStep` 与 LangGraph graph state 强制改造成同一种内部 step。模型历史则统一为 AgentLoom canonical items；在当前 smolagents adapter 中，这些 items 是模型回放的唯一真相，不能再从 observation 文本反向拼出下一轮历史。

Agent YAML 使用必填 `agent_runtime` 字段选择基座，避免和现有全局 `runtime` 存储配置冲突；第一版合法值为 `smolagents`，`langgraph` 留到第二阶段实现。模型目录中的每个可用 model type 使用必填 `adapter` 字段选择 wire protocol，第一版合法值为 `openai_chat`、`openai_responses` 和 `anthropic_messages`。`model` 是传给 LiteLLM 的不透明模型名，不通过其前缀推断 Agent runtime 或模型协议。

主运行时只支持结构化 tool call。没有 `code_act` 模式，也没有用户可选的 `tool_call_type`。`execution_env` 和 `code_agent` 不属于正式主运行时配置面。实现中不为这些旧名字建立迁移分支、warning 或专用兼容逻辑；它们不会进入 effective runtime configuration，也不会产生任何行为。模型必须返回原生结构化工具调用；运行时不从 prose、XML 或 JSON 文本中猜测工具调用。

## User Stories

1. As an Application author, I want to select an Agent runtime explicitly with `agent_runtime`, so that the same Application contract can run on different maintained bases without conflicting with the global runtime-storage configuration.
2. As an Application author, I want `agent_runtime: smolagents` to preserve the established AgentLoom execution behavior, so that extracting the seam does not silently change existing runs.
3. As an Application author, I want the future `agent_runtime: langgraph` implementation to run through the same Application and CLI interfaces, so that adopting LangGraph does not require a second product surface.
4. As an Application author, I want runtime selection and model protocol selection to be independent, so that I can change one without rewriting the other.
5. As an Application author, I want every model type to declare its wire adapter explicitly, so that protocol selection never depends on guessed model names.
6. As an Application author, I want summary models to follow the same explicit adapter contract, so that background model calls do not use a hidden default.
7. As an Application author, I want all Agents to use structured tool calls, so that workflows have one observable execution model.
8. As an Application author, I want configuration that is not part of the current contract to have no runtime effect, so that obsolete implementation details do not create hidden branches.
9. As an Application author, I want unsupported runtime capabilities rejected before execution, so that a workflow is not silently degraded.
10. As an Application author, I want a Supervisor and its Workers to declare runtimes independently, so that mixed runtime topologies have a valid future configuration seam.
11. As a CLI user, I want `loom run` and existing Application entry points to remain stable, so that runtime selection stays behind the AgentRuntime interface.
12. As a CLI user, I want failures classified consistently across runtimes, so that I do not need to understand framework-specific exceptions.
13. As a maintainer, I want a small complete-run AgentRuntime interface, so that runtime-specific step models do not leak into Application code.
14. As a maintainer, I want the interface to avoid a public `step()` method, so that callers do not need to understand ReAct steps, graph supersteps, handoffs, or suspended nodes.
15. As a maintainer, I want smolagents construction, `RunResult`, `ActionStep`, `ChatMessage`, callbacks and reset semantics contained inside its adapter, so that removing the adapter does not scatter those concepts across AgentLoom.
16. As a maintainer, I want LangGraph state, nodes, commands and checkpointer objects contained inside its adapter, so that graph implementation choices remain local.
17. As a maintainer, I want the current smolagents adapter and future LangGraph adapter to return the same AgentLoom run result, so that callers are independent of the selected base.
18. As a maintainer, I want the current smolagents adapter and future LangGraph adapter to emit the same minimum runtime event vocabulary, so that logging, audit and observability remain stable.
19. As a maintainer, I want AgentLoom to own tool authorization and side-effect governance, so that changing runtime cannot bypass Hook or permission rules.
20. As a maintainer, I want every tool invocation to retain a stable call ID and a canonical `ToolCallRecord`, so that audit, replay and error handling remain correlated.
21. As a maintainer, I want `final_answer` to retain its terminal semantics across runtimes, so that output checks and Goal settlement remain consistent.
22. As a maintainer, I want AgentLoom to own subagent lifecycle events, so that Worker observability does not depend on a vendor-specific handoff representation.
23. As a maintainer, I want runtime capabilities to be explicit and small, so that unsupported topology or checkpoint behavior fails rather than silently changing semantics.
24. As a maintainer, I want the first capability set limited to structured tools, parallel tools, checkpoint/resume and subagents, so that the interface does not become a bag of framework-specific flags.
25. As a maintainer, I want model protocol adapters to consume and produce AgentLoom-owned interaction items, so that provider structures do not leak into runtime-neutral code.
26. As a maintainer, I want `message`, `function_call`, `function_call_output` and `reasoning` represented distinctly, so that Responses and Anthropic thinking/tool semantics can be preserved.
27. As a maintainer, I want reasoning replay payloads retained without exposing them as user-visible final text, so that provider requirements and privacy are both respected.
28. As a maintainer, I want unknown executable model items rejected, so that the model cannot wait forever for an action AgentLoom silently ignored.
29. As a maintainer, I want model protocol failures surfaced without cross-adapter fallback, so that selected wire contracts remain honest.
30. As a maintainer, I want LiteLLM used as transport and provider mapping rather than canonical state, so that LiteLLM implementation details do not own AgentLoom replay.
31. As a maintainer, I want the smolagents adapter to support the three model protocols through the shared ModelTurnAdapter, so that protocol support is not duplicated in the runtime owner.
32. As a maintainer, I want the LangGraph adapter to call the same ModelTurnAdapter, so that LangGraph does not introduce a parallel LangChain model stack.
33. As a maintainer, I want the LangGraph tool node to call only AgentLoom Tool Gateway, so that graph tools receive the same Hook, permission and audit treatment.
34. As a maintainer, I want runtime-specific checkpoints wrapped in one AgentLoom envelope, so that storage and discovery remain stable while payloads vary.
35. As a maintainer, I want checkpoint metadata to identify runtime, runtime version and state schema version, so that invalid resume attempts fail deterministically.
36. As a maintainer, I want resume supported only by the runtime that created the checkpoint, so that cross-runtime state conversion is not falsely implied.
37. As a maintainer, I want runtime-neutral audit events kept separately from runtime-specific resumable payloads, so that observability does not pretend to be a lossless runtime state.
38. As a maintainer, I want historical checkpoints inspectable even when they cannot resume, so that retained evidence remains useful.
39. As a maintainer, I want complete canonical model items persisted and restored, so that Responses reasoning and tool-call replay never depend on lossy observation text.
40. As a maintainer, I want `code_act`, CodeAgent construction and code-agent prompts absent from the new runtime path, so that model-generated Python execution cannot return through an alternate branch.
41. As a maintainer, I want text fallback tool-call parsing absent, so that all runtime adapters enforce structured tool invocation.
42. As a maintainer, I want live examples and fixtures to use only the active configuration contract, so that copied configurations are valid.
43. As a maintainer, I want examples that exist only to validate removed code execution deleted, so that the suite reflects supported behavior.
44. As a maintainer, I want useful examples migrated to structured tools where feasible, so that application coverage is retained.
45. As a maintainer, I want the implementation delivered in independently verifiable phases, so that runtime extraction, behavior cleanup and LangGraph introduction can be diagnosed separately.
46. As a reviewer, I want the same minimal Application executed through smolagents and LangGraph in the second stage, so that the runtime seam is proven by two real adapters.
47. As a reviewer, I want each model protocol exercised through both runtime adapters after LangGraph is implemented, so that runtime and protocol dimensions are proven independent.
48. As a reviewer, I want real Application evidence inspected after each run, so that process exit codes and final prose cannot substitute for runtime correctness.
49. As a reviewer, I want validation failures and skips grouped by root cause, so that unavailable credentials are not mistaken for passing protocol support.
50. As a maintainer, I want runtime implementations isolated in one worktree during development, so that broad refactoring does not contaminate the primary checkout.
51. As a maintainer, I want local ignored model configuration updated for validation without committing secrets, so that real provider runs use the new adapter contract.

## Implementation Decisions

### Two independent seams

- `AgentRuntime` is the external seam between AgentLoom invocation ownership and a concrete Agent engine. It covers one complete Agent run, not one model turn.
- `ModelTurnAdapter` is an internal seam for one model request and response. It handles wire protocols but knows nothing about Application topology, Hook lifecycle, checkpoint storage or Worker orchestration.
- Runtime selection and model protocol selection use separate configuration fields and separate registries.
- Runtime construction as well as runtime execution must pass through the runtime registry. A caller that directly constructs `SmolagentsRuntimeAdapter` from production flow has not completed the seam.
- The default CLI and Python execution interfaces remain unchanged; callers do not receive third-party runtime objects.

### AgentRuntime interface

- The interface has one execution operation plus resource cleanup. New execution and resume use the same operation; an optional checkpoint in the request distinguishes them.
- The execution request contains a runtime-neutral Agent definition, task and run identities, additional arguments, limits, runtime requirements, Tool Gateway, model turn adapter, event sink and optional checkpoint.
- The result contains a normalized terminal state, final output, usage, artifacts, normalized event summary and a runtime checkpoint envelope. A raw runtime result may be retained only for diagnostics and is not part of the stable caller contract.
- The interface does not expose step, graph node, message bus, native session or framework-specific callback objects.
- Runtime errors normalize into configuration, unsupported capability, provider, tool, interrupted, budget-limited and internal failure categories without erasing their causal error. LiteLLM/provider errors are passed through as causes; AgentLoom does not inspect or police LiteLLM's private internal routing.

### Configuration

- Every Supervisor and Worker definition declares `agent_runtime`. The current delivery implements `smolagents`; `langgraph` is reserved for the second implementation stage after the smolagents seam, code-action cleanup and model protocols pass their acceptance gates.
- Every usable model type, including summary models, declares `adapter`. First-version values are `openai_chat`, `openai_responses` and `anthropic_messages`.
- The model name is opaque and is passed to LiteLLM. Runtime and protocol are not inferred from it.
- Required fields and known adapter names are checked when an Application is prepared to run, before its first model call. AgentLoom does not issue probe requests or inspect which private LiteLLM route was selected.
- Unsupported runtime capabilities are rejected at Application validation or run preparation, before model or tool execution.
- There is no implicit runtime or model adapter fallback.
- Removed execution-mode names are absent from the supported schema and from generated examples. They do not receive dedicated compatibility handlers or migration documentation.

### AgentLoom ownership

- AgentLoom owns Application interpretation, Supervisor/Worker topology, run/task identity, Hook Plan and Hook Run, Goal, Todo, workspace policy, Tool Gateway, ToolCallRecord, audit events, checkpoint envelope and Application run result.
- Tool Gateway is the only route to a side-effecting tool. Runtime adapters receive proxy tools whose calls settle through AgentLoom authorization, Hook and audit behavior.
- AgentLoom owns a small normalized runtime event vocabulary for run, model, tool, subagent, usage, checkpoint and terminal observations.
- AgentLoom model interaction items are the canonical provider-neutral model history used by ModelTurnAdapter, checkpoint replay, audit and context features. They do not attempt to encode every runtime’s non-model loop state.

### Runtime ownership and checkpoint

- Each runtime adapter owns its loop, conversation/session representation, internal cursor, native callback mapping, pending interrupts, runtime-specific handoff state and lossless resumable payload.
- AgentLoom stores an envelope containing runtime ID, runtime version, state schema version, opaque runtime payload, run/task identity and runtime-neutral audit metadata.
- The envelope is opaque to generic checkpoint storage, but the current smolagents payload must contain the complete ordered canonical model items, provider response IDs and replay payloads required for the next model turn.
- Canonical items preserve message, function call, function-call output and reasoning boundaries. Reasoning signatures, encrypted content or other replay payloads are retained losslessly when supplied, but are not rendered as final-answer text or ordinary logs.
- smolagents `ActionStep`, `ChatMessage` and observation strings may remain compatibility projections inside the adapter. They are not a second authoritative model history and cannot be used to reconstruct missing canonical items.
- Resume requires the same runtime ID and a compatible state schema. Cross-runtime resume is unsupported.
- Canonical model items do not replace runtime-specific loop state; both belong in the adapter payload when both are required for lossless same-runtime resume.
- There is no old-checkpoint translation or best-effort recovery path. Historical checkpoints may remain inspectable, but unsupported resume fails explicitly.

### SmolagentsRuntimeAdapter

- smolagents is the first registered adapter, not an implicit default, AgentLoom superclass or public state model.
- Existing construction, `ToolCallingAgentV2`, RunResult projection, ActionStep conversion, memory reset/continuation, callbacks and required monkey patches move behind this adapter.
- Runtime and Application owners must stop importing smolagents Agent, memory, message and result types.
- The first migration is mechanical and preserves supported structured-tool behavior before protocol changes.
- This stage deletes `CodeAgentV2`, code execution configuration, code-agent prompts, code-action-only examples and text tool-call fallback. It does not retain deprecated aliases or an alternate execution branch.
- Worker calls, Worker cache and Worker resume use runtime-neutral requests, results and checkpoint envelopes at their callers. Native `memory.steps` and `ActionStep` access remains local to this adapter.
- smolagents-specific loop state and canonical model history remain inside its opaque runtime payload.

### LangGraphRuntimeAdapter

- LangGraph is the second real adapter used to validate the seam.
- The adapter builds an AgentLoom-controlled fixed graph. Users cannot supply arbitrary graphs in this version.
- The minimal graph contains a model node, an AgentLoom Tool Gateway node and terminal routing through `final_answer`.
- The model node uses AgentLoom ModelTurnAdapter directly; it does not add a second LangChain provider stack.
- The tool node cannot execute raw LangChain tools; it converts calls to AgentLoom Tool Gateway and writes canonical tool results back into graph state.
- LangGraph nodes, commands, supersteps and checkpointer values remain private to the adapter.
- The adapter maps graph events and terminal output into AgentLoom events and result.
- The first vertical slice covers a single Agent, one or more structured tools, final answer and same-runtime checkpoint/resume. Supervisor/Worker topology and parallel Workers follow after that slice passes.
- The LangGraph dependency is pinned to a verified version and its lockfile changes are reviewed. A dry run against the current environment resolved successfully, but full installation and test evidence are required during implementation.

### ModelTurnAdapter

- AgentLoom owns provider-neutral `message`, `function_call`, `function_call_output` and `reasoning` items.
- `openai_chat` calls LiteLLM chat completion and maps native tool calls into AgentLoom items.
- `openai_responses` calls LiteLLM Responses and preserves supported response items, call IDs, response IDs, usage and replay-safe reasoning fields.
- `anthropic_messages` calls LiteLLM completion with Anthropic provider mapping and normalizes `tool_use`, `tool_result` and thinking into AgentLoom items.
- AgentLoom does not inspect LiteLLM’s private routing. It does not retry through a different adapter when the selected call fails.
- All three adapters consume the same ordered canonical history and return ordered canonical output items. Output order is not inferred from convenience text fields.
- Model request governance that already applies to chat calls, including limits, retries, `Retry-After`, rate limiting, context cache and request headers, applies at the shared adapter boundary to Responses and Anthropic calls as well.
- Text parsing is not a substitute for native tool calls. Unknown executable item types are protocol errors.
- `final_answer` is an AgentLoom terminal tool. A successful call ends the run without an extra model turn.

### Runtime capabilities

- The first capability vocabulary is limited to structured tools, parallel tools, checkpoint/resume and subagents.
- Capabilities describe semantic support, not implementation details.
- Application validation compares compiled runtime requirements with adapter capabilities and rejects unsupported combinations.
- No capability is silently degraded. Parallel-to-sequential fallback, ignored subagents and disabled checkpointing are not allowed unless the Application explicitly requests those semantics.

### Delivery phases

#### Stage 1 — current development

1. Define runtime-neutral request, result, event, capability and checkpoint-envelope contracts plus a runtime registry.
2. Route production construction and complete-run execution through the registry and `SmolagentsRuntimeAdapter`; remove smolagents types from Application, invocation and generic checkpoint owners.
3. Remove code-action execution, code-specific configuration, prompts, dedicated tests, incompatible examples and text tool-call fallback.
4. Implement the shared ModelTurnAdapter and its three wire protocols through LiteLLM.
5. Make canonical items the smolagents model-history source and complete item-native checkpoint/replay, including Worker calls and reasoning replay payloads.
6. Migrate useful Applications to structured tools and delete Applications that exist only to test removed behavior.
7. Pass focused contracts, the full repository test suite, static checks and representative real Application runs for all locally available protocols.
8. Review the complete change against this specification and repository standards before committing the worktree branch.

Each Stage 1 item is part of the current delivery. Work proceeds in order; a later item cannot be declared complete while an earlier acceptance gate is failing. Stage 1 contains no deferred TODO for canonical replay.

#### Stage 2 — after Stage 1 acceptance

1. Add the LangGraph dependency and register a real `langgraph` runtime; until then, selecting it remains an explicit unsupported-runtime error.
2. Build an AgentLoom-controlled fixed StateGraph with a model node, Tool Gateway node and `final_answer` termination.
3. Run the same minimal Application through smolagents and LangGraph and compare normalized results, Hook behavior, ToolCallRecord, events and checkpoint envelopes.
4. Add same-runtime LangGraph checkpoint/resume, then Worker-as-Tool, Supervisor/Worker topology, parallel Workers and Goal continuation.
5. Run the two-runtime by three-protocol matrix wherever credentials and provider support are available.

Stage 2 uses the Stage 1 `AgentRuntime` contract without adding smolagents concepts to it. If LangGraph cannot implement the contract without such leakage, the seam is revised explicitly rather than patched with framework-specific optional fields.

## Testing Decisions

- The highest test seam is the complete AgentRuntime interface. Stage 1 runs its contract suite against smolagents; Stage 2 runs the same suite and request fixtures against LangGraph.
- A second focused seam tests ModelTurnAdapter independently of either Agent runtime.
- Tests assert external results, normalized events, ToolCallRecord outcomes, Hook effects, checkpoint envelopes and resume behavior rather than private runtime step classes.
- Architecture tests reject smolagents and LangGraph imports from runtime/application/checkpoint owners; third-party types remain inside their adapter modules.
- Runtime registry tests verify explicit selection, missing runtime, unknown runtime, construction through the registry and missing optional runtime dependency.
- Capability tests verify that unsupported Application requirements fail before model or tool execution.
- Tool governance tests verify that the current smolagents adapter, and later LangGraph adapter, invoke tools only through AgentLoom Tool Gateway and preserve call IDs, error/blocked states and audit order.
- Smolagents adapter regression tests preserve established run, Worker, Goal, Todo, Hook and checkpoint behavior after mechanical migration.
- Registry tests reserve `langgraph` as an uninstalled or unsupported runtime in this delivery and require a clear preflight failure rather than fallback to smolagents.
- Checkpoint-envelope tests reject runtime IDs or state schemas that do not match the selected adapter. Stage 1 also proves that a checkpoint round-trip restores the same ordered canonical items, call IDs and replay payloads used by the next model request, without observation-text reconstruction.
- Worker checkpoint tests cover falsey results, repeated and parallel same-name calls, call indexes, cache behavior and resume without exposing native runtime memory to the caller.
- Model protocol tests cover `openai_chat`, `openai_responses` and `anthropic_messages`, including native tool calls, ordered outputs, usage, reasoning preservation, governance controls and provider error propagation.
- Text fallback tool-call parser tests are removed with the parser.
- Configuration tests verify explicit runtime and adapter fields for active definitions and model types, including summary models.
- No test preserves removed execution-mode fields as a public contract. Source, fixture and example inventories instead prove that supported code no longer refers to CodeAgent or code-action behavior.
- The current-stage primary end-to-end acceptance test runs representative Applications through the smolagents adapter and verifies final output semantics, ToolCallRecord, Hook behavior, audit events and checkpoint outcome through the `AgentRuntime` interface.
- The current-stage protocol matrix exercises each model adapter through smolagents when credentials and provider support are available. The two-runtime matrix becomes mandatory in the LangGraph stage. Missing credentials are reported as not run, not passed.
- Real validation covers every local Application that does not require manual external infrastructure. Each result is classified as passed, failed, deliberately removed because it only tested unsupported behavior, or skipped with a concrete root cause.
- Every real run is verified through its run manifest, logs, shell/tool audit and required artifacts. Process exit code and final prose alone are insufficient.
- Stage 2 dependency validation pins and installs the selected LangGraph version in an isolated worktree, reviews lockfile changes and reruns packaging/import-boundary checks.

## Out of Scope

- TUI assistant protocol changes.
- User-authored arbitrary LangGraph graphs.
- Implementing `LangGraphRuntimeAdapter` in the current coding pass; it is the explicitly planned second stage.
- Streaming token delivery as a public product feature; normalized non-token runtime events remain in scope for audit and checkpoint integration.
- Cross-runtime checkpoint resume.
- Compatibility resume for historical code-action or pre-item checkpoints.
- Provider built-in executable item families beyond ordinary messages, function calls, function-call outputs and reasoning.
- OpenAI Agents SDK, AutoGen, Agno, Strands, Pi or other additional runtime adapters in this delivery.
- Removing the smolagents adapter.
- Compatibility execution for code-action mode.
- Text-based tool-call fallback.
- Automatic fallback between model protocols or Agent runtimes.
- Treating provider-side conversation storage as the only resume mechanism.
- Public migration or changelog documentation for inactive configuration fields.
- Publishing this specification to an issue tracker.

## Further Notes

- The design follows the public full-run interfaces used by Microsoft Agent Framework `SupportsAgentRun`, AWS Strands `AgentBase`, Semantic Kernel `Agent.invoke`, Agno `AgentProtocol` and Google ADK `BaseAgent.run_async`, while retaining AgentLoom’s own governance and Application semantics.
- OpenAI Agents SDK and Pydantic AI are the primary references for item, reasoning, tool-result and replay modeling. LangChain/LangGraph provides the reference for keeping a graph runtime separate from provider protocol adapters.
- LangGraph is selected because it is widely adopted and actively maintained, and because its graph/checkpoint semantics differ enough from smolagents to expose a false seam.
- LangChain Agent is not the second runtime. LangGraph is the runtime implementation; AgentLoom ModelTurnAdapter remains the provider/model seam.
- Pi has an embeddable TypeScript SDK and a headless RPC mode. It is a strong future out-of-process runtime adapter candidate, not part of this Python in-process delivery.
- The earlier architecture deliberately avoided a generic Agent engine interface because only one implementation existed. The addition of LangGraph creates the second real adapter required to justify the seam.
- Adapter-specific state must remain local. A shared interface that exposes every framework’s step or state type would be shallow and should be rejected.
