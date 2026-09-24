# Spec: Unified Step Observability and Durable Large Tool Results

## Problem Statement

Users cannot reliably follow a pi Run Step by Step. Its text output mainly shows setup messages and the final answer, while smolagents already prints a useful New run and Step sequence. The runtimes present different views of the same work. Existing RuntimeEvent messages, Hook diagnostics, runtime.log, and checkpoints each serve a narrower purpose; none alone shows the actual Model request, the committed Tool outcome, and what the Model subsequently received.

Large Tool results create a second problem. The original result, the content shown to the Model, and the human log can differ without a durable record linking them. ContextStore is a bounded cache whose entries can be evicted or expire; it cannot guarantee later retrieval of a full result. A large platform Tool result can also cross the pi bridge in full even when the Model receives only a preview. Users need to inspect the original result, see exactly what the Model saw, and continue a Task from its latest safe checkpoint without silently losing a reference or repeating a committed Tool effect.

## Solution

The pi and smolagents adapters report the same runtime-neutral execution facts. One shared presenter renders them in the existing smolagents style: New run, Step, Calling tool with the final authorized arguments, Observations, errors, duration and tokens, and one accepted final answer. At INFO level, Observations prints the text actually delivered to the Model: a small result in full, or the same preview and resolvable reference delivered for a large result. The Model response body follows smolagents' DEBUG behavior. Terminal text mode and runtime.log use the same presentation. Required local records separately link the actual Model request and response, effective Hook decisions, complete Tool input and result, and Model-visible projection.

The trace index and large content are files associated with the logical Task, independent of any one Run and of checkpoint cleanup. A Run's evidence and runtime checkpoint can refer to the same content through a scoped opaque reference. This does not require a new database. A bounded, read-only capability lets the Agent search or page through content by byte range; a Python inspection API expands a retained Step. A reference is never shown to the Model until its content is durable and retrievable.

The existing Hook Plan and per-invocation HookRun remain responsible for Tool authorization, input changes, observers, and Stop decisions. The Agent loop still owns Model calls, Tool scheduling, retries, and checkpoint state; Model configuration is bound when the runtime is constructed, then called by that loop. Trace and presentation observe actual execution boundaries rather than creating new decision Hooks. Local trace persistence is required for Run success. Future external exporters consume the provider-neutral facts and references asynchronously; their failures do not fail the Run.

## User Stories

1. As a CLI user, I want pi Runs to show a New run panel, so that the task and Agent context are visible before execution.
2. As a CLI user, I want both runtimes to show the same Step rule and numbering style, so that switching runtimes does not change how I read a Run.
3. As a CLI user, I want each Tool invocation to show its final authorized arguments, so that I can tell what actually executed.
4. As a CLI user, I want each Tool result to appear under Observations, so that I can follow the Agent's work without opening audit files.
5. As a CLI user, I want the Observations text to match what the Model was shown, so that the log does not imply the Model read unavailable content.
6. As a CLI user, I want small Tool results printed in full, so that ordinary Steps remain directly readable.
7. As a CLI user, I want large Tool results displayed as a preview with a usable reference, so that I can see the Model's actual context without flooding the Run log.
8. As a CLI user, I want Step duration and token totals and deltas, so that I can identify slow or expensive turns.
9. As a CLI user, I want errors shown in the same Step presentation, so that a failed Tool or Model call is not mistaken for success.
10. As a CLI user, I want the accepted final answer printed once, so that a rejected Stop proposal or CLI duplication does not look like completion.
11. As a CLI user, I want the runtime log to preserve the same Step content as text mode, so that I can inspect a Run after the terminal closes.
12. As a machine-output consumer, I want JSON and JSONL stdout to retain their existing lifecycle protocol, so that human Step formatting cannot corrupt parsing.
13. As an Application author, I want pi and smolagents to use the same Step semantics, so that one workflow has comparable evidence under either runtime.
14. As an Application author, I want parallel Tool calls associated with their call IDs inside one Step, so that interleaved results remain attributable.
15. As an Application author, I want Supervisor and Worker Steps linked by parent identity, so that nested execution remains understandable.
16. As an Application author, I want retry attempts distinguished from new Steps, so that transient Model errors do not distort the execution history.
17. As an Application author, I want Stop continuation and output correction shown as later Model turns, so that the displayed sequence matches execution.
18. As an Agent, I want a small Tool result delivered directly, so that ordinary work needs no extra retrieval call.
19. As an Agent, I want a large result represented by a preview and reference, so that it does not consume the entire Model context.
20. As an Agent, I want a read-only retrieval capability available whenever a result reference can be produced, so that I never receive an unusable reference.
21. As an Agent, I want to search and page through a referenced result with a strict byte limit, so that one very long line cannot overflow the next Model turn.
22. As a Run inspector, I want to expand a Step through a Python API, so that I can view the complete retained Tool result and Model exchange without a new CLI command.
23. As a Run inspector, I want the size-unabridged retained result and Model-visible projection linked to the same Tool call, so that I can explain why the Model made a decision.
24. As a Run inspector, I want the actual Model request and response captured after AgentLoom's request projection, so that I can see the context used for each turn.
25. As a Run inspector, I want sensitive fields redacted according to one policy, so that local presentation and later export do not expose them accidentally.
26. As a Task owner, I want referenced results to survive a resumed Run with a new run ID, so that checkpoint recovery can continue from committed work.
27. As a Task owner, I want completed Tool effects recognized during resume, so that recovering a Step does not repeat an already committed side effect.
28. As a Task owner, I want a missing or corrupt reference reported explicitly, so that recovery never silently substitutes an incomplete result.
29. As a maintainer, I want full large results kept out of pi bridge frames, so that a Model preview does not hide a transport-size failure.
30. As a maintainer, I want local trace write failures to fail the Run clearly, so that a reported success always has its required execution evidence.
31. As a maintainer, I want external exporter failures isolated from execution, so that an observability service outage cannot stop an Agent.
32. As a maintainer, I want a runtime-neutral presenter without a smolagents dependency, so that the pi-only installation remains valid.
33. As an integration author, I want stable event, parent, Step, and payload identities, so that a future Langfuse or other exporter can build its own trace tree.
34. As a reviewer, I want the same Application-level validation seam used for both runtimes, so that the feature is proven by observable Run behavior rather than internal method assertions.
35. As an Application author, I want PreToolUse and Stop to retain their current blocking behavior, so that adding trace does not bypass execution policy.
36. As a Run inspector, I want a blocked Tool call distinguished from an executed Tool failure, so that the history does not claim a side effect occurred.
37. As a maintainer, I want pi's Hook context to carry the real Step number, so that Hook decisions and Tool calls can be correlated with the displayed Step.
38. As a Shell Hook author, I want the current PostToolUse tool_response contract preserved, so that adding large-result references does not silently change my Hook input.

## Implementation Decisions

### Execution facts and ownership

- Keep the existing AgentRuntime operation at the level of a complete Agent invocation. Model configuration enters the concrete runtime at construction; its Agent loop decides when to call the Model and Tools. The pi Node bridge and smolagents Python Model-turn boundary capture the actual request after request projection, before provider dispatch, and the corresponding response or failure. A higher Application layer cannot infer what the provider actually received.
- Normalize these runtime facts into a versioned internal vocabulary identifying Application, logical Task, execution Run, Agent invocation and parent, local Step, commit sequence, Model turn and retry attempt, and Tool call. A Step covers one Model turn and its Tool batch; parallel calls are Step children correlated by call ID. A retry of the same turn is an attempt; output correction and Stop continuation create later turns. The Step is observational, not a new AgentRuntime operation or configured Hook.
- Use existing boundaries. smolagents Tools and pi platform Tools pass through Tool Gateway; pi native file and Shell Tools pass through NativeToolHost. Both runtimes' Stop decisions use their existing HookRun, owned by each Agent invocation. Take Tool facts from terminal ToolCallRecord values and Hook facts from effective PreToolUse and Stop decisions. smolagents already has a Step callback; pi has Model-turn events but currently leaves HookRun.step_number at its default zero. Synchronize pi's real Step identity to that context and verify parallel calls and resumed runs.
- Preserve Hook semantics: PreToolUse may change or block Tool input before strict validation and CoreToolGuard; Stop may reject a final answer. PostToolUse, PostToolUseFailure and StopFailure only observe. A blocked call has no Tool effect and does not emit PostToolUseFailure. An unavailable Tool, Model failure, or bridge failure is not an executed Tool failure. Do not duplicate Post dispatch across the pi bridge and Python execution boundary, introduce a general SDK Hook forwarding layer, or add an automatic retry Hook.
- Separate a required internal recorder from the existing best-effort RuntimeEvent/user event sink and Post observers. Write from the committed Tool boundary and actual Model boundary; do not use HookRun.dispatch or its bounded diagnostic snapshot as the required record. The recorder write must sit outside the exception handlers that deliberately ignore Post observer failures. A local write failure fails the Run, even if a Tool effect was already committed; recovery must then reconcile that effect instead of retrying blindly. User observer errors keep their current Run API behavior, and future exporter failures do not block execution.
- Store the actual projected Model request and provider response, the Tool's final authorized input, returned content, Model-visible content, status, timing and error. Apply one sensitive-data policy to stored content; non-secret content is not truncated for size, and large bodies are linked by stable IDs rather than duplicated in every event. “Complete” means complete apart from that policy. The inspection API reconstructs a Step from these linked records.

### Shared smolagents-style presentation

- Extract a runtime-neutral Rich presenter using the current AgentLoom smolagents labels, order, and visual vocabulary: New run panel, yellow Step rule, Calling tool argument panel, Observations, red errors, Step duration with cumulative and per-Step token usage, and yellow final answer. Both runtimes render the same fact in the same format without adding a smolagents dependency to a pi-only installation. Text mode and runtime file logging render the same Step facts; machine-readable stdout remains reserved for its current protocol.
- The displayed Step number is local to an Agent invocation, as in smolagents. Agent and parent identity disambiguate concurrent Workers; the trace also records a global sequence. Model reply body stays at DEBUG, matching smolagents INFO behavior. The structured trace retains the Model exchange independently of log verbosity.
- The Observations body is the exact Tool text delivered to the Model. Apply required sensitive-field masking before storing retrievable Tool content and forming the Model-visible projection, rather than masking or summarizing the display independently. Small results appear in full; large results show the same preview and reference delivered to the Model. Retain the full-size, policy-sanitized result separately for inspection; Agent retrieval must not reveal fields removed by that policy.
- Print an accepted final answer only after Stop and output validation succeed. Prevent the final CLI echo from printing it a second time. Display rejected proposals as failed or continued Steps, not as completed answers.
- Preserve all Step text for a retained trace, either by retaining every runtime-log segment or by regenerating the plain-text projection from durable execution records. A bounded active log must not be the only copy of historical Step content.

### Large results and references

- Use ordinary files for immutable Task-scoped payload storage and a small Task-scoped trace index under the configured runtime root. Task scope is required because resume creates a new Run ID while retaining the Task ID. This storage is independent of successful checkpoint cleanup, ordinary Run retention, and Run log rotation. It is not a new database.
- Stream content into a temporary file, calculate a full integrity hash and byte count, then atomically publish it. A small metadata record links an opaque scoped result reference to content type, hash, Tool call, producing Run and Step, and storage key. The Model receives the opaque reference, not a filesystem path or raw content hash.
- Select inline content or a preview-plus-reference using both the available Model context budget and bridge byte budget. The terminal presentation does not determine the Model budget. Small content is delivered to the Model and printed in full; a large result's projection includes its size, source identity, and a clear retrieval instruction.
- Provide one internal, read-only retrieval capability for both runtimes whenever references can be issued. It resolves only content authorized for the current Task and Agent context. Preserve compatibility with existing ContextRef consumers while backing new durable result references with the payload store. Do not require every Agent YAML to list a second external Tool.
- Retrieval supports bounded search and byte-range paging with an opaque continuation cursor. Every response has a hard byte ceiling and reports whether more content remains. There is no unlimited read option; a single-line JSON result remains fully recoverable across pages.
- Pi platform Tool callbacks return a committed receipt and Model-visible projection across the bridge. The full platform result remains in AgentLoom-owned storage and does not re-enter the bridge as a second large record; smolagents likewise keeps original content and projection distinct. This does not change the existing Shell Post Hook stdin contract: PostToolUse still receives the full tool_response. A versioned reference input for Shell Hooks requires separate evidence and design; it is not introduced silently here.
- Persist the payload before publishing its reference or sending the Model projection. A local persistence failure fails the Run with a clear cause. If a Tool effect already occurred, recovery must verify existing commit evidence and either reuse the committed outcome or refuse automatic replay when its state is uncertain. It must not claim exactly-once effects merely because a trace entry was written.
- The trace index and payload store have no ContextStore-style entry-count eviction or independent TTL in this release. They are not removed by current automatic Run cleanup or successful checkpoint cleanup. Explicit deletion and eventual storage-management policy are separate work; no new automatic trace-cleanup policy is introduced.

### Recovery and export

- Runtime checkpoints continue to own resumable Agent state and committed-effect evidence. Trace records own inspection evidence. Resume starts from the latest safe committed checkpoint only after validating referenced content and Tool commit state. A missing or corrupt reference, or an uncertain side effect, must cause an explicit failure rather than an incomplete replay. Historical browsing does not imply arbitrary-Step time travel or deterministic rerun.
- The Python inspection interface resolves a Run and Step to ordered metadata, Model-visible content, and complete retained payloads with integrity checks. It offers bounded reads for large content.
- Internal events and payload references are provider-neutral. A future exporter may map them to spans and selectively load content under its own policy; full local payloads are not sent remotely by default. Export is asynchronous and non-blocking, with failures recorded diagnostically. No Langfuse, OTLP, or other remote destination is enabled by this specification.

## Testing Decisions

- The primary test seam is a real Application Run through the public execution API and text CLI, using deterministic local Model and Tool fixtures. Run the same behavioral scenarios with pi and smolagents. Assertions inspect user-visible text, the runtime log, the Run receipt, the Python inspection API, and resume outcome rather than Rich object types or private callback order.
- Prior art is the existing pi Application tests, smolagents Runtime Adapter and checkpoint tests, CLI output-protocol tests, Tool Gateway tests, and Run-observability tests. Reuse their fake-provider and isolated-runtime-root patterns.
- Verify the New run, Step, final authorized Tool argument, Observations, duration/token, error, and single accepted final-answer presentation in both runtimes. Verify per-Agent numbering and parent/call correlation with parallel Workers and Tool calls, retries, output correction, and Stop continuation. Pi's existing Hook context must report the real Step number, including after resume.
- Verify that small Tool content is identical in Model input and Observations. For a large multiline result and a single-line result larger than one page, verify the Model and log receive the same preview/reference, repeated bounded reads reconstruct the complete result, and the local inspection API returns it. The existing Shell Post Hook must still receive the full tool_response.
- Verify that pi bridge communication stays bounded even when the platform Tool returns a result larger than its frame limit. The Tool is invoked once, and its full result remains inspectable. Verify both runtimes capture the actual projected Model request, including history and Tool definitions, and its corresponding response or error rather than an upper-layer approximation.
- Verify interruption after committed work, resume under a new Run ID, reference resolution, and no repeated committed Tool effect. Missing or modified payloads and uncertain Tool commit states must produce explicit failures rather than automatic replay.
- Verify PreToolUse input modification and blocking, exactly one matching Post observer for a completed or failed call, no PostToolUseFailure for a blocked call, and Stop gating in both runtimes. Model and bridge failures must not become Tool-failure Hook events. Verify Post observer failure cannot hide a required trace-write failure or change an already committed Tool result.
- Verify local trace-write failure changes the Run outcome and never publishes an unusable reference. Check that the Model-visible projection and Observations apply the same redaction before delivery. A failing stub of the future exporter interface, if introduced in this release, must not affect the Run.
- Verify text formatting never contaminates JSON or JSONL stdout and that the pi-only installation does not import smolagents. Avoid tests that merely mirror private storage structures or renderer implementation.

## Out of Scope

- A new Step or Model Hook, general SDK Hook forwarding layer, automatic retry Hook, changes to configured Hook authorization or Stop semantics, or a public AgentRuntime step execution API.
- A Studio trace page, a new CLI trace command, or a separate user-facing trace JSONL protocol.
- Actual Langfuse, OTLP, or other remote exporter installation and any default remote full-content upload policy.
- Arbitrary historical Step time travel, deterministic replay of external Model or Tool responses, and cross-runtime checkpoint conversion.
- Rendering binary media inline as text. Media retains type and payload identity, with a human-readable reference in text output.
- A new automatic cleanup policy for trace content, or a breaking change to the Shell Post Hook input contract.

## Further Notes

- This specification follows the established distinction between Task ID and Run ID and between Run evidence and checkpoint state. The runtime-neutral AgentRuntime seam and Hook Runtime ordering remain authoritative.
- The local record retains non-secret content without size truncation and links the full-size, policy-sanitized Tool result to its Model-visible projection. "Complete" does not promise recovery of fields removed by the shared sensitive-data policy. Observations follows the Model-visible projection; the retained result remains available through authorized local inspection and bounded retrieval.
- HookRun diagnostics are bounded and do not retain successful Tool result bodies, so they cannot provide the required full trace. ContextStore's capacity eviction and optional TTL likewise make it unsuitable as the only source of a durable result reference.
