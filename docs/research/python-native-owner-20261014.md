# Python native ownership implementation evidence

This note covers tickets #124 and #127 for the source-only execution design. AgentLoom uses its own Python bootstrap and the existing psutil dependency. It does not install, import, execute, bundle, or rename an OpenClaw distribution, SDK, or runtime asset. Existing Pi remains a separate supported runtime.

## Resource boundary

`execution.managed_process` provides one native-resource facade: launch, release the startup gate, request stop, observe the real root result, wait for physical exit, and recover an existing resource receipt. An independent Python guardian is created for each native root. It anchors that root's process group until all managed members have exited. Factories, dependencies, and Context remain live in the calling Python process.

The caller's private control socket is independent of stdin/stdout/stderr. Native children inherit their actual stdio and a one-use trusted scope pipe, but do not inherit the caller's control writer. Caller EOF closes admission and triggers cleanup. The guardian freezes and kills concrete group members while retaining its own identity. Linux additionally uses `PR_SET_CHILD_SUBREAPER` and `waitpid` to reap adopted children; the actual root's Popen retains its authentic return code.

`root-result.json` records the observed native root result. `exited.json` records the matching generation's physical cleanup proof. The former does not establish the latter. The proof is fsynced before notification; the parent joins the retired guardian. Cleanup failures retain the resource and first error. A later stop has a request identity so an earlier failed receipt cannot settle a new cleanup attempt. Recovery uses the persisted private socket and generation, never a bare saved PID as kill authority.

Built-in task launches use this facade through PiTransport, MCP stdio's public AnyIO byte streams, shell foreground/background handling, captured/file-descriptor commands, grep/glob/profile helpers, hooks, and runtime version/ABI probes. Administrative Pi installation remains outside Agent execution. `AgentInvocation.run()` binds default ownership for ordinary CLI/root execution; each Application Worker attempt binds an independent scope around live factory construction and execution. A trusted durable Host already contained in the outer guardian group uses that group's authority.

The accepted containment contract requires task descendants to remain in their managed group. The owner confirms local resources; it cannot certify that a request already received by a remote model provider has stopped billing. It does not own queue admission, business grants, workflow retries, or concurrency quotas: those remain with DBOS and the application.

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
| `tests/application_test/test_python_process_owner.py` | 14 passed, 5.71 s | Top-level SIGKILL with TERM-ignoring descendants and port closure; registered/captured/file-descriptor routes; binary/text stdio; authentic exit code; root-first cleanup; real bootstrap cancellation; grep/glob/profile ownership; live/recovered receipt-write retries with original errno; fragmented wrong-generation rejection; root-result storage fault and recovery. |
| `tests/application_test/test_python_runtime_resources.py` | 6 passed, 22.16 s | Installed Pi handshake and real MCP peer; normal exit, host SIGKILL, MCP timeout/cancel/start failure, and tool timeout. |
| `tests/application_test/test_python_smol_cli.py` | 5 passed, 23.30 s | Formal CLI with real smol runtime: foreground, background, timeout, promotion, and original cleanup error in a failed terminal receipt. |
| Finite Application/single invocation/control/Hook suites | 93 passed, 15.60 s | Live dependencies, Context, per-map capacity, nested calls, independent batches, partial failure, original controls, and native Hook stdin deadlines. |
| Pi installation regression suite | 21 passed, 21.52 s | Existing installation contract after task-time probe routing. |
| Integrated public runtime/durable suites | 46 passed, 153.79 s | First 13 native cases above, all six Pi/MCP cases, all five smol CLI cases, 20 real Pi/smol batch and CLI cases, and two default durable cases on the integrated source. |

Local logs are preserved under `/Users/bytedance/.codex/tmp/agentloom-managed-execution-20261014/`: `python-root-result-storage-red.log`, `python-root-result-storage-green.log`, `python-recovery-green-3.log`, `python-runtime-green-2.log`, `python-smol-cli-green-1.log`, `python-finite-contract-green-2.log`, `python-pi-install-regression.log`, and `python-owner-integrated-green.log`.

The integrated 46-case run preceded the final root-result storage fix; the complete 14-case native run includes that fix. The Pi/MCP runs use the integration worktree's already installed Pi environment with an external PYTHONPATH overlay resolving AgentLoom to the implementation source. The owner worktree's independent environment has no Pi assets; its earlier missing-install failure is not product acceptance evidence. No dependency was installed or changed to obtain the results above.

Linux validation, full News acceptance, final code/design review, package cleanup, and repository delivery are coordinated on the integration branch. Previous SDK probes are not evidence for this Python implementation.
