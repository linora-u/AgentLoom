# Python native ownership implementation evidence

This note covers tickets #124 and #127 for the source-only execution design. AgentLoom uses its own Python bootstrap and the existing psutil dependency. It does not install, import, execute, bundle, or rename an OpenClaw distribution, SDK, or runtime asset. Existing Pi remains a separate supported runtime.

## Resource boundary

`execution.managed_process` provides one native-resource facade: launch, release the startup gate, request stop, observe the real root result, wait for physical exit, and recover an existing resource receipt. An independent Python guardian is created for each native root. It anchors the native root and retains identity-safe process handles until its owned group and delegated generations have exited, including ordinary detached descendants. Factories, dependencies, and Context remain live in the calling Python process.

The caller's private control socket is independent of stdin/stdout/stderr. Native children inherit their actual stdio and a one-use trusted scope pipe, but do not inherit the caller's control writer. Caller EOF closes admission and triggers cleanup. The guardian freezes and kills ordinary owned process identities while retaining its own identity. Genuine nested guardians remain observers in the exit barrier; outer cleanup closes their parent control channel and waits for their own cleanup rather than killing them prematurely. Linux additionally uses `PR_SET_CHILD_SUBREAPER` and `waitpid` to reap adopted children; the actual root's Popen retains its authentic return code.

`root-result.json` records the observed native root result. `exited.json` records the matching generation's physical cleanup proof. The former does not establish the latter. The proof is fsynced before notification; the parent joins the retired guardian. Cleanup failures retain the resource and first error. Recovery uses the persisted private socket and generation, never a bare saved PID as kill authority.

The guardian acknowledges accepted STOPs and assigns their cleanup sequence. A live caller waits for its own retry acknowledgement, then accepts the current guardian error even if another caller initiated that attempt. Recovery receives its accepted sequence through the same private socket and accepts errors at or after that sequence; old errors cannot settle a new retry. The sequence is part of the control/error protocol, not a new scheduling ledger. Resource descriptions now use version 2. A version 1 description with an actual generation proof and retired anchor remains queryable; an unretired version 1 owner is explicitly rejected for acknowledged STOP rather than assumed compatible. A missing acknowledgement never proves exit or frees capacity.

Built-in task launches use this facade through PiTransport, MCP stdio's public AnyIO byte streams, shell foreground/background handling, captured/file-descriptor commands, grep/glob/profile helpers, hooks, and runtime version/ABI probes. Administrative Pi installation remains outside Agent execution. `AgentInvocation.run()` binds default ownership for ordinary CLI/root execution; each Application Worker attempt binds an independent scope around live factory construction and execution. A trusted durable Host already contained in the outer guardian group uses that group's authority.

The containment contract includes inherited ordinary detached descendants. Scope generation comes from verified guardian bootstrap, and centralized environment binding propagates it to Pi, MCP and registered commands; ambient selectors cannot choose another scope. A new owner requires verified bootstrap, actual live guardian group membership or actual live ancestry. On macOS, a fully detached PID1 caller possessing only an old environment marker is rejected before new guardian/model/business launch while the original marked descendant is still reclaimed. Retained-group callers and Linux subreaper-adopted callers remain admissible. The owner confirms local resources; it cannot certify that a request already received by a remote model provider has stopped billing. It does not own queue admission, business grants, workflow retries, or concurrency quotas: those remain with DBOS and the application.

## Source reference

Only source concepts were inspected in the frozen OpenClaw `v2026.10.3` research snapshot:

- `src/process/supervisor/service-child-relay.ts`: private parent-loss signaling and separate output/control ownership.
- `src/process/supervisor/service-child-group-anchor.ts`: keep cleanup authority alive, distinguish root settlement from retirement, and gate startup.
- `src/node-host/node-worker-child-lifecycle.ts`: explicit admission and child lifecycle ownership.

These files are not part of AgentLoom's runtime or wheel. The guardian's executable is `src/execution/_process_guardian.py`. Its runtime imports only Python stdlib and psutil.

## Public behavior evidence

All checks below use local providers or controlled runtimes and actual local native processes. They make no paid model/search calls and do not modify News output.

| Evidence | Result | Behavior observed |
| --- | --- | --- |
| `tests/application_test/test_python_process_owner.py` | 18 passed, 6.13 s | Top-level SIGKILL with TERM-ignoring descendants and port closure; registered/captured/file-descriptor routes; binary/text stdio; authentic exit code; root-first cleanup; real bootstrap cancellation; grep/glob/profile ownership; live/recovered receipt-write retries with original errno; fragmented wrong-generation rejection; root-result storage fault and recovery; current errors from competing live/recovered STOPs and both retry routes. |
| `tests/application_test/test_python_runtime_resources.py` | 6 passed, 22.16 s | Installed Pi handshake and real MCP peer; normal exit, host SIGKILL, MCP timeout/cancel/start failure, and tool timeout. |
| `tests/application_test/test_python_smol_cli.py` | 5 passed, 23.30 s | Formal CLI with real smol runtime: foreground, background, timeout, promotion, and original cleanup error in a failed terminal receipt. |
| Finite Application/single invocation/control/Hook suites | 93 passed, 15.60 s | Live dependencies, Context, per-map capacity, nested calls, independent batches, partial failure, original controls, and native Hook stdin deadlines. |
| Pi installation regression suite | 21 passed, 21.52 s | Existing installation contract after task-time probe routing. |
| Integrated public runtime/durable suites | 46 passed, 153.79 s | First 13 native cases above, all six Pi/MCP cases, all five smol CLI cases, 20 real Pi/smol batch and CLI cases, and two default durable cases on the integrated source. |

Local logs are preserved under `/Users/bytedance/.codex/tmp/agentloom-managed-execution-20261014/`: `python-root-result-storage-red.log`, `python-root-result-storage-green.log`, `python-recovery-green-3.log`, `python-runtime-green-2.log`, `python-smol-cli-green-1.log`, `python-finite-contract-green-2.log`, `python-pi-install-regression.log`, `python-owner-integrated-green.log`, `python-two-stop-authority-red.log` (both new competing-STOP cases failed on the old source), and `python-two-stop-authority-all-green-2.log`.

The integrated 46-case run preceded the final root-result storage and competing-STOP fixes; the complete 18-case native run includes both fixes. The Pi/MCP runs use the integration worktree's already installed Pi environment with an external PYTHONPATH overlay resolving AgentLoom to the implementation source. The owner worktree's independent environment has no Pi assets; its earlier missing-install failure is not product acceptance evidence. No dependency was installed or changed to obtain the results above.

Linux validation, full News acceptance, final code/design review, package cleanup, and repository delivery are coordinated on the integration branch. Previous SDK probes are not evidence for this Python implementation.

## Final detached and nested qualification

The original public CI #130 keyboard/SDK-death native Bash cases were left unchanged. Frozen fix `96f2c852` and its exact-tree integration merge `9d1ba4cd` retain concrete process identities across detachment and preserve nested observers until inner extinction. Finite, durable root-first, SIGKILL and nested public facts show zero survivors before recovery or safety cleanup. macOS and Linux each passed the same 99-case source matrix. Independent Spec and Standards reviews have no actionable source findings.

The rebuilt `9d1ba4cd` wheel contains 289 byte-identical Python modules and 22 byte-identical packaged assets; SHA-256 `5c767656082d65f7bfadb1e82d3c3d5e9465343d0db21594f31cf03756520576`. Linux passed the same 99-case installed-package matrix. macOS passed 96 installed cases, then the remaining three unchanged cases passed after supplying the documented project-root context for an installed package launched outside a project; this is a 96+3 result, not one 99-case run. Framework imports resolve to the installed target, without a source overlay. Initial macOS Node ABI and missing-project-context failures are retained separately. Historical table entries above retain their own earlier source boundaries. The Aug18 paid run remains `62dd0aeb`/`7a4d6787`; a final-reader audit confirmed its 96 physical/mandatory receipts, 76 current SUCCESS workflows and 288 non-live identities without rerunning paid work.

See [final first-party source review](openclaw-detached-custody-source-only-20261014.md) for exact upstream paths and limits, including nested lineage joins and Linux native wait ownership. AgentLoom borrows lifecycle principles; its Python/psutil implementation is not represented as source-equivalent to OpenClaw native supervision. Repository delivery and required full public CI remain separate gates.
