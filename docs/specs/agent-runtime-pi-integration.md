# AgentLoom 可替换 Agent 基座与 Pi 接入

规格日期：2026-09-20。研究基线：main，c697b24f602e71d56c9aa2c532e6079c0db5aa9d。

状态：开发规格，01–11、13 已有交付记录，12/14 正在串行收口；当前进度以 [票据索引](../tickets/agent-runtime-pi/README.md) 为准。2026-09-21 维护者取消旧 smol YAML 字段兼容：`runtime_options` 是唯一解释的后端配置；旧顶层字段静默忽略，不转换、不拒绝。历史验收记录保留当时事实。

## Problem Statement

AgentLoom 的核心价值是用 YAML 定义多 Agent 应用，并管理协作、长期记忆、工具产物和运行证据。维护者希望未来能更换单个 Supervisor 或 Worker 的 Agent 基座，而不用重新实现这些应用能力。

当前生产注册表只提供 smolagents，虽然已经存在 AgentRuntime、Tool Gateway、模型调用及 checkpoint 的接口，公共运行定义仍要求 Python ModelTurnBinding，并包含 smolagents 的规划、摘要、提示词和执行步数等假设。部分 MCP 工具构造、Goal/Todo 工具和 CLI 异常分类仍依赖 smolagents。只有 fake runtime 的合约测试，不能证明另一套真实 Agent 循环能够替换它。

简单地把 Pi 当工具运行，不能满足按 Agent 选择基座、统一工具权限和记忆复用的要求。反过来，要求 Pi 复制 smolagents 的规划、上下文压缩、模型消息和工具实现，又会失去采用成熟基座的收益。

维护者还希望通过多个独立 worktree 并行开发，在完成验证后合并 main。若各分支独立设计运行契约、Python/Node 协议和工具治理接口，或者同时修改配置、注册表与锁文件，开发会在集成阶段重新串行化。

## Solution

AgentLoom 拥有应用定义、Supervisor/Worker 协作、task/Run 身份、工具治理、长期记忆和可追溯产物。各 Agent 基座拥有单个 Agent 的执行循环、原生模型调用、对话上下文、规划、会话压缩与原生恢复状态。

第二个真实基座确定为 Pi。接入 Pi coding-agent SDK 的 AgentSession，通过受控 Node 子进程运行；复用其原生模型接口、自动会话压缩和官方基础工具。AgentLoom 的记忆、Worker 调用、产物检索、MCP 和专业工具经适配器提供给 Pi。两类工具都遵循 AgentLoom 的权限、Hook、文件保护和证据规则。

仓库 smolagents 应用迁移到 `runtime_options`，启动方式保持不变。旧顶层 smol 参数不再解释，外部应用需自行迁移才能保留这些参数的效果。smolagents 专属实现收进自己的 adapter，其他基座不必复刻。更换基座时保留业务定义与工具权限，允许修改后端专属参数、调整提示词并重新验证任务质量；不承诺模型行为相同或运行中跨基座迁移。

记忆保留现有 Application/Project 范围、持久化和审核行为，不新增必填记忆配置，也不借此次接入重做记忆产品。Pi 会话摘要不会自动成为 AgentLoom 长期记忆。

先完成公共契约和 smol 过渡适配，再让 smol 收口、工具治理、Pi 接入三条分支并行。集成负责人逐条合入集成分支，完成真实应用、发行安装和故障验证后，一次合入 main。

## User Stories

1. As an Application author, I want to keep defining Supervisor and Worker roles in YAML, so that changing an Agent runtime does not require rewriting my application.
2. As an Application author, I want to select smolagents or Pi for each Agent, so that I can replace one executor at a time.
3. As an Application author, I want one canonical runtime_options schema and unchanged launch commands, so that backend settings have a single owner. Old top-level smol fields are silently ignored without conversion or rejection.
4. As an Application author, I want backend-specific options separated from application semantics, so that one runtime does not have to imitate another runtime's algorithms.
5. As an Application author, I want invalid or unsupported runtime_options diagnosed before execution; historical top-level smol fields remain ignored.
6. As an Application author, I want to retain role descriptions, Worker input contracts and collaboration relationships, so that changing a backend preserves my business design.
7. As an Application author, I want unknown or unavailable runtimes rejected explicitly, so that a requested Pi run never silently falls back to smolagents.
8. As an Application author, I want existing model profiles mapped into Pi's native provider configuration, so that I do not maintain a second model catalog.
9. As an Application author, I want unsupported model protocols or parameters reported clearly, so that a native provider mapping does not silently change the request.
10. As an Application author, I want Pi to use its native loop and conversation management, so that AgentLoom does not maintain a second implementation of those mechanisms.
11. As an Application author, I want Pi's official basic tools available under AgentLoom governance, so that I can benefit from their maintained behavior.
12. As an Application author, I want only one active implementation of an equivalent basic tool by default, so that Agents are not confused by duplicate tool choices.
13. As an Application author, I want intentional tool mappings and collisions resolved explicitly, so that registration order cannot silently replace a tool.
14. As an Application author, I want existing AgentLoom professional and business tools available to Pi, so that useful AST, LSP and domain integrations are retained.
15. As an Application author, I want MCP tools independent of smolagents construction, so that external tools remain available after changing runtime.
16. As an Application operator, I want the same authorization rules applied to native and AgentLoom tools, so that selecting Pi does not expand tool authority.
17. As an Application operator, I want transformed arguments revalidated and used by the actual executor, so that Hook decisions govern the operation that really occurs.
18. As an Application operator, I want denied calls recorded as blocked before side effects occur, so that operational evidence accurately represents policy enforcement.
19. As an Application operator, I want existing file protection preserved for supported native writes, so that official tool reuse does not discard recovery material.
20. As an Application operator, I want Shell and recursive search restrictions preserved, so that checking a tool's top-level path is not mistaken for complete enforcement.
21. As an Application operator, I want tool calls correlated across Python and Node, so that I can trace a result to the correct Agent, task and Run.
22. As an Application operator, I want complete tool artifacts retained when model-visible output is truncated, so that later retrieval can recover the relevant evidence.
23. As an Application operator, I want successful results persisted before they are acknowledged to the Agent, so that result delivery is not confused with evidence durability.
24. As an Application operator, I want tool errors, policy blocks and uncertain side effects distinguished, so that recovery does not blindly repeat an operation.
25. As an Application operator, I want fresh Worker instances and isolated native sessions, so that concurrent Workers do not share mutable execution state.
26. As an Application operator, I want Worker creation and completion owned by AgentLoom, so that native subagent features cannot bypass the YAML topology.
27. As an Application operator, I want Goal completion determined by AgentLoom's existing rules, so that a native Agent's final response does not prematurely complete the application objective.
28. As an Application operator, I want regular usage records retained, so that native model execution remains observable without restoring removed Goal budgets.
29. As an Application operator, I want cancellation to reach models, tools and managed child processes, so that an interrupted Run does not leave hidden work running.
30. As an Application operator, I want protocol failure or process death to settle pending requests, so that the application does not wait forever.
31. As an Application operator, I want same-runtime Pi recovery with the same task and a new Run, so that committed progress can survive interruption.
32. As an Application operator, I want completed Worker results reused during supported recovery, so that completed work is not performed again.
33. As an Application operator, I want incompatible or cross-runtime checkpoints rejected, so that unsupported state conversion is never implied.
34. As a memory user, I want AgentLoom to retain verified facts and experience independently of the Agent runtime, so that switching to Pi does not discard accumulated knowledge.
35. As a memory user, I want a later Pi run to use memory learned and accepted during an earlier smolagents run, so that memory portability is demonstrated in practice.
36. As a memory user, I want current memory scopes and approval behavior preserved, so that backend adoption does not introduce new configuration work or change visibility.
37. As a memory user, I want native conversation summaries kept separate from trusted long-term memory, so that generated summaries do not bypass evidence requirements.
38. As a memory user, I want actual tool evidence linked to its source and execution identity, so that a model claim or truncated output cannot masquerade as a verified fact.
39. As an integrator, I want native resources and tools selected explicitly by AgentLoom, so that machine-local Pi settings cannot silently change application behavior.
40. As an integrator, I want Pi installation reproducible without a local upstream checkout, so that clean clones and worktrees can run the same backend.
41. As an integrator, I want a supported Pi-only environment without smolagents, so that runtime independence extends to installation and failure handling.
42. As an integrator, I want credentials limited to the selected runtime instance, so that model secrets do not enter logs, artifacts or unrelated Workers.
43. As a contributor, I want one frozen shared contract before parallel implementation, so that worktrees implement compatible interfaces.
44. As a contributor, I want exclusive ownership of shared source and lockfiles, so that parallel work does not produce avoidable merge conflicts.
45. As a contributor, I want independent worktree environments and evidence directories, so that one branch's tests cannot alter another branch's runtime state.
46. As a contributor, I want the real Pi SDK loop exercised with deterministic provider responses, so that CI can verify integration without depending on model behavior.
47. As a reviewer, I want acceptance through the existing public application entry point, so that passing adapter mocks does not substitute for a working application.
48. As a reviewer, I want unavailable live-provider checks reported separately from passing checks, so that verification claims remain accurate.
49. As a maintainer, I want each worktree merged into one integration branch with intermediate checks, so that main receives a validated combined change.
50. As a maintainer, I want final evidence tied to the delivered source revision and dependency versions, so that earlier branch results cannot substitute for final acceptance.

## Implementation Decisions

### 1. Authority and terminology

- Use existing terms: Application, Supervisor, Worker, task, Run, AgentRuntime, Tool Gateway, ToolCallRecord, Hook Spec, Hook Plan, Hook Run, Goal, ContextRef and memory candidate.
- This specification replaces the earlier runtime specification's planned LangGraph second backend, mandatory shared Python model binding for every backend, prohibition on governed native tool implementations, universal final-answer tool, and universal Todo execution mechanism.
- Preserve the existing canonical Python package identity and source-layout decision. Do not restore legacy imports, CodeAct execution or text-based tool-call guessing.
- The accepted Hook Runtime ADR remains binding: Skills do not authorize Hooks; Hook Plans are immutable; each invocation has its own Hook Run; configured transforms precede strict decoding and core authorization; PreToolUse and Stop are blocking gates; observer Hooks keep their existing failure semantics.
- Keep Goal and its current continuation/completion rules. Goal cost/token budgets have already been removed and are not reintroduced. Ordinary usage accounting remains.

### 2. Module ownership

| Module | Owns | Does not own |
| --- | --- | --- |
| Configuration / Application definition | YAML normalization, provenance, model selection, topology, preflight requirements | Native loops or a second Pi-only application schema |
| Application execution | Run lifecycle, task identity, final receipt, revision and failure reporting | Pi process internals or smol step objects |
| Agent runtime orchestration | Worker lifecycle, shared service access, Goal continuation, runtime contract and recovery coordination | Native planning, conversation compression or provider request implementation |
| Tool Gateway | Resolved tool manifest, authorization, Hook order, file protection, result records, artifact/evidence settlement | All basic-tool algorithms |
| Self-learning | Existing durable memory, evidence gates, review and historical retrieval | Pi conversation history or a new memory policy product |
| smolagents adapter | smol construction, native loop extensions, templates, summary, error recovery and model binding integration | Application ownership or universal backend defaults |
| Pi adapter and bridge | Native AgentSession, model mapping, official tool wrappers, IPC, native state and event translation | Independent application topology, memory authority or permission policy |
| MCP / professional tools | External protocol access and selected specialized operations | smol-specific construction as a prerequisite |

Existing directories are retained. Move implementation only where ownership actually changes; do not create forwarding-only abstractions or conduct an unrelated repository-wide reorganization.

### 3. Runtime and configuration contract

- Retain the existing complete-invocation AgentRuntime seam and registry. Do not add a public per-step loop API or expose native messages, graph nodes or Pi session objects to Application execution.
- Normalize role, instructions, model profile, visible tool contracts, instance identity, runtime requirements and backend options before construction. Model selection is data; a Python ModelTurnBinding is not mandatory for all runtimes.
- Keep shared Python model adapters for smol and existing framework-owned background operations where they are still needed. Native Pi provider support does not require deleting them.
- Backend selection remains explicit through agent_runtime. Unknown, uninstalled or unsupported choices fail without fallback. Validation and construction consume the same registry semantics.
- Use backend-owned runtime_options as the sole interpreted backend configuration; do not add memory configuration. Ignore historical top-level max_steps, planning_interval, smart_summary, todo, prompt and max_consecutive_parse_errors silently, without conversion, rejection or old/new conflict checks. Migrate maintained applications and examples to canonical options.
- Backend defaults belong to each adapter. A smol-only default does not become a Pi requirement; an incompatible key inside runtime_options is diagnosed. runtime_options.max_steps retains smol counting semantics; Pi turn limits are not falsely declared equivalent. Smol uses todo_mode as a quoted string, prompt_template_path as a string path, and max_consecutive_model_errors as a positive integer.
- Results identify completion, interruption, execution-limit termination and failure through the existing result/error model, with final output, usage, artifacts and optional native checkpoint. A native final response does not itself settle an AgentLoom Goal.
- Preserve observable Stop policy while removing universal final_answer injection. smol may continue using its native terminal tool; Pi maps native completion into the same external result contract.
- Runtime capabilities describe tested behavior, including structured tools, platform Worker tools, parallel calls and same-runtime recovery. Additional requirements such as Goal support are enforced where requested; unsupported behavior is rejected, not silently disabled.
- Fresh Worker construction depends on runtime construction semantics, not on the presence of a Python model binding. Every invocation owns independent mutable native state.

### 4. Native model and Pi SDK integration

- Use Pi coding-agent AgentSession through its supported SDK, inside one managed Node process per active Agent instance in the first version. Do not drive the interactive CLI or assume the bare core Agent has the complete automatic compaction behavior.
- Keep the existing model catalog as the user-facing source. Convert explicit profile/provider protocol, model identity, endpoint and supported parameters to Pi-native configuration. Do not infer protocol solely from a model-name prefix or silently discard request headers and required parameters.
- Unsupported profile mappings fail explicitly. There is no requirement to implement all historical smol protocol combinations on Pi before shipping; each advertised Pi mapping must be tested and documented, and existing smol mappings remain supported.
- Use per-instance in-memory credential overrides or an equivalent controlled mechanism. Credentials are excluded from protocol diagnostics, public configuration projections, fingerprints, checkpoints and artifacts.
- Supply controlled resources, settings and auth to Pi. Do not inherit global Pi configuration, arbitrary extensions or undeclared project resources. Preserve AgentLoom's established Skill and Hook selection; no second implicit discovery path gains authority.
- Pi's native model transport, automatic context compaction, retry implementation and message state remain native. Usage reports are normalized without double-counting cumulative and incremental events.

### 5. Tool selection and governance

- Pi uses official basic tools by default where those capabilities are selected for the Agent. AgentLoom provides Worker, memory, historical retrieval, ContextRef, Goal and business/professional tools as appropriate. Do not automatically expose every Pi builtin in addition to the application's selected tools.
- Resolve a tool manifest before execution. Each entry identifies a stable logical tool, visible name/schema, implementation provider, operation class, relevant path/command parameters and applicable evidence/artifact handling.
- Map existing YAML tool selection and permission intent into the manifest. A controlled alias may bridge an existing logical tool to a Pi-native visible name. Unknown ambiguity or a collision is an error; intentional official-tool wrappers are explicit replacements.
- Avoid duplicate basic-tool implementations in an Agent's visible tool list. Existing AgentLoom basic tools remain available to smol and explicit applications; they are not deleted merely because Pi supplies equivalents.
- Generalize the existing Tool Gateway execution pipeline rather than introducing a separate permission engine for Pi. Native execution uses the same governed lifecycle, with an executor supplied by the Pi adapter; Python tools continue through the current invocation path.
- For native calls, preparation performs Hook transforms, final schema decoding, core authorization and required pre-write protection before authorizing execution. The bridge executes exactly the authorized implementation and final arguments.
- Verify the actual SDK entry-point order before freezing this contract: the inspected Pi loop validates tool arguments before its ordinary beforeToolCall hook. A late tool_call/execute wrapper alone cannot preserve an AgentLoom Hook that repairs otherwise invalid raw arguments. C0 must prove a supported adaptation entry with that case; if none exists, stop this integration gate and resolve the SDK/contract incompatibility explicitly, rather than quietly weakening the accepted Hook ADR or proceeding with an unworkable promise.
- Native completion settles output, errors, artifacts and trusted evidence in AgentLoom before acknowledging a successful tool result to Pi. Observer Hook delivery is not the durability barrier and retains the ADR's observer semantics.
- Preserve ToolCallRecord's established completed/error/blocked terminal semantics. Track preparation, authorization, execution, durable commitment and uncertain outcomes in a separate task-scoped execution journal correlated with call identity and native session position; do not turn uncertainty into a fake terminal success or repurpose an observer log as that journal. C0 freezes its public contract, G1 implements durable settlement, and integration connects checkpoint recovery.
- Authorization is correlated with runtime instance, Run, call ID, working directory, provider and final arguments. A grant cannot be reused with different arguments or by another Worker. Policy denial yields blocked and no executor call.
- Preserve applicable Shell command checks and recursive search exclusions. A top-level path check is not a substitute for command policy or an existing execution sandbox. Policy belongs to AgentLoom even when the operation implementation comes from Pi.
- Separate native output presentation from full evidence retention. Capture complete source data or a durable original artifact before irreversible truncation; register retrievable references when Pi keeps full output elsewhere. Do not mark truncated text as complete evidence.
- Completeness is relative to the authorized query and its declared limits, not unlimited file/search output. Record coverage, limits and truncation separately. A search stopped at its match limit has not collected later matches; repeating the search is not recovery of the original artifact. Verify capture for every enabled native mapping and reject unsupported mappings rather than claiming lost data remains retrievable.
- Trusted memory evidence requires an explicit, validated extractor and provenance. Neither successful-looking text nor an arbitrary Pi event becomes trusted evidence automatically.
- MCP construction produces backend-neutral tool definitions and invocations. Connections and tool instances remain scoped and cleaned up according to existing lifecycle rules.

### 6. Memory, Goal and state ownership

- Keep current Application/Project memory scopes, SQLite persistence, evidence/review rules and root-run snapshot visibility. No Worker-private scope, live cross-Worker memory broadcast or new mandatory read/write policy is introduced.
- Keep existing model-visible memory list/propose behavior and platform-owned historical retrieval. Proposing a memory does not bypass the current acceptance process, and native compaction does not promote a fact.
- Expose the same memory context and tools to Pi through runtime-neutral preparation and governed calls. A later Pi Run must be able to consume a memory accepted under the existing rules during a previous smol Run.
- ContextRef remains an AgentLoom task-artifact and retrieval contract, distinct from both long-term memory and native conversation summaries.
- Agent-local planning/Todo mechanisms are backend-owned. Preserve existing smol Todo behavior without forcing Pi to run a duplicate planner. Keep application Goal state, ownership and completion evidence in AgentLoom.
- Native session files are managed artifacts under the owning task/Agent instance. Checkpoint envelopes identify runtime, runtime/bridge/state versions, task and native session reference; public owners do not parse native session internals.
- First-version resume is same-runtime and compatible-version only. Preserve completed Worker reuse and committed results. Incompatible state is rejected before a resumed model/tool call.
- Do not promise exactly-once external side effects across a crash between execution and durable settlement. Known committed calls are not replayed; unresolved calls are marked uncertain and are not automatically repeated as if they never executed.
- Handle the separate window where AgentLoom has committed a result but Pi has not persisted the matching native toolResult. Correlate host commits with native session position and call identity; recover by delivering the committed result into the supported native continuation, without invoking the tool again. Prove this reconciliation through the selected SDK. If a state cannot be reconciled safely, reject automatic resume explicitly; never re-execute to repair missing native history.

### 7. Python / Node protocol

- Freeze one versioned, bidirectional JSONL protocol and shared fixtures before parallel implementation. Protocol stdout contains only LF-delimited frames; diagnostics use stderr. Bounded frames carry artifact references rather than unlimited inline output.
- Handshake identifies protocol, bridge, Pi and Node versions, runtime instance and required capabilities. Reject incompatibility before accepting a Run.
- Requests, responses and events have unambiguous message kinds and correlation IDs. Bind calls to Agent instance, task, Run and native session; retain the provider tool call ID with that identity, not as a globally unique key.
- Define initialize, run, snapshot, cancel and close operations. Run acceptance is distinct from Run completion. Initialization and snapshot/resume obey native session state constraints.
- Distinguish AgentLoom tool invocation from native tool preparation and settlement. Freeze authorization, final-argument, result/error, artifact and evidence acknowledgement semantics in shared fixtures.
- Both sides continue reading while awaiting a Run, Worker or tool callback. Dispatch callbacks with their captured Hook Run and configuration context. Do not hold a session/global execution lock across a callback that requires the other side to progress.
- Cancellation remains processable during model calls, tool calls and compaction. Use bounded cooperative shutdown followed by managed-process termination; process death rejects all pending requests.
- Emit a unique external terminal result after mandatory tool settlement and runtime cleanup. An interrupted or dead process cannot manufacture successful completion. Duplicate or late frames cannot mutate a settled Run.

### 8. Dependencies and distribution

- The reference Pi checkout is not an AgentLoom dependency and is not assumed to exist in another worktree or installation.
- Initial dependency candidate: the published Pi coding-agent package at 0.79.4, requiring Node at least 22.19.0. Registry metadata has been checked; published SDK behavior and packaged assets still require implementation-stage verification.
- Pin the complete Node dependency graph and record integrity, because exact top-level versions alone do not constrain upstream caret dependencies. Verify the actual selected distribution's public SDK, tool factories, resources, compaction and restoration before freezing the bridge contract.
- Maintain an AgentLoom-owned bridge package and reproducible build. Package required JavaScript, dependency closure and SDK assets or install a versioned verified bridge during explicit setup. Running an Application never performs an implicit npm install.
- Keep Pi's bridge dependency graph separate from the existing TUI. Studio development and its dependencies are outside this work.
- Provide a supported Pi-only installation without smolagents. Preserve the recommended existing smol installation/start path by explicitly selecting its dependency extra or equivalent install profile. Verify installed artifacts outside the source checkout, including CLI failure paths.

### 9. Parallel implementation and integration

- C0 owns shared contract changes, configuration normalization, registry behavior and a minimal working smol compatibility update. C0 is not complete if existing smol execution is broken pending later branches.
- After C0's green commit, S1 owns smol-specific cleanup, G1 owns common tool governance/MCP/platform-service integration, and P1 owns Pi adapter/bridge. They start from the same frozen integration commit.
- The integration owner retains shared runtime/Application/configuration entry points and Python packaging/lockfiles. P1 alone owns the bridge's Node manifest/lockfile. Shared acceptance assets have one owner.
- Local interface changes are proposals to the integration owner. Accepted changes land as a common contract revision and are propagated to every affected branch before implementation continues.
- Merge completed branches into an integration branch, not directly into main. Validate intermediate merges, complete real cross-runtime acceptance on the final candidate, then merge the combined change into main.
- Do not parallelize the final shared-registry wiring, lockfile regeneration or final merge. Parallelize owned implementations and independent test-fixture preparation.

## Testing Decisions

### Highest existing seam

The primary acceptance seam is the public execute_app function and its ApplicationRunResult, Run events and persisted artifacts. The existing CLI consumes this same seam. Do not invent an ApplicationRunner class, a separate Pi application launcher or several competing integration-test harnesses.

Runtime contracts, the existing Tool Gateway, memory evidence gates and checkpoint coordination supply focused lower-level checks where fault injection is necessary. A small shared IPC fixture suite is justified by the process boundary; it does not replace Application acceptance.

Good tests assert externally observable results, effects, isolation, records and recovery. Avoid asserting private Pi message/step structures, implementation class names, exact model prose or one historical sequence of internal callbacks.

### Acceptance matrix

| ID | Scenario | Required evidence |
| --- | --- | --- |
| A01 | Canonical smol Application | Migrated runtime_options YAML/CLI run; old top-level smol fields are ignored without conversion or rejection; model profiles, committed Worker reuse and ordinary Goal behavior remain correct |
| A02 | Pi-only Application | Real Pi SDK loop and native provider path execute through execute_app; no hidden smol construction |
| A03 | Mixed-runtime collaboration | smol Supervisor calls a Pi Worker and Pi Supervisor calls a smol Worker through AgentLoom Worker tools; parallel Workers retain independent instance/session/Hook identity |
| A04 | Native and AgentLoom tools | Real official basic-tool implementation and real platform tool complete under one correlated tool/evidence contract; no duplicate basic tools |
| A05 | Governance | Hook-modified arguments are the ones executed; invalid/denied file, Shell and search operations cause no prohibited side effect; blocked differs from error |
| A06 | File protection and large output | Applicable backups exist before mutation; the authorized, query-bounded collected result remains retrievable after presentation truncation; limits and missing coverage are explicit |
| A07 | Memory portability | smol-origin memory accepted under existing evidence/review rules is available in a subsequent Pi Run; scope and snapshot rules are unchanged |
| A08 | Evidence integrity | Wrong Run/call/source, fabricated claims, truncated-as-complete results and blocked/failed calls cannot create trusted successful memory evidence |
| A09 | Goal | Native final output does not bypass root completion ownership, Stop policy, evidence or continuation; removed budgets stay removed |
| A10 | Same-runtime recovery | Pi state survives process restart; same task/new Run; committed Worker results are reused; a host-committed/native-uncommitted tool result is reconciled without re-execution; incompatible and cross-runtime states fail clearly |
| A11 | Cancellation and transport failure | Cancellation during model/tool/compaction, malformed frames and child death terminate pending work without deadlock, orphaned managed children or false success |
| A12 | Ambiguous side effects | Simulated loss after a side effect but before settlement is reported as uncertain, without automatic duplicate execution |
| A13 | Preflight and discovery | Unsupported runtime/model/options or missing dependency fails before relevant execution; definition inspection does not spawn Pi or discover undeclared resources |
| A14 | Distribution and CLI | Clean Pi-only and recommended smol installations work outside the checkout; Pi-only has no direct/transitive smol dependency; the smol profile launches old YAML; help, provider/child failure and CLI stdout remain correct |

### Deterministic and live verification

- Required deterministic CI uses the real Pi SDK, real bridge, real tool implementations and real AgentLoom execution. Replace only provider responses through a supported Pi test-provider mechanism or a local protocol-faithful provider endpoint.
- Fake AgentRuntime implementations remain useful for contract failures and isolation tests; they do not satisfy A02–A04 or demonstrate native Pi integration.
- Add a bounded live-provider smoke through the same Application seam to verify actual model profile translation and at least one native and one platform tool. Preserve the configured model and independent output oracle. Missing credentials are NOT-RUN with a reason, never PASS.
- All deterministic acceptance and package checks are required before final merge. A live smoke is a separately reported release verification item: if unavailable, the merge record must explicitly state that real-provider interoperability was not verified; it cannot claim full live acceptance.
- An executed live smoke that fails is a known defect, not NOT-RUN. Fix it and rerun, or explicitly withdraw the failing mapping from advertised support and revalidate rejection; do not waive a known failure by calling the check optional.
- Reuse existing runtime-definition, runtime-selection, Tool Gateway pipeline, Hook boundary, memory evidence, event-import, Run lifecycle, CLI transport and real checkpoint test patterns.
- Run focused checks in each worktree; run the existing required full test/CI set and the new combined matrix on the final integrated revision. Do not repeat expensive real-model application suites on every branch.
- Retain dependency versions, source revision, fixture digest, effective runtime/model identity, tool records and independently checked artifacts. Do not overwrite failed attempts or substitute logs from another branch.

## Out of Scope

- Studio/TUI features, a new user interface or a second Node dependency graph inside the TUI.
- LangGraph integration in this delivery, arbitrary external multi-Agent graphs, a new DAG language or redesigned Supervisor/Worker semantics.
- Reimplementing Pi's loop, provider stack, native compaction, editor algorithm or planner in AgentLoom.
- Universal message history, universal step counting, identical model behavior, or cross-runtime checkpoint migration.
- New memory scopes, new memory policy configuration, a new retrieval algorithm, live memory broadcasting or automatic promotion of Pi summaries.
- Reintroducing CodeAct, legacy Python namespace compatibility or removed Goal budgets.
- Automatically enabling arbitrary Pi extensions, tools, Skills or user-directory settings.
- General-purpose remote process pools, cross-machine execution, plugin marketplaces or framework-wide async rewrites.
- Modifying, vendoring by accident or depending on the developer's local upstream reference checkouts.
- Creating feature worktrees, implementing code or merging main as part of this specification-writing task.

## Further Notes

- The user's local-document delivery instruction overrides the skill's default issue-publication destination. No new issue or triage action is required for this delivery.
- The Application acceptance seam follows the already discussed real-application replacement scenario; no additional interview is needed to produce this spec.
- Preserve the accepted Hook ADR, current Goal-without-budget behavior and established memory evidence rules when resolving older documentation conflicts.
- Companion execution-plan task IDs C0, S1, G1, P1, V1 and I0 refer to implementation deliverables, not to current completion claims.
- The first implementation gate verifies the actual Pi package distribution. Local source inspection and registry metadata are evidence of feasibility, not proof that a packaged adapter already works.
- Architecture is accepted through demonstrable substitution: change the selected Agent runtime and any explicitly backend-specific options while retaining the Application's business contract, governance and durable memory.
