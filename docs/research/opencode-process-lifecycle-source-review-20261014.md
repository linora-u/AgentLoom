# OpenCode process lifecycle source review

Date: 2026-10-14

This note answers one narrow question: how OpenCode actually manages concurrency, persistent state, process cleanup, and recovery, and what that practice can or cannot simplify for AgentLoom. It is a source review, not a design selection.

## Sources and version boundaries

OpenCode's official repository is `anomalyco/opencode`. The local checkout at `/Users/bytedance/code/lumi/opencode` points to `git@github.com:anomalyco/opencode.git` and was inspected at commit `0a601cf334b9a83cc2854108a2b860f25e6e7e8e` (`fix(docs): correct Kimi K2.7 Code request limits (#38248)`, 2026-07-22). That checkout's `packages/opencode/package.json` reports version `1.18.4`.

For current upstream behavior, I shallow-cloned official snapshots into ignored research directories:

- `.agentloom/research/opencode-v2.0.28`, Git tag `v2.0.28`, commit `026b106bc5d41b679ffc413264229c340871e4d3`, dated 2026-10-13, with root `package.json` version `2.0.28`.
- `.agentloom/research/opencode-dev`, branch `dev`, commit `f1632d052de5ee0cffe28a34dc1a5346a6bf7136`, dated 2026-10-13.

The public npm registry reports `opencode-ai@1.19.0` as `latest`; the package name `opencode` itself returned 404. I therefore treat Git tag `v2.0.28` as the current official Git release source, and npm `opencode-ai@1.19.0` as the current npm stable channel. I do not collapse those into one “latest stable” claim.

Primary files read:

- `packages/core/src/session/execution.ts`
- `packages/core/src/session/execution/restart.ts`
- `packages/core/src/session/run-coordinator.ts`
- `packages/core/src/session/store.ts`
- `packages/core/src/session/inbox.ts`
- `packages/core/src/session/runner/llm.ts`
- `packages/core/src/shell.ts`
- `packages/core/src/mcp/stdio.ts`
- `packages/core/src/mcp/client.ts`
- `packages/core/src/pty.ts`
- `packages/core/src/persistent-pty/daemon.ts`
- `packages/server/src/process.ts`
- `packages/server/src/fetch.ts`
- `packages/server/src/routes.ts`
- related upstream tests under `packages/core/test`.

## Findings for AgentLoom

OpenCode does not prove that AgentLoom can drop durable execution or trust ordinary subprocess cleanup to solve host death. It proves a narrower and useful split:

1. For agent/session concurrency, keep coordination keyed by session ID and process-local until clustering is real.
2. For crash/restart, persist a small execution claim before the model/tool drain begins, and sweep unreleased claims on next managed-server boot.
3. For local child processes, put every spawned command/MCP server inside a scope-owned process group and kill that group on cancellation, timeout, or scope close.
4. Treat top-level host `SIGKILL` as a recovery problem, not a cleanup callback problem.

That is simpler than building a new queue lease scheduler or orphan registry. It is not the same as AgentLoom's desired “top-level Python kill -9 automatically reclaims all Pi/Node/MCP/tool descendants” physical process guarantee. OpenCode v2 handles host death mostly by durable continuation; only PTY has a deliberately independent helper process, and that helper is for preserving terminal sessions, not globally reaping every orphaned child.

## Concurrency model

OpenCode v2 uses `SessionRunCoordinator` as the local concurrency boundary. The coordinator serializes work per key while allowing different keys to run concurrently. Its comments describe one “busy period” per key: `run()` starts or joins the active execution, `wake()` rings a coalesced doorbell, and `interrupt()` marks the current execution as stopping. The implementation stores active executions in an in-memory `Map`, forks one Effect fiber per active key, and resolves joiners through a `Deferred`.

The upstream tests make the intended contract explicit:

- `packages/core/test/session-run-coordinator.test.ts` verifies same-key concurrent resumes join one drain.
- The same test file verifies two different keys can be active together.
- It verifies active execution cleanup after failure/defect and interruption races.

`SessionExecution` wires this coordinator to real sessions. In v2.0.28, `packages/core/src/session/execution.ts` calls `SessionRunCoordinator.make<SessionSchema.ID, ...>` and runs `SessionRunner.Service.drain({ sessionID, force, continuation, promotable })` through the selected instance. The code comments say this is process-local execution. It is not clustered ownership or a distributed lock.

This is directly relevant to AgentLoom: same-session single-flight plus cross-session concurrency is a small, well-bounded pattern. It does not require a general scheduler if the current product requirement is local execution with finite worker limits.

## Persistent state and restart recovery

The biggest v2 change from the local 1.18.4 checkout is the execution claim.

In `packages/core/src/session/execution.ts`, OpenCode writes execution intent and the started event in one transaction before the turn runs. A claim without a terminal event survives a crash or SIGKILL and becomes input to restart recovery. This recovery does not depend on a shutdown callback. [Fixed execution source](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/src/session/execution.ts#L81)

Terminal events release the claim, except shutdown interruption, which intentionally preserves it so the next server start resumes.

`packages/core/src/session/store.ts` exposes the durable operations behind that contract:

- `listSuspended()`: top-level sessions holding an execution claim.
- `claim(sessionID)`: records write-ahead execution intent.
- `release(sessionID)`: releases the claim and resets resume accounting.
- `countResume(sessionID)`: durably counts resume attempts for orphaned claims.

`packages/core/src/session/execution/restart.ts` implements the sweep. Its comments say suspended sessions are those whose claim was never released, including process death without teardown and graceful shutdown. It also states the core limitation:

> “Recovery is at-least-once: local coordination prevents concurrent drains, not repeated external side effects after a crash.”

The sweep assumes the old owner is dead. The managed-server protocol is expected to guarantee that by confirming the previous process is dead before a successor sweeps. The service is inert until invoked by the managed server or an embedder.

`packages/server/src/process.ts` invokes this during managed Node server boot: after building routes, it calls `installRestartContinuity(Context.get(context, SessionRestart.Service))`. `packages/server/src/fetch.ts` does the same for embedded fetch runtimes: after building the application layer, it forks `SessionRestart.Service.resumeSuspendedSessions`.

The practical takeaway is that OpenCode v2.0.28 adds durable claims and at-least-once restart continuation. AgentLoom already delegates durable execution and recovery to DBOS; this source pattern is a comparison, not a recommendation to add a second claim/sweep loop. Tool side effects still need idempotency or business-result reconciliation. [Fixed restart source](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/src/session/execution/restart.ts)

## Shell and child-process cleanup

OpenCode v2 shell commands are location-scoped resources. In `packages/core/src/shell.ts`, `Shell.create` spawns the selected shell through `Environment.Service.spawner` with:

- `stdin: "ignore"`
- `detached: process.platform !== "win32"`
- `forceKillAfter: Duration.seconds(3)`

The command's output is streamed to a file under a project-specific output directory. A long-lived Effect fiber holds the spawned handle's scope open until command completion. Timeout calls `handle.kill({ forceKillAfter: Duration.seconds(3) })`.

The process-group behavior is in the spawner layer, not in the shell tool itself. In the local 1.18.4 checkout this was `packages/core/src/cross-spawn-spawner.ts`: non-Windows defaults to `detached: true`; cleanup attempts to kill `-pid` as a process group and falls back to direct process kill. The v2 tests still validate that behavior in `packages/core/test/effect/cross-spawn-spawner.test.ts`:

- it kills a running process;
- scope exit kills the child;
- `forceKillAfter` escalates for a SIGTERM-ignoring process;
- one test deliberately shows that a grandchild holding stdio can keep running after foreground completion/SIGKILL while capture finishes. That test distinguishes command completion from “all descendants physically gone.”

This distinction matters for AgentLoom. OpenCode's shell cleanup is good local hygiene for owned process groups. It is not a proof that every descendant is gone after every possible parent death. If a descendant deliberately escapes the group or survives after foreground completion, OpenCode does not present a universal orphan-reaper guarantee.

## MCP stdio cleanup

The local 1.18.4 checkout used the upstream MCP SDK `StdioClientTransport`, plus an OpenCode finalizer that searched descendants with `pgrep -P`, sent `SIGTERM` to them, then called `client.close()`. That was better than nothing but was not a strong process-group contract.

OpenCode v2 has its own stdio transport in `packages/core/src/mcp/stdio.ts`. It launches MCP through the selected location's Environment spawner and acquires the handle in that location's scope. Protocol and process ownership therefore share an execution location, and scope shutdown can stop the process even if transport close was not called. [Fixed MCP transport](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/src/mcp/stdio.ts)

The implementation starts the server through `environment.spawner.spawn(ChildProcess.make(...))`, keeps stdin open through a queue, drains stdout/stderr, and defines `close()` to stop the handle. Stop waits briefly for natural exit, then sends `SIGTERM`, then escalates to `SIGKILL` if needed.

The upstream test `packages/core/test/mcp.test.ts` includes `terminates MCP descendants after the wrapper exits successfully`, specifically covering a wrapper that exits while its descendant ignores SIGTERM and retains pipes. This is a clear improvement over the 1.18.4 descendant scan.

For AgentLoom, this suggests a minimal seam: do not let MCP subprocesses be an ad hoc SDK side effect if location/worker ownership matters. Wrap stdio transport creation in the same process-spawner/scope mechanism as shell commands.

## PTY lifecycle

OpenCode has two PTY concepts.

The ordinary PTY service in `packages/core/src/pty.ts` is scope-owned. It stores active PTYs in an in-memory map, kills running sessions in its finalizer with `session.process.kill()`, and retains a bounded number of exited sessions for observability. That is normal cleanup, not crash survival.

OpenCode v2 also has a persistent PTY daemon in `packages/core/src/persistent-pty/daemon.ts`. It can spawn an `opencode-pty` helper with `{ detached: true, windowsHide: true, stdio: "ignore" }`, then discover and claim it through a runtime directory and `service.json`. The daemon owns terminal state and supports handoff/attach semantics.

This is the one place where OpenCode intentionally allows an independent helper process. Its purpose is preserving and transferring PTY sessions across client/server lifetime boundaries. The upstream test `packages/core/test/persistent-pty-daemon.test.ts` also shows it rejects incompatible daemons without replacing or killing them.

That maps only partially to AgentLoom's desired process contract. A PTY daemon is an allowed “third-party/helper may survive” style component, but it is not a generic worker-child reaper. Reusing this idea would mean accepting a small established helper for a specific resource class, not adding a broad AgentLoom supervisor.

## HTTP/background server lifetime

OpenCode's v2 server process is scope-owned. `packages/server/src/process.ts` binds a Node HTTP server, serves the Effect HTTP app, and registers finalizers that:

- begin stopping status,
- remove the active application reference,
- call `server.closeAllConnections()`,
- close the server scope and lifecycle cleanup.

`packages/server/src/fetch.ts` is the embedded-runtime variant. It explicitly says the embedder owns lifecycle and must close the Effect scope to release the application layer. It also says sessions with unreleased execution claims resume after layer build.

So OpenCode has two server lifetime models:

- managed Node server: OpenCode owns the listener and install restart continuity;
- embedded fetch handler: the embedder owns listener/scope, OpenCode provides route layer and restart sweep.

Neither model turns top-level `SIGKILL` into a cleanup callback. Both rely on either scope closure for normal shutdown or durable claims for post-death continuation.

## Top-level SIGKILL, worker death, and cancellation are separate cases

OpenCode's source distinguishes these cases:

- **Normal cleanup**: closing an Effect scope runs finalizers. Shell/MCP handles are killed through their process-group-aware spawner. HTTP server scopes close connections. Ordinary PTYs are killed.
- **Task cancellation or timeout**: shell timeout/abort calls handle kill with escalation. Session interruption interrupts the owning Effect fiber; in v2 it can release or preserve claims depending on reason.
- **Worker/server graceful shutdown**: v2 shutdown interruption preserves the execution claim, so restart continuity can resume.
- **Top-level unexpected process death or SIGKILL**: no finalizer can run. v2 treats the unreleased claim as the durable signature and resumes on the next boot. It does not promise that all old OS descendants are physically gone.
- **Future/Effect completion**: tests and implementation treat handle exit, stream capture completion, and descendant cleanup as separate observations. The held-stdio tests are explicit evidence.

This separation is the main first-principles lesson for AgentLoom. If the desired property is “no business work is silently lost,” use a durable claim/sweep. If the desired property is “every OS descendant disappears when the top-level Python process is kill -9’d,” a normal library scope cannot guarantee it. That latter property needs an OS/session/container-owned boundary or a small independent guardian that survives long enough to reap, which is a different requirement.

## Dependencies doing the work

OpenCode does not implement a grand scheduler. It composes smaller primitives:

- Effect `Scope`, `FiberSet`, `Deferred`, `Layer`, finalizers, and scoped resources own normal lifecycle.
- SQLite/Drizzle-backed store and event projections hold session state and execution claims.
- `SessionRunCoordinator` is project code, but it is a small keyed single-flight/doorbell coordinator.
- The environment spawner / cross-spawn layer owns child-process handles, detached process groups, and escalation.
- MCP SDK is still used for protocol/client behavior, but v2 wraps stdio transport process ownership in OpenCode's own environment spawner.
- Node HTTP server / Effect HTTP own serving; OpenCode adds scope finalizers and forced connection close.
- `opencode-pty` is an independent helper for persistent PTYs.

## Known gaps relative to AgentLoom's target

OpenCode v2 is closer to AgentLoom's desired direction than the July checkout, but the gaps are material:

1. It gives at-least-once execution recovery, not exactly-once execution.
2. It does not guarantee that every descendant of a killed top-level host process is physically reaped.
3. Its process cleanup works best for children spawned through the OpenCode environment spawner. Anything outside that seam is outside the contract.
4. Persistent PTY deliberately permits an independent daemon; that is resource-specific, not a universal process owner.
5. The managed-server recovery sweep assumes a protocol that confirms the previous owner is dead before a successor sweeps. AgentLoom would need an equivalent local boot/registration rule if it adopts this pattern.

## What can simplify AgentLoom

The simplest useful borrowing is:

- keep DBOS/SQLite for durable admission and result state where AgentLoom already has it;
- use DBOS's existing durable workflow/step state and recovery for in-flight work; do not add a parallel execution-claim/sweep scheduler merely because OpenCode implements one;
- route Pi/Node/MCP/tool subprocess creation through one scoped spawner that starts a new process group and kills that group on timeout/cancel/scope close;
- accept that host `SIGKILL` recovery and process-tree cleanup are separate: recover work from durable claims; reclaim OS children by using a library/helper/OS boundary, not by hoping Python finalizers run.

This answers the user's suspicion: yes, a simpler design is possible if we stop trying to make one mechanism satisfy every property. OpenCode's practice argues for a small number of narrow contracts, each with a clear failure boundary. It does not argue for Ray, a large new scheduler, launchd-only execution, or a custom orphan registry inside AgentLoom.

## Clickable primary-source index

- [v2.0.28 release tag](https://github.com/anomalyco/opencode/releases/tag/v2.0.28); npm `opencode-ai` latest remained 1.19.0 when checked. Version 2.0.28 was not present under that npm package; it must not be presented as the npm default release.
- [SessionRunCoordinator](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/src/session/run-coordinator.ts), [coordinator tests](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/test/session-run-coordinator.test.ts).
- [execution store](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/src/session/store.ts), [managed server process](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/server/src/process.ts), [embedded fetch lifetime](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/server/src/fetch.ts).
- [shell scope](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/src/shell.ts), [spawn/control tests](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/test/effect/cross-spawn-spawner.test.ts).
- [MCP descendant cleanup test](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/test/mcp.test.ts#L794), [MCP client](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/src/mcp/client.ts).
- [ordinary PTY](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/src/pty.ts), [persistent PTY daemon](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/src/persistent-pty/daemon.ts), [daemon tests](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/test/persistent-pty-daemon.test.ts).

Parent reviewed the critical claims against the fixed snapshot. No tests were run in this research round, so upstream test code is coverage evidence, not an observed pass in this environment.
