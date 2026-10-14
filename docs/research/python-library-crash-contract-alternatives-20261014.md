# Python library crash-contract alternatives

2026-10-14. Scope: research only. I did not change product code, dependencies, configs, GLOSSARY, ADRs, run model/News/AnySearch traffic, commit, or push.

## Decision

No reviewed mode of these mature Python libraries was established to satisfy the whole conjunction:

1. pip-installable local operation on macOS and Linux;
2. local persistent execution state, SQLite by default, with a plausible future remote backend;
3. no user-configured launchd/systemd and no default external broker/database/service;
4. public supported APIs, not private SDK table adapters;
5. no new AgentLoom queue/claim/lease/retry scheduler, orphan registry, or custom supervisor;
6. after **only the top-level executor Python process is SIGKILLed**, a separate library-owned guardian/nanny that remains alive automatically cleans Pi/Node, MCP, tool subprocesses, and grandchildren, then exits itself. Descendants must remain in the managed scope; deliberate escape is excluded from this acceptance case.

The decisive gap is resource ownership after executor death. Prefect and Celery can manage workflow/task state; Dask has a real Nanny; loky/Pebble/Billiard can manage worker processes while their parent infrastructure is alive. None exposes a stable local API that turns an arbitrary AgentLoom Application and all of its external subprocesses into a cross-platform, recursively drained scope after the executor is killed.

The finding is a capability gap in the reviewed modes. It does not establish a universal impossibility across all public Python libraries. A process library does not itself need SQLite: it can be composed with DBOS, provided that the composition meets the process-lifetime contract without a new scheduler or supervisor.

## Hard constraints versus candidates

| Candidate | Local persistent execution state | Separate guardian/nanny survives executor SIGKILL | Recursive cleanup of arbitrary Application subprocesses and grandchildren | Public stable API suitable for this contract | Verdict |
| --- | --- | --- | --- | --- | --- |
| Prefect 3.8.8 Process worker / runner | Yes, via self-hosted server; SQLite default is documented for lightweight single-server deployments. | A worker/runner can monitor cancellation and infrastructure metadata, but this is not a generic Application guardian after the executor process is killed. | Not established. Internal process manager can signal a process group when configured, but this is private runner plumbing and does not own Agent-created Pi/MCP/tool descendants by default. | Partial. Server/work-pool APIs are public; the useful process-group pieces are in `prefect.runner._*` private modules. | Keep only as execution-control reference; not a crash-contract answer. |
| Celery 5.6.3 + Billiard 4.2.4 | Requires broker; SQLite is not the default durable broker model. | Linux has parent-death signaling for pool children; no equivalent cross-platform descendant guardian was established. | Not established for arbitrary Pi/MCP/tool grandchildren. Billiard termination targets worker PIDs/jobs. | Public task APIs exist, but the required cleanup contract is absent. | Excluded under the current deployment and crash requirements. |
| Dask Distributed 2026.8.0 Nanny | No SQLite durable task state by default; a DBOS composition remains possible in principle. | Watches Dask Workers; this does not prove that an independent guardian survives a killed LocalCluster host. | Not established for arbitrary Application subprocesses spawned inside tasks. | Public Nanny/Worker APIs exist, but the contract is Worker management, not complete AgentLoom process-tree ownership. | Guardian candidate with an unproven resource boundary. |
| loky 3.7.0 | No durable queue/state; process pool only. | No once top-level executor is SIGKILLed. Existing manager thread dies with the executor. | Source can kill a worker's child tree while parent infrastructure is alive, but prior Application probe showed worker crash can orphan native child. | Public `ProcessPoolExecutor.shutdown(kill_workers=True)` exists; recursive cleanup details are source-level, not a full crash contract. | Useful for live-parent cleanup only. |
| Pebble 5.2.3 | No durable queue/state; process pool only. | No separate durable guardian for top-level executor death. | Source force-stops worker processes; arbitrary grandchildren are not the documented contract. | Public pool API exists, but no required guardian semantics. | Excluded. |
| Ray 2.59.0 | `ray.init()` can start a local runtime; DBOS can remain the persistence layer. | macOS uses a reaper; default Linux uses kernel fate sharing that kills raylet with the host. | POSIX per-worker group cleanup exists, but complete cleanup after top-level host death is not established. | Public local task/actor APIs exist. Size alone is not a user requirement or a reason to exclude it. | Worth focused review; not yet accepted. |

## Prefect 3.8.8

Documented guarantee:

- Prefect server is backed by a database. The server docs state SQLite is the default and is recommended for lightweight, single-server deployments; PostgreSQL is best for production, HA, and multi-server deployments. That matches part of the local persistence requirement but implies a running server/control plane, not just an embedded library call. Source: [`docs/v3/concepts/server.mdx` at tag `3.8.8` / commit `b73b09dc3699489c799dbb40ac5ed2da459e391c`](https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/docs/v3/concepts/server.mdx).
- Workflow cancellation requires a deployment and a monitoring process. The worker sees the Cancelling state, sends a signal to the flow-run infrastructure, and kills infrastructure after a grace period. Prefect stores an `infrastructure_pid`/identifier for restart-resilient cancellation. Source: [`docs/v3/advanced/cancel-workflows.mdx`](https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/docs/v3/advanced/cancel-workflows.mdx).

Source inference:

- `EngineCommandStarter` has `isolate_process_group=False` by default and sets `start_new_session=True` only when isolation is explicitly requested. Source: [`src/prefect/runner/_starter_engine.py`](https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/runner/_starter_engine.py).
- `ProcessManager` and `PosixProcessGroupScope` can check and signal a process group using `os.killpg`. This is meaningful process-control machinery, but it lives under private `prefect.runner._*` modules. Source: [`src/prefect/runner/_process_manager.py`](https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/runner/_process_manager.py).
- The public `ProcessWorker` runs process-based flow infrastructure, but the reviewed implementation does not establish ownership of arbitrary subprocesses spawned inside AgentLoom Application logic. Source: [`src/prefect/workers/process.py`](https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/workers/process.py).

Prior local evidence, within its actual scope:

- `.agentloom/validation/prefect-application-go-no-go-20261014-v1/REPORT.md` tested the ordinary Process worker route wrapping an Application-style process. It observed replacement beginning while the previous native child was still alive. This invalidates that integration mode. It does not prove every possible Prefect integration is impossible.

Not established:

- A public Prefect API that creates a separate library-owned guardian for each AgentLoom Application and recursively drains Pi/Node/MCP/tool grandchildren on macOS and Linux after the top-level executor is SIGKILLed.
- A supported way to make Prefect's private process-group scope own subprocesses created by MCP SDK transports and runtime tools that may deliberately create their own sessions.

Prefect is therefore a reasonable source of ideas for state and cancellation metadata, but using it to satisfy this task would require either relying on private runner internals or adding AgentLoom-owned resource tracking/supervision. Both violate the brief.

## Celery 5.6.3 and Billiard 4.2.4

Documented guarantee:

- Celery's task docs say the default behavior acknowledges a task before execution; `acks_late` moves acknowledgment after return for idempotent tasks. The same docs state that even with `acks_late`, a child process terminated by `sys.exit` or signal will still acknowledge the message unless `task_reject_on_worker_lost` is enabled. Source: [`docs/userguide/tasks.rst` at Celery `v5.6.3` / commit `3f4d8d795ad128bd7430cc5dc174a802cded425c`](https://github.com/celery/celery/blob/3f4d8d795ad128bd7430cc5dc174a802cded425c/docs/userguide/tasks.rst).
- Celery's worker docs directly contradict the required crash contract: when a worker is force-killed with `KILL`, "the worker will not be able to reap its children; make sure to do so manually." Source: [`docs/userguide/workers.rst`](https://github.com/celery/celery/blob/3f4d8d795ad128bd7430cc5dc174a802cded425c/docs/userguide/workers.rst).

Parent source review qualification: the same file, lines 100-102, documents Linux support since 5.2 for `PR_SET_PDEATHSIG`. The manual-cleanup sentence alone cannot establish that Linux pool children always survive. This feature still does not establish recursive Pi/MCP/tool cleanup on both macOS and Linux.

Source inference:

- Celery request execution wires task success/failure and late acknowledgment decisions around child task execution, not around local process-tree drain. Source: [`celery/worker/request.py`](https://github.com/celery/celery/blob/3f4d8d795ad128bd7430cc5dc174a802cded425c/celery/worker/request.py).
- Billiard's `Pool.terminate_job(pid, sig)` resolves a process by PID and sends a signal to that PID. It marks the job as controlled/terminated; it does not recursively enumerate and wait on arbitrary grandchildren. Source: [`billiard/pool.py` at Billiard `v4.2.4` / commit `69c2f296c2b36622a90c548f26c940712cb9a1df`](https://github.com/celery/billiard/blob/69c2f296c2b36622a90c548f26c940712cb9a1df/billiard/pool.py).
- Billiard `BaseProcess.terminate()` delegates to the process popen terminate. Source: [`billiard/process.py`](https://github.com/celery/billiard/blob/69c2f296c2b36622a90c548f26c940712cb9a1df/billiard/process.py).

Not established:

- Any public Celery/Billiard mode that runs without a broker by default, stores local state in SQLite by default, and provides a surviving guardian to recursively clean arbitrary local subprocesses after the executor is SIGKILLed.

Celery/Billiard has not established the required cross-platform recursive cleanup contract, and its default broker deployment also conflicts with the local deployment requirement.

## Dask Distributed 2026.8.0

Documented guarantee:

- Dask workers are by default launched, monitored, and managed by a small Nanny process. Source: [`docs/source/worker.rst` at distributed tag `2026.8.0` / peeled commit `0a341df4782bc358a074fb12fe7ce277fe2313ce`](https://github.com/dask/distributed/blob/0a341df4782bc358a074fb12fe7ce277fe2313ce/docs/source/worker.rst).
- The "Why did my worker die?" docs say the Nanny watches the worker and restarts it when necessary, including memory-limit restarts. Source: [`docs/source/killed.rst`](https://github.com/dask/distributed/blob/0a341df4782bc358a074fb12fe7ce277fe2313ce/docs/source/killed.rst).

Source inference:

- `Nanny.kill()` delegates to its `WorkerProcess.kill()` and blocks until the process is down and scheduler is informed. Source: [`distributed/nanny.py`](https://github.com/dask/distributed/blob/0a341df4782bc358a074fb12fe7ce277fe2313ce/distributed/nanny.py).
- Dask Worker docs state computations run in a `ThreadPoolExecutor` in the same worker process by default, and multiple worker processes can be launched for process-level parallelism. Source: [`distributed/worker.py`](https://github.com/dask/distributed/blob/0a341df4782bc358a074fb12fe7ce277fe2313ce/distributed/worker.py).

Not established:

- SQLite-default durable task state under the same semantics as AgentLoom's per-worker queues.
- A Dask public API that makes the Nanny own subprocesses and grandchildren spawned by task code and third-party MCP/Pi transports while they remain in the managed scope.
- A guarantee that killing the submitting top-level executor leaves a Nanny that drains all task-created Pi/Node/MCP/tool descendants and then exits.

Dask has a real Nanny. Its Worker lifecycle contract does not by itself prove complete Application resource ownership. Pairing it with DBOS would still leave the subprocess boundary unproven; lacking its own SQLite persistence alone does not disqualify that composition.

## loky, Pebble, and the process-pool family

Documented/source-backed facts:

- Prior research already fixed loky at 3.7.0 and found `ProcessPoolExecutor.shutdown(wait=True, kill_workers=True)` can trigger process-tree killing while the executor machinery is alive. It does not provide durable state or a separate guardian after the top-level executor dies. Existing Application probe results were 2/3, with worker crash leaving native child alive at replacement time. Source: [`docs/research/upstream-process-resource-ownership-20261014.md`](upstream-process-resource-ownership-20261014.md), `.agentloom/validation/dbos-loky-application-go-no-go-20261014-v1/REPORT.md`.
- Pebble 5.2.3 `ProcessPool.stop()` closes channels and force-stops worker processes. Source: [`pebble/pool/process.py` at tag `5.2.3` / peeled commit `b368bf5a265ca1bf9a758bc83ea8a60fbdd9862c`](https://github.com/noxdafox/pebble/blob/b368bf5a265ca1bf9a758bc83ea8a60fbdd9862c/pebble/pool/process.py).

Not established:

- Durable queue state with SQLite defaults.
- A surviving library-owned guardian after executor SIGKILL.
- Recursive cleanup of subprocesses that no longer have a live worker parent or were deliberately moved into another process group/session.

These libraries can improve live-parent cleanup, timeouts, and worker isolation. They do not solve top-level executor death under the brief's constraints.

## Ray

The initial review used Ray 2.9.3, which is too old to assess the current process-group feature. Parent review instead fixed Ray 2.59.0: [process configuration](https://github.com/ray-project/ray/blob/ray-2.59.0/src/ray/common/ray_config_def.h) enables per-worker process-group cleanup by default on POSIX. A locally started runtime is permitted by the user; Ray's footprint is an integration cost, not a disqualifying requirement.

The remaining issue is the top-level host failure path: [Node](https://github.com/ray-project/ray/blob/ray-2.59.0/python/ray/_private/node.py) selects kernel fate sharing on Linux, and [utils](https://github.com/ray-project/ray/blob/ray-2.59.0/python/ray/_private/utils.py) requests parent-death SIGKILL. On macOS, the [reaper](https://github.com/ray-project/ray/blob/ray-2.59.0/python/ray/_private/ray_process_reaper.py) signals its own group, then kills that group after one second. Per-worker groups are separate; [NodeManager::Stop](https://github.com/ray-project/ray/blob/ray-2.59.0/src/ray/raylet/node_manager.cc) attempts group cleanup, but sends signals without a final group-empty wait. These source paths do not establish that a surviving guardian always completes all group cleanup before exiting after the host is killed. See the dedicated [process-library assessment](python-library-process-ownership-dask-ray-pebble-20261014.md) for upstream-test coverage and public API boundaries.

## Current AgentLoom facts that matter

These are product facts, not a proposed answer:

- Current durable code is macOS launchd-specific. `LaunchdJobs.__init__` raises on non-Darwin platforms. Source: [`src/execution/durable/os_job.py`](../../src/execution/durable/os_job.py).
- Current durable executor combines DBOS 3.2.0 queues with `LaunchdJobs`. Source: [`src/execution/durable/executor.py`](../../src/execution/durable/executor.py).
- Current `process_scope.py` is AgentLoom-owned. It decides whether children start a new session while an isolated Application process is bound. Source: [`src/execution/process_scope.py`](../../src/execution/process_scope.py).
- Current subprocess cleanup uses AgentLoom's own token and psutil scan logic. Source: [`src/execution/process.py`](../../src/execution/process.py).

Those facts can be useful implementation context, but they cannot satisfy the user's "reuse mature third-party Python libraries and public APIs; no custom supervisor/orphan registry" requirement. Treat them as existing coupling to remove or replace, not as evidence that a third-party library already solves the problem.

## What would have to change for a candidate to pass

A candidate would need to document all of the following as public, supported behavior:

- A library-owned guardian/nanny process can be started from Python on macOS and Linux without user-configured launchd/systemd or an external broker/database/service by default.
- The guardian's resource boundary includes arbitrary subprocesses and grandchildren created by the Application, including Node/Pi bridges, MCP stdio processes, and shell/tool descendants.
- If the submitting executor Python process is SIGKILLed, the guardian still detects owner death, terminates the entire managed local resource boundary, completes cleanup and exits without manual intervention or waiting for executor restart.
- Public APIs expose sufficient cleanup completion information for the DBOS adapter to prevent replacement before old resources exit. The process library need not own the queue, SQLite state or business receipt itself.

None of the reviewed candidates currently documents that contract.

## Source notes

- Fixed versions/commits checked: Prefect 3.8.8 `b73b09dc3699489c799dbb40ac5ed2da459e391c`; Celery 5.6.3 `3f4d8d795ad128bd7430cc5dc174a802cded425c`; Billiard 4.2.4 `69c2f296c2b36622a90c548f26c940712cb9a1df`; Dask Distributed 2026.8.0 peeled commit `0a341df4782bc358a074fb12fe7ce277fe2313ce`; Pebble 5.2.3 peeled commit `b368bf5a265ca1bf9a758bc83ea8a60fbdd9862c`. Parent source review added Ray release tag `ray-2.59.0`, replacing the obsolete 2.9.3 selection basis.
- Celery and Dask hosted docs returned 403/404 for some pages in this environment, so I used the same first-party documentation files from their official GitHub repositories at fixed commits.
- No new execution experiment was run. Prior validation reports are cited only for the integration mode they actually tested.
