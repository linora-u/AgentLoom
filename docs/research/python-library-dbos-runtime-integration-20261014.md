# Python library + DBOS runtime integration assessment

2026-10-14. Scope is research only. This document answers the narrowed question from `read_library_dbos_runtime_brief`: can AgentLoom meet the new local durable runtime contract by combining DBOS 3.2.0 public SDK with AnyIO/Trio/loky/Pebble/Pydantic AI-style durable execution, while avoiding launchd/systemd, an always-on broker/database service, and a new AgentLoom-owned process supervisor?

## Decision

No current candidate in this set satisfies the whole conjunction.

DBOS 3.2.0 remains a suitable candidate for durable queue admission, workflow identity, recovery, retry, and the existing 20/30/20 stage capacities. It does not provide a public cross-platform process guardian that survives `SIGKILL` of the top-level executor and then proves all Pi/Node/MCP/tool descendants have exited. AnyIO and Trio provide structured async subprocess lifecycle for direct children. loky gets closer for child-process pools and has useful tree-kill code, but the already preserved AgentLoom probe shows a root worker `os._exit(17)` can leave a native descendant alive while replacement begins. Pydantic AI's durable execution integration is useful as an architectural pattern—agent operations are wrapped as durable engine units—but it does not add the missing local process ownership layer.

Therefore the conclusion is: **DBOS can still own durable work; a separate library-owned guardian that meets the stated Pi/Node/MCP descendant cleanup contract on both macOS and Linux was not established from this set of public APIs.** The missing component is a mature, public, pip-installable process-container library with a guardian/nanny contract. Changing the queue engine alone has not been shown to fill this gap. This is a finding about the reviewed candidates, not proof that no such library can exist; the user's requirements remain unchanged.

## Requirements that drive the answer

The binding requirement is not "durable workflow resumes." It is: after killing only the top-level executor Python process, a separate library-owned guardian may survive, automatically clean every legitimate task descendant process, and then exit. The scope includes Pi/Node, MCP stdio servers, ordinary tool subprocesses, and their grandchildren. Manual cleanup, waiting for restart, marking a `Future` terminal, or joining only a direct PID is insufficient. Local client lifetime and remote provider inference lifetime must remain separate claims.

The user also excluded the old deployment answer: no user-configured launchd/systemd, no separately deployed broker/database/service by default, and no new queue/claim/lease/retry scheduler. This rules out recommending the current macOS launchd design as the solution, even though it passed a full NewsAgent date.

## Current AgentLoom facts

The current durable executor already separates DBOS from OS ownership. `DurableExecutor` registers DBOS workflow and queue listening through public DBOS APIs, then invokes `self.jobs.invoke(task)` inside a preemptible DBOS step; queue capacity is configured from Worker policy and DBOS owns workflow retry/admission state.[^executor]

The current OS owner is still `LaunchdJobs`, and it is explicitly macOS-only:

> `RuntimeError("durable OS execution currently supports macOS launchd; Linux adapter is not validated")`

It writes one launchd plist per invocation with `KeepAlive=false`, `AbandonProcessGroup=false`, and then waits for the concrete job root and process group to disappear before releasing the invocation scope.[^osjob] That is old implementation fact, not an acceptable answer to the new task because launchd/systemd are excluded as default user-configured deployment mechanisms.

The newer in-process scope code is AgentLoom-authored. `process_scope.py` binds an invocation process group, and Pi/MCP/tool code checks that scope to decide whether children should join the Application's group instead of starting a separate session.[^scope] MCP stdio uses AnyIO public process APIs and `ClientSession`, but the managed transport itself is local AgentLoom code.[^mcp] Pi passes `AGENTLOOM_MANAGED_PROCESS_GROUP` to its Node bridge from trusted host context.[^pi]

That means this scope work is not evidence that a third-party library already owns the full guardian problem. It is evidence that AgentLoom has started building the missing process-scope seam itself.

## Candidate findings

### DBOS 3.2.0

Documented guarantee: DBOS workflows checkpoint state in a database; on failure/restart, workflows resume from the last completed step. DBOS queues are database-backed and provide concurrency limits, rate limits, timeouts, prioritization, and durable scheduling.[^pydantic-dbos-doc] AgentLoom pins `dbos==3.2.0` in the durable extra.[^pyproject]

Established in this repo: DBOS 3.2.0 with current product code and macOS launchd completed the full NewsAgent date. The status document records 80 current workflows as `SUCCESS`, 594 candidates completed, and 122 OS invocations with revoke/unload/physical-exit receipts.[^dbos-product-status]

Not established: DBOS itself does not own Pi/Node/MCP/tool process trees. The current product had to add `LaunchdJobs` for OS scope ownership. Existing lifecycle research also records that DBOS `destroy()` is not physical drain proof: logical workflow shutdown and process-resource exit are separate observations.[^durable-selection] DBOS therefore cannot alone satisfy the SIGKILL-of-executor cleanup acceptance.

### AnyIO

Documented/source-supported guarantee: AnyIO exposes async subprocess handling. Existing repo research fixed AnyIO 4.11.0 and found `Process.aclose()`/cancellation behavior waits for a direct process but does not recursively own descendants. `start_new_session=True` creates a session; it does not change `kill()` into a tree kill.[^process-research-anyio]

Upstream-tested boundary: AnyIO tests include direct-process cancellation behavior and a case where wait returns after the direct process exits even if a grandchild still holds stdout open.[^process-research-anyio]

Not established: a library-owned guardian/nanny that survives parent executor `SIGKILL` and cleans all descendants on macOS and Linux. AnyIO is useful inside a managed transport, as AgentLoom's `managed_stdio_tools()` demonstrates, but it is not the guardian.

### Trio

Documented/source-supported guarantee: Trio `run_process()` handles cancellation of the direct subprocess and supports a custom `deliver_cancel` hook. The existing process research records that `deliver_cancel` itself is cancelled once the direct subprocess exits, and normal successful completion does not use the cancellation hook.[^process-research-trio]

Not established: a public API contract for "wait until the whole process group or resource tree is empty" after the top-level executor is killed. Trio could implement clean direct-process cancellation, but the missing requirement is cross-platform descendant ownership after executor death.

### loky 3.7.0

Documented/source-supported guarantee: loky provides a mature process-pool executor. Its `kill_process_tree()` implementation uses psutil to discover descendants and sends kill signals; `ProcessPoolExecutor.shutdown(wait=True, kill_workers=True)` waits for the executor management thread.[^process-research-loky]

Upstream-tested boundary: upstream loky tests call `kill_process_tree()` and then separately use `psutil.wait_procs(children, timeout=5)` to confirm the children disappeared. That distinction matters: the kill function and complete descendant drain are not the same public event.[^process-research-loky]

AgentLoom observed boundary: the preserved DBOS + loky Application probe passed close-blocked cancellation and close-failure cases, but failed root worker `os._exit(17)`: old native resource remained alive while the next Runtime started.[^loky-report] That probe used a native child in a separate session and killed a Worker, not the top-level executor while a library guardian survived. It rejects that integration mode; it is not a test of the newly confirmed failure scenario or every loky mode.

Not established: loky can be the separate guardian that survives top-level executor `SIGKILL` and cleans every task descendant. The failure does not prove loky has no useful mode, but it does reject a direct "wrap each Application in loky and call shutdown" answer.

### Pebble

No repo-pinned version or preserved validation was found in the current lockfile or prior research. Pebble is a process/thread pool library, but this round did not establish from fixed upstream source or tests that it provides a surviving guardian process with automatic full descendant cleanup after top-level executor `SIGKILL` on both macOS and Linux. Under the research discipline for this task, Pebble must remain "not established," not an inferred candidate.

### Pydantic AI durable execution

Documented guarantee/pattern: Pydantic AI says durable agents preserve progress across transient API failures, application errors, or restarts, and supports durable engines including Temporal, DBOS, Prefect, Restate, AWS Lambda, Kitaru, Airflow, and Absurd. Its DBOS integration says `DBOSDurability` routes model requests and MCP communication through DBOS steps when `agent.run()` is called inside a DBOS workflow.[^pydantic-overview] [^pydantic-dbos-doc]

Useful pattern: this supports the design principle already used by AgentLoom: keep the Agent normal, wrap durable units at the engine boundary, and avoid rewriting the Agent runtime around DBOS. Pydantic AI also states DBOS custom tools/event handlers may rerun if recovery happens before checkpointing, so side effects must be idempotent.[^pydantic-dbos-doc]

Not established: process-resource guardianship. The Pydantic AI docs are about durable engine integration and replay/checkpoint semantics. They do not promise that MCP stdio server processes, Node bridges, tool subprocess grandchildren, or detached local children are killed and drained after the Python executor is SIGKILLed.

## Why launchd/systemd are not the answer here

launchd and systemd/cgroup remain the strongest known OS-level mechanisms in the existing research. launchd's documented process-group cleanup and systemd/cgroup's control-group model are the reason the old DBOS+launchd product could pass the macOS NewsAgent acceptance.[^process-research-os] But the new requirement explicitly excludes user-configured launchd/systemd and separately managed service-style deployment as the default answer.

The key implication: **we cannot replace "AgentLoom wrote a launchd adapter" with "AgentLoom writes a systemd adapter" and call that a Python-library solution.** That would move the old design sideways, not satisfy the new selection constraints.

## Precise missing capability

The missing capability is a public Python library contract with all of these properties:

1. It starts each Application invocation inside a managed local process boundary on macOS and Linux.
2. A separate library-owned guardian/nanny process survives `SIGKILL` of the top-level executor.
3. The guardian knows the legitimate task resource boundary without AgentLoom maintaining an orphan registry or process supervisor.
4. It automatically terminates and waits for all descendants, including Pi/Node/MCP/tool grandchildren, before allowing replacement.
5. It works with local pip install and local persistent state, without a separately deployed broker/database/service by default.
6. Its behavior is documented or covered by upstream tests at fixed versions/commits.

None of DBOS 3.2.0, AnyIO 4.11.0/4.13.0, Trio, loky 3.7.0, Pebble, or Pydantic AI durable execution was shown to provide that full contract.

## Recommended next step

Keep DBOS as the durable execution candidate and continue the independent guardian selection under the user's confirmed requirements. Do not add a custom supervisor or weaken automatic cleanup to unblock selection. Before any product migration, define a focused acceptance test: start a real Application that creates Pi/Node/MCP/tool descendants and grandchildren, kill only the top-level executor Python process, and require automatic cleanup plus no replacement until the managed local resource boundary is empty. Passing normal cancellation, direct PID join, or DBOS logical terminal state should not count. No such new experiment was run in this research round.

[^executor]: `/Users/bytedance/code/AgentLoom-private/src/execution/durable/executor.py`, lines 69-114.
[^osjob]: `/Users/bytedance/code/AgentLoom-private/src/execution/durable/os_job.py`, lines 76-245.
[^scope]: `/Users/bytedance/code/AgentLoom-private/src/execution/process_scope.py`.
[^mcp]: `/Users/bytedance/code/AgentLoom-private/src/integrations/mcp/managed_stdio.py`; `/Users/bytedance/code/AgentLoom-private/src/integrations/mcp/client.py`, lines 62-69.
[^pi]: `/Users/bytedance/code/AgentLoom-private/src/runtimes/pi/environment.py`; `/Users/bytedance/code/AgentLoom-private/src/runtimes/pi/transport.py`, lines 66-78.
[^pyproject]: `/Users/bytedance/code/AgentLoom-private/pyproject.toml`, lines 81-85.
[^dbos-product-status]: `/Users/bytedance/code/AgentLoom-private/docs/research/dbos-product-integration-status-20261014.md`.
[^durable-selection]: `/Users/bytedance/code/AgentLoom-private/docs/research/durable-open-source-selection-20261014.md`.
[^process-research-anyio]: `/Users/bytedance/code/AgentLoom-private/docs/research/upstream-process-resource-ownership-20261014.md`, section "AnyIO 和 Trio 解决直属进程的结构化生命周期"; cited upstream source includes AnyIO 4.11.0 source `https://github.com/agronholm/anyio/blob/4.11.0/src/anyio/_backends/_asyncio.py#L1066-L1097`.
[^process-research-trio]: `/Users/bytedance/code/AgentLoom-private/docs/research/upstream-process-resource-ownership-20261014.md`, same section; cited upstream Trio source `https://github.com/python-trio/trio/blob/4630d02872c0aa5778ce0afd702c5d532a2ccf60/src/trio/_subprocess.py#L606-L635`.
[^process-research-loky]: `/Users/bytedance/code/AgentLoom-private/docs/research/upstream-process-resource-ownership-20261014.md`, section "loky 3.7.0 更贴近完整 Application 隔离"; cited upstream source `https://github.com/joblib/loky/blob/935c2922ab80f0f269cb18de0d1c5c35374ae1e0/loky/backend/utils.py#L16-L51`.
[^loky-report]: `/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-loky-application-go-no-go-20261014-v1/REPORT.md`.
[^pydantic-overview]: Parent verified published Pydantic AI v2.56.0 and fetched its durable execution overview: https://github.com/pydantic/pydantic-ai/blob/v2.56.0/docs/durable_execution/overview.md. The originally supplied v1.0.0 path returned 404 and was discarded.
[^pydantic-dbos-doc]: Parent verified Pydantic AI v2.56.0 DBOS integration docs: https://github.com/pydantic/pydantic-ai/blob/v2.56.0/docs/durable_execution/dbos.md. Lines 50-57 establish DBOSDurability and custom-tool wrapping boundaries. This is a reference pattern, not an installed AgentLoom dependency or evidence of compatibility with its pinned DBOS version.
[^process-research-os]: `/Users/bytedance/code/AgentLoom-private/docs/research/upstream-process-resource-ownership-20261014.md`, sections "Supervisor、Huey 和 launchd 管理的范围" and "Linux cgroup 提供跨祖先退出的资源边界".
