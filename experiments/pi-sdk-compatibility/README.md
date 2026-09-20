# Ticket 01: published Pi SDK compatibility proof

This isolated experiment exercises **`@earendil-works/pi-coding-agent@0.79.4` from npm**.
It does not import the local upstream `pi/` checkout, register a production runtime,
or implement AgentLoom's production Tool Gateway. Python is a governance/journal
fixture. Pi owns the actual AgentSession, agent loop, native provider, official
tools, session persistence, and automatic compaction.

## Reproduce

From this directory, with Node >= 22.19.0 and Python 3:

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm run verify
```

`verify` checks the installed SDK/registry/lock agreement and every dependency's
registry URL and integrity, runs TypeScript checking and all 13 SDK tests, and
writes `evidence/result.json`. It exits nonzero on any failed check. The report
includes output, versions, executable, source file hashes and a combined source
SHA-256. `RESULT.json` records the authoring run; reruns write separate evidence.
`gitHeadAtRun` is the checkout HEAD at execution time, while the source hashes
identify the tested working files (including any changes not yet committed).

Tests use private temporary directories, loopback HTTP, local Python and Bash.
The recovery tests intentionally SIGKILL their **own child Node process**; they do
not kill the test runner or another application. Successful and failed tests clean
their private directories. No model credentials or remote model requests are used.
The npm manifest's minimum Node version is upstream's requirement; the exact
tested Node/OS/Python versions are in the result, not an assertion of every host's
compatibility.

## Evidence and adopted entry points

| Ticket criterion | Actual evidence | Entry point / conclusion |
| --- | --- | --- |
| Published artifact and full lock | `npm-distribution.json`, `npm-pi-transitives.json`, `package-lock.json`, verifier | `npm ci`; all resolved dependencies have registry URLs and integrity |
| Invalid raw input repaired before execution | `hooks.test.mjs`: numeric path/content become `7.txt` / `42`; actual official write file equals `42` | Async `message_end` replacement, then a non-coercing schema check |
| Strict schema retained | The provider receives required string path/content; strict-generation case additionally asserts `strict: true`, `additionalProperties: false` | Public `before_provider_request` can explicitly opt this write schema into strict generation |
| Failure/denial/identity/order | Hook deny, Python failure, missing Hook, tampered args, unexpected field, mixed write/read/deny batch and repeated ID tests | Separate `tool_call` gate plus one-use `execute` gate; all correlate task/Run/session/call/tool |
| Native provider/tools/automatic compaction | Native OpenAI completions SSE receives only scripted model responses; official write/read/bash execute; threshold compaction persists its native summary and uses it next turn | `ModelRegistry.registerProvider`, `createAgentSession`, public tool factories and in-memory SettingsManager |
| Host committed, native not persisted | Child runs official Bash append, Python fsyncs journal, child SIGKILLs before returning result; session file has assistant call and no toolResult | `SessionManager.open` + validated `appendMessage` + a fresh `createAgentSession` with restored manager |
| Normal recovery and no duplicate effect | Continued provider request contains the restored tool result, append file still has one line, Hook/executor ran once; reapplying recovery appends zero entries | Native state can be repaired from committed host evidence before normal `session.prompt` continuation |
| Unsupported recovery fails explicitly | Uncertain/missing record, wrong session/task/args/native parent leave session file byte-identical | Never infer commitment or re-execute an uncertain operation |

The SDK API use is typechecked against the installed published declarations. The
provider fixture is an HTTP transport substitute, **not** an agent or tool loop.
The real SDK serializes requests, parses SSE, validates tools, executes tools,
updates history, and decides when to compact.

## Boundaries ticket 03 must preserve

1. `message_end` may asynchronously replace an assistant message before initial
   native tool validation. `tool_call` runs later and cannot repair initially
   invalid inputs by itself. Native `prepareArguments` is synchronous and cannot
   wait for Python IPC. Store original inputs in host evidence before replacement;
   native persisted assistant arguments become the final transformed arguments.
2. Pi catches `message_end` extension exceptions. An error observer is insufficient
   to enforce a blocking Hook. An explicit, one-use authorization bound to exact
   call identity, tool and final arguments is checked independently before any
   executor effect. Final schema validation must not coerce values: the native
   validator's normalization is not AgentLoom's strict decode contract.
3. The default OpenAI completions serializer sends `strict: false` even with
   `compat.supportsStrictMode: true`; that setting controls whether the field is
   emitted. The public request Hook can set it to `true`, as tested for official
   write's all-required schema. The test endpoint intentionally returns invalid
   arguments even then to exercise failure handling. This proves the request and
   execution boundary, not a real model's adherence to strict generation. Mapping
   optional/nullable fields for other tools remains a later provider decision.
4. Pi defaults to parallel tool execution. This experiment explicitly declares
   `executionMode: sequential` for dependent batches; all batch Hook decisions
   finish before tool execution begins. The test proves write/read/deny identities
   and order. It does not establish independent parallel native tool settlement.
   Pi's provider parser can coalesce duplicate IDs in a single malformed SSE
   response; the guard detects duplicate IDs that reach it and repeated IDs across
   turns. Do not equate provider raw fragments with native call identities.
5. Recovery here deliberately supports a **single-call final assistant turn** with
   a matching committed success (or its already-appended matching result). The
   fsynced host journal is independent of Pi JSONL, and native parent/call/name/
   args/session/task/Run must match before append. Resume uses normal
   `session.prompt` with a host continuation instruction after restoring the native
   result. Multi-call partial recovery, errors/blocks, alternate branches,
   cross-version state, cross-runtime conversion and a crash after an effect but
   before host commit are **not** proven here. Ticket 12 must implement its full
   journal and supported recovery policy; uncertain state must never auto-replay.
6. SDK resources are explicit: private cwd/agent directory; in-memory settings,
   auth and model registry; no discovered extensions, skills, prompt templates,
   themes or context files. Only the fixture extension and explicit tools are
   loaded. Production needs equivalent controlled resources and per-instance
   credentials.
7. The experiment's Python Hook and journal are intentionally fixtures. Their
   allow/deny modes, fixed task/Run names, one-call recovery, and sequential policy
   are not a replacement for tickets 03/05/06/09/12. SDK compatibility passing does
   not make Pi production-ready or count as Application-level acceptance.

## Enabled native output inventory

These are findings from the **installed npm distribution**, to guide later
artifact implementation; this ticket does not claim production artifact capture.

| Enabled tool | Query/operation boundary | Model display truncation | Public capture route |
| --- | --- | --- | --- |
| `write` | Exactly the authorized path/content | Short write confirmation; no read-output truncation | Retain authorized input and actual write result; optional `WriteOperations` wrapper delegates official write |
| `read` | Authorized path with optional offset/limit | Head capped at 2,000 lines or 50 KiB; explicit limit applies first | `ReadOperations.readFile` exposes bytes before presentation truncation; retain coverage/offset/limit rather than claiming unrequested ranges |
| `bash` | Authorized command/cwd/timeout | Tail capped at 2,000 lines or 50 KiB | Public `createLocalBashOperations`/`BashOperations.onData` for capture, or returned `details.fullOutputPath` when truncated; copy before cleanup |

`edit`, `grep`, `find` and `ls` are not enabled or validated by this PoC. In
particular, later search support must distinguish its query limit from display
truncation; matches never collected cannot be reconstructed from an artifact.

## Distribution detail

The root artifact integrity is
`sha512-PthzVzM5m4XH/hrU+2fVjuwuH5M4eMFWbd0NCRScH14XKpwlPc8/Fh6JDz0jQb5kTBT9oQT183YLTHVVulFL9A==`.
The published coding-agent shrinkwrap omitted integrity for its nested
`pi-agent-core`, `pi-ai`, and `pi-tui` packages, all at 0.79.4. Their exact tarballs
were downloaded from the npm registry and SHA-512 checked against registry
metadata. Those values are recorded in `npm-pi-transitives.json` and filled into
this experiment's lock. A subsequent `npm ci` successfully replayed the complete
lock. Regenerating with `npm install` may inherit those upstream omissions again;
the verifier fails closed if any dependency loses integrity. No upstream file or
repository-wide manifest/lock was changed.

Live provider smoke: **NOT-RUN**. All mandatory ticket 01 SDK probes use actual
SDK behavior with deterministic provider responses and pass; no production Pi,
Application/Worker integration, full recovery matrix, or installation profile is
claimed by this ticket.
