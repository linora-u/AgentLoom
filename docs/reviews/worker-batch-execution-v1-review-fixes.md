# Worker batch review and corrective evidence

Recorded 2026-10-11 for [#110](https://github.com/linora-u/AgentLoom/issues/110).
The two independent reviews froze `4bd71748dc05d6af0e114b10b947ee12e1b9c55f` against
`bc5cff6e61abf2a6d7ba7b0b6d2efac210463659`. Their separate axes are preserved below;
corrective evidence follows without combining or reranking findings.

Corrective product/test commit: `0e60af42`. Integration acceptance through `4a211d8d`
was merged into this implementation branch at `8bd117dcef426a2b40f42289e69dea037d0c7bae`. This is corrective
implementation evidence, not final delivered-revision acceptance.

## Standards

Reviewed `bc5cff6e61abf2a6d7ba7b0b6d2efac210463659...4bd71748dc05d6af0e114b10b947ee12e1b9c55f`.

- **Documented-standard violation [P2]: configuration reverse-loads execution.** [system_loader.py:76-80](/Users/bytedance/code/AgentLoom-private/src/config/system_loader.py:76) adds `from agentloom.execution.concurrency.policy import validate_worker_execution` unconditionally, even when the project has no Worker policy. This violates the configuration ownership/no-reverse-import rule in [test_architecture_boundaries.py:179-216](/Users/bytedance/code/AgentLoom-private/tests/test_architecture_boundaries.py:179). A fresh-process public-loader probe showed zero execution modules before loading and ten afterward, including `batch_models`, `legacy`, `parallel_executor`, and `rate_limiter`. The import-only architecture check never calls the loader; its AST counterpart still checks the retired `configuration` directory name at line 207. Move source-section validation beside `WorkerExecutionSettings` in [config_validation.py:364-379](/Users/bytedance/code/AgentLoom-private/src/config/config_validation.py:364); retain capacity resolution in execution and verify the public load boundary.

- **Possible Duplicated Code — judgement, not a hard violation.** [application.py:325-349,392-430](/Users/bytedance/code/AgentLoom-private/src/app/application.py:325) and [runner.py:231-236,598-634](/Users/bytedance/code/AgentLoom-private/src/app/runner.py:231) independently compose root isolation and the same drain/finalize/resource-close sequence. Repeated hunks include `isolated_checkpoint_context()`, `bind_task_context_engine(None)`, `bind_goal_state_provider(None)`, `lifecycle.finalize_run(` and `lifecycle.close_resources(`. The two entrypoints need different initialization and result adapters, but duplicating the ownership ordering means adding another root service or fixing teardown requires synchronized edits. Consider extracting only the shared root-service binding and teardown transaction into the lifecycle owner.

No additional documented-standard breaches identified. Tooling-enforced style/type checks and pre-existing defects excluded.

Standards: **1 hard finding; 1 judgement finding.** Worst hard issue: config-to-execution dependency inversion.

## Spec

Contract: [worker-batch-execution-v1.md](/Users/bytedance/code/AgentLoom-private/docs/specs/worker-batch-execution-v1.md) (S). Reviewed HEAD `4bd71748` against `bc5cff6e`.

Apparently implemented but incorrect:

- **[P1] Original control exceptions are masked.** [batch_runner.py:258](/Users/bytedance/code/AgentLoom-private/src/execution/concurrency/batch_runner.py:258) and [application.py:494](/Users/bytedance/code/AgentLoom-private/src/app/application.py:494). S:137: “Control exceptions also stop/drain before propagation”; S:179: “stop/drain and propagate for the second”. Public probes reproduce observer KeyboardInterrupt plus required-write OSError, and Worker KeyboardInterrupt plus close OSError: both drain but raise WorkerBatchError instead of the original signal.

- **[P1] Broken stop observation prevents drain.** [batch_runner.py:67](/Users/bytedance/code/AgentLoom-private/src/execution/concurrency/batch_runner.py:67). S:137: “Stop further work, preserve evidence, drain what can finish and propagate the structured batch failure.” An accepted token whose is_set() raises OSError is repeatedly called even after internal stop; map and owner exit never return. Independent watchdog terminated the zero-start public probe after eight seconds.

- **[P2] Startup stop observation is not latched.** [batch_runner.py:150](/Users/bytedance/code/AgentLoom-private/src/execution/concurrency/batch_runner.py:150). S:134: “A stop token stops admission”; S:124: “a stop observed during execution makes it stopped”. A public token true once at startup then false yields completed with [canceled, success], and the second input actually starts.

Missing/partial requirements:

- **[P2] Maintained regression callers retain retired configuration.** [mixed_runtime_support.py:149](/Users/bytedance/code/AgentLoom-private/tests/application_test/mixed_runtime_support.py:149). S:145: “Update affected maintained examples, documentation and caller configuration together.” Its Supervisor concurrency:3 prevents Runtime startup. Two mixed-runtime cases still fail at HEAD; the earlier broad run records 62 associated configuration/recovery failures.

- **[P2] Caller acceptance depends on collection order.** [test_skill_builder.py:22](/Users/bytedance/code/AgentLoom-private/applications/repo_map/tests/test_skill_builder.py:22). S:194–195: “Reuse those behavioral fixtures and extend the high-level Application contract”. Independent repo_map collection now fails with missing tests.application_test; loading legacy tests first hides it, leaving standalone migration acceptance broken.

- **[P1] Final delivery acceptance remains open.** [acceptance.md:121](/Users/bytedance/code/AgentLoom-private/docs/reviews/worker-batch-execution-v1-acceptance.md:121). S:197–198: “Completion requires the acceptance matrix, supported-version checks and both Runtime Application validations on the delivered revision.” The report explicitly leaves fixes and final bounded live Pi/smol Provider validation to F; historical successes do not close that gate.

Unrequested behavior: no additional scope creep established.

## Closure evidence

### Standards closures

- Configuration reverse-import: source/scope validation now belongs beside
  `WorkerExecutionSettings` in `config_validation`; execution retains the single
  precedence resolver and its compatible validation export. A fresh process calls
  the public `load_project_system_config` with capacity twenty and verifies that
  no execution module loads. The AST guard scans the actual `config` directory.
  Both new checks failed before the repair; all seventeen architecture checks pass.
- Root ownership duplication (judgement accepted): both public entrypoints use
  `ApplicationRunLifecycle.isolate_root_services` and `finish_root_run`. Only root
  isolation and drain/finalize/resource/engine teardown ordering moved. Their
  initialization and result adapters stay separate. A drain failure after normal
  completion uses the finalization state, still closes every root owner, and
  propagates its original error; ordinary and control fault cases pass.

### Spec closures

- Original control signal: invocation evidence retains the process-control cause
  even when cleanup also fails. The controller drains and rethrows that exact
  object with `batch_outcome`, structured `batch_errors`, and the primary
  `batch_error`; the immutable/persisted receipt includes `infrastructure_errors`.
  Secondary Worker cleanup remains in each item receipt. Public Observer plus
  required-write and Worker plus close failures pass, with all known input
  dispositions and no additional Worker starts.
- Root propagation: standalone and runner entrypoints preserve the same control
  object through a continuously failing owned-resource close, unavailable terminal
  manifest, or Run lease release. The runner's existing
  `ApplicationRunInterrupted.original_error` adapter keeps that identity. Typed
  `run_secondary_errors` retain phases and causes; initial manifest interruption
  plus failed lease release also starts zero Workers and preserves the signal.
- Broken stop observation: one locked observation path is shared by admission and
  invocation startup. Once internal stop is set, the failed external token is no
  longer consulted. The zero-start case returns a structured `stop_observation`
  failure; a gated started item and its cleanup drain before return. The retained
  baseline probe exceeded an independent eight-second test watchdog; that watchdog
  is test infrastructure, not a product exit deadline.
- Startup stop: the startup seam latches even a true-once token into internal
  stop. Both inputs are canceled, neither has an invocation ID, the batch is
  stopped, and no Runtime starts. The retained baseline probe instead completed
  and actually started the second item.
- Maintained callers: removed the retired Supervisor concurrency from the shared
  mixed Runtime project fixture. Agent-as-Tool dispatch stays with the Runtime;
  no irrelevant map policy was substituted. The former configuration/recovery
  failure groups now pass with their original assertions, including all sixteen
  real Pi recovery cases in the targeted run.
- Standalone caller acceptance: the controlled Runtime fixture now lives in
  `test_support.worker_batch`, outside collected test modules and the colliding
  `tests` package. Application, legacy and repo_map tests import that same fixture.
  Independent repo_map collection runs all sixty cases with importlib mode; the
  legacy and public Application cases pass as well.
- Final delivered-revision gate: **open**. Acceptance on `bb3c63a9` passed: Python
  3.12/3.14 each 556 cases, standalone repo_map each 60, full regression 2,384 plus
  one Windows-only skip, and both bounded live Provider CLI campaigns. These runs
  prove that source, not the later uncovered control/control combination or a
  future repaired source. PR base/public synchronization and delivery are owned
  by the integration task.
  Subsequent independent Spec review found a second-control terminal-persistence
  case; see the separate axes below. That repair and its delivered-source
  acceptance remain open.

### Validation and retained attempts

Durable ignored artifacts are in
`/Users/bytedance/code/AgentLoom-private/.agentloom/validation/worker-batch-review-fixes-20261011/`.

| Verification | Result | Artifact |
| --- | --- | --- |
| Isolated Python 3.14 targeted public/runner/architecture/mixed/recovery suites | 234 passed, zero skips, 159.58 s | `targeted.log`, `targeted.xml` |
| Added runner plus standalone persistent root-cleanup case | 2 passed, 1.37 s | `root-persistent-cleanup.log`, `.xml` |
| Independently collected repo_map suite | 60 passed, zero skips, 22.65 s | `repo-map-standalone.log`, `.xml` |
| Original public control/startup/broken-token probes on the integration baseline | Three expected failures; broken token watchdog expires at eight seconds | `red-probes.json`, `red-*.log` |
| Changed Python files Ruff and Git whitespace check | Passed | `ruff.log`, `diff-check.log` |

The first broad attempt could not import pytest-mock; its startup log is retained
as `targeted-missing-pytest-mock.log`. The next borrowed-interpreter run passed 234
cases, but a recovery test deliberately replaces `PYTHONPATH` and could fall back
to that interpreter's earlier editable source. Its log/XML are retained as
`targeted-borrowed-interpreter.*`, not used as the authoritative F result. F then
created its own environment with an immutable source path in its own `.pth`,
read-only dependency paths and the existing Pi SDK link. The authoritative run
above uses that environment even in fault-injection child processes.
No shared editable mapping, system network setup or live Provider profile changed.

Command lists, interpreter/module paths and source revisions are recorded in
`source.json`. The targeted command includes `tests/test_runner.py`, all Worker
Application/legacy/storage/value suites, architecture checks, mixed Application
and memory, YAML task sequences, structured subagent contracts, Hook stdin and
`tests/pi_test/test_recovery_application.py`. Repo_map is invoked separately:

```sh
PYTHONPATH="$PWD:$PWD/applications" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  .venv/bin/python -m pytest applications/repo_map/tests --import-mode=importlib -q
```

## Independent closure review on bb3c63a9

The following read-only reviews were conducted independently on the same frozen
source. The original findings above remain unchanged. These separate axes record
what each reviewer found after the first repair; neither is substituted for the
other. Acceptance artifacts are documented in
[the acceptance report](worker-batch-execution-v1-acceptance.md).

### Standards

Reviewed frozen HEAD `bb3c63a97b70ad7db8f39bc3711a60d4d1f5d129`; original baseline `bc5cff6e61abf2a6d7ba7b0b6d2efac210463659`. Closure scope: corrective diff `4a211d8d..bb3c63a9`, original Standards findings, and affected ownership/import boundaries. HEAD and tracked working tree remained unchanged and clean.

Standards sources: repository ownership rules in `tests/test_architecture_boundaries.py`, `docs/en/tool_catalog.md`, `docs/research/worker-batch-context-ownership.md`, and the existing lifecycle owner contract; the full Fowler smell baseline was considered. No repository `CODING_STANDARDS.md`, `CONTRIBUTING.md`, or local `AGENTS.md` was found. Tooling-enforced formatting/import/type concerns are excluded.

- **Original hard finding closed: configuration reverse-import.** `src/config/system_loader.py:9,77` now calls the validator owned by `src/config/config_validation.py:383`, beside the configuration schema. `src/execution/concurrency/policy.py` consumes that validator and retains capacity resolution; configuration does not reverse-load execution. An independent fresh-process probe imported the loader from this checkout, loaded capacity twenty, and found zero execution modules both before and after the public load. The corrected AST guard scans `config`; final XML contains all seventeen passing architecture checks, including actual public-loader execution.
- **Original judgement finding closed: possible Duplicated Code.** `src/app/lifecycle.py:123,138` now owns the shared root-service isolation and ordered drain/finalization/resource/engine teardown transaction. Both `Application.open()` and the runner call these methods while retaining their distinct initialization and result adapters. This extracts the repeated ownership decision without introducing a configurable lifecycle framework, delegating facade, or duplicate resolver. The new phase/error record has concrete use in preserving teardown evidence. Final XML confirms the standalone/runner persistent-close cases, manifest/lease secondary failures, and drain failure after successful execution pass.

No remaining or newly introduced actionable documented-standard violation or Fowler smell identified within this closure scope. Existing final regression evidence was inspected, not rerun: 2,384 passed, one Windows-only skip, zero failures/errors.

Standards: **0 remaining hard findings; 0 remaining judgement findings.** Worst remaining hard issue: **none identified**.

### Spec

Reviewed frozen `bb3c63a97b70ad7db8f39bc3711a60d4d1f5d129` against `bc5cff6e61abf2a6d7ba7b0b6d2efac210463659`, concentrating on repair delta `4a211d8d..bb3c63a9`. Contract: `docs/specs/worker-batch-execution-v1.md` (S); original 50-story/39-decision/26-scenario mapping retained. No scope shrink or additional scope creep established.

**Original closures verified:** ordinary secondary persistence/cleanup errors preserve the original Worker/observer control with complete dispositions, structured infrastructure diagnostics and drained invocations. Broken stop observation is latched and no longer retried; started work drains. Startup true-once stop prevents all later admission. Maintained mixed-runtime configuration is migrated, and standalone repo_map uses stable shared support rather than a collected test module.

**[P1] Control propagation remains partial for a secondary control during terminal persistence.** `src/app/lifecycle.py:560` catches only `Exception` in `persist_uncaught_failure`. S:137: “Control exceptions also stop/drain before propagation”; S:180: “Original and cleanup causes retained”. Public probe `/tmp/agentloom_batch_final_control_secondary_probe.py` supplies an original Worker `KeyboardInterrupt` and a second `KeyboardInterrupt` from terminal-manifest writes. Both `Application.open` and `execute_app` raise the secondary object; neither preserves the original or its attached `batch_outcome`. The runner additionally bypasses `ApplicationRunInterrupted`. Actual work drains (`active=0`, one closed Runtime); the independent ten-second watchdog does not expire. Preserve the first control and retain the secondary cause across this best-effort persistence path.

**Delivered-revision evidence verified, subject to that repair:** actual JUnit reports show 2384 passed plus one Windows-only skip in the CI-equivalent regression; Python 3.12/3.14 acceptance each reports 556 passed, zero skips; independently collected repo_map each reports 60 passed. Both bounded live Provider campaigns exit zero with `run.completed`, completed persisted batches, unique invocations and physical Tool/checkpoint evidence. Reports and parent process checks contain no surviving owned PID/listener/callback thread; credential scans report zero matches. The live/preflight Git source fingerprint exactly matches frozen HEAD. These artifacts close the previously missing runtime/version evidence at this revision, not the uncovered combination or a later repaired revision.

**Delivery remains open:** PR #11 is draft and baseline-targeted while another task owns prerequisite publication. Required final acceptance must be refreshed appropriately after any product repair; PR retarget/public synchronization and task cleanup remain integration deliverables.

Spec: **one new actionable finding; original control-preservation closure incomplete.**


## Terminal-control corrective review on 30605297

These independent reports preserve their original timing: both preceded final
delivered-source acceptance, and the Spec report preceded the successful isolated
Python 3.12 retry. They do not replace the first review axes above.

### Standards

Reviewed frozen SHA `306052971e6eaba16bbaef3ccbc32023c7033782` on `codex/worker-batch-terminal-control`; corrective diff `bb3c63a9...30605297`, original baseline `bc5cff6e61abf2a6d7ba7b0b6d2efac210463659`. HEAD and tracked worktree were unchanged and clean.

Retained the prior ownership/import standards and complete Fowler judgement baseline. Tooling-enforced formatting, imports, and typing are excluded.

**Hard violations:** none identified. `ApplicationRunLifecycle.persist_uncaught_failure` continues to own terminal-state and secondary-error selection. Returning its selected exception makes that decision explicit; both public callers consume it without reproducing selection policy or introducing a second lifecycle owner. Ordinary failure followed by the first control signal updates the lifecycle's own error/outcome through its existing outcome classifier. The standalone entrypoint retains its direct-propagation adapter; the runner retains its public Run-error adapter. Shared root isolation/drain/finalization/resource teardown remains centralized. Configuration dependency direction is unaffected.

**Judgement findings:** none actionable. No new duplication, speculative abstraction, cross-object state mutation, or uninformative domain naming identified. The test helper centralizes the two public Application seams, while parameterization covers control combinations and distinct timing cases without copied test orchestration.

Inspected, without rerunning, the new test source and `worker-batch-terminal-control-20261012/control-matrix.xml`: all 26 public-seam cases passed, zero failures/errors/skips. Assertions observe exact propagated exception identity, structured secondary causes, actual started/canceled dispositions, resource closure, and runner terminal events; they do not mirror private lifecycle flags.

Standards: **0 hard findings; 0 actionable judgement findings.** Prior Standards closures remain intact. Worst remaining hard issue: **none identified**.

### Spec

Read-only closure review froze clean commit `306052971e6eaba16bbaef3ccbc32023c7033782`, delta `bb3c63a9..30605297`. Contract: `docs/specs/worker-batch-execution-v1.md` (S). Original 50-story/39-decision/26-scenario scope remains unchanged.

**Original P1 closed in the reviewed repair.** S:137: “Control exceptions also stop/drain before propagation”; S:180: “Original and cleanup causes retained”. `ApplicationRunLifecycle.persist_uncaught_failure` now catches secondary `BaseException`, retains it as a typed manifest cause, and returns the selected terminal error. If a control is already pending, its exact object survives. If only an ordinary error was pending, the first persistence control propagates while retaining that earlier error. Both standalone and runner entrypoints consume the selected result; the runner again applies its existing `ApplicationRunInterrupted.original_error` adapter for KeyboardInterrupt. No unconditional swallowing of the first control was introduced.

The original public reproducer passes on this committed source under its independent ten-second watchdog (`final-watchdog-probe-result.json`: exit zero, 0.726 seconds). Both entrypoints retain the original object and attached complete batch receipt, with one closed Runtime and zero active work. The 26-case public matrix covers both entrypoints, KeyboardInterrupt/SystemExit/custom controls, original-plus-secondary controls, first control after ordinary failure, and first control after successful work. Its assertions inspect exact identity, structured secondary causes, actual started/canceled dispositions, invocation IDs, drained Runtime resources and runner terminal events.

Actual retained red→green logs/JUnit prove the masking failure and the otherwise-hidden first-control swallowing regression before the selected-error correction. Final committed Python 3.14 focused evidence records 162 passed, zero skipped. No new incorrect implementation, missing requirement within this delta, or unrequested behavior identified.

**Final gates remain distinct and open.** S:197–198: “Completion requires the acceptance matrix, supported-version checks and both Runtime Application validations on the delivered revision.” Historical `bb3c63a9` broad/live successes do not prove this repaired revision. The initial final Python 3.12 attempt failed before collection because Phoenix plugin autoload lacks `pkg_resources`; it is not a product failure or a passing minimum-version result. Root/E must supply repaired-revision acceptance evidence. PR retarget/public synchronization and worktree cleanup remain integration delivery work.

Spec: **zero new findings; terminal-persistence P1 repair verified; final acceptance/publication pending.**

The retained focused minimum-version follow-up is
`worker-batch-terminal-control-20261012/final-py312-attempt2.xml`: 162 passed, zero
skips, JUnit 8.911 s, exit zero in its accompanying result JSON on `30605297`.
The initial Phoenix/plugin-autoload failure remains preserved. Integration
`ed506f44` has the exact same complete non-doc tree, fingerprint `2314313f…`.
The final acceptance report records the new 582-case version runs, full regression
and both bounded live Provider campaigns; no historical green replaced them.

## Delivered-source review on ed506f44

The terminal Standards report remains the independent Standards axis for this
identical source. The latest independent Spec audit below also inspects actual
final acceptance and cleanup/secret evidence. Original reports and open-gate
statements above retain their historical context.

### Spec

Read-only delivered-source audit froze clean `ed506f4474552ab13ed622894c7e64ce6da89516`. Its complete non-documentation tree is identical to reviewed terminal-control repair `30605297` (fingerprint `2314313f…`); the selected-source fingerprint `51cd41e1…` matches both acceptance preflights and live reports. Original 50-story/39-decision/26-scenario scope remains intact.

**Product findings closed.** Prior reviews plus the terminal-control closure establish ordinary/control secondary-error preservation, exact first-control identity and batch receipt, structured causes, actual drain, failed-stop observation handling, startup-stop latching, maintained caller migration and standalone test-support independence. Actual final acceptance XML includes all 26 new public terminal-control cases on each supported Python version. No remaining product finding or additional scope creep identified.

**Local completion gate verified.** S:197–200: “Completion requires the acceptance matrix, supported-version checks and both Runtime Application validations on the delivered revision” and “Preserve failed attempts and cleanup diagnostics.” Inspected authoritative receipts under `.agentloom/validation/worker-batch-final-20261012/final-ed506f44` and `worker-batch-acceptance-20261012/final-ed506f44`:

- CI-equivalent regression exits zero with unchanged source: actual XML has **2410 passed**, one Windows/wexpect-only skip, zero failures/errors.
- Python 3.12.13 and 3.14.8 acceptance each exits zero with **582 passed, no skips**; standalone repo_map each has **60 passed, no skips**.
- First post-repair bounded live Pi/smolagents campaigns exit zero without watchdog expiration (32.81/14.00 seconds). Both CLI terminals and manifests are completed, full persisted batch records match reports, all two-item outcomes succeed with distinct invocation IDs, and outputs equal independently recorded physical Tool evidence.
- Contemporaneous child OS/port/thread/private-directory checks and parent process-exit receipts show no remaining owned resources. These are run-time observations, not present-day PID reinterpretations. Provider request retries remain bounded at one and do not replay complete Workers.
- Actual artifact scan reports **46,483 files**, zero credential/fixture matches and zero read errors; secret-bearing model inputs are separately classified, with current credential inputs mode 0600.

The initial Python 3.12 Phoenix/plugin-autoload failure remains preserved; isolated rerun XML proves 162 focused tests passed on the same repaired source. Historical failures were not discarded or substituted for current evidence.

**Delivery remains pending separately:** prerequisite merge owned by another task, PR retarget, public CI/synchronization, private/public merge and issue closure. Product/local acceptance closure does not claim those external steps are complete.

Spec: **zero remaining product findings; delivered-source local acceptance verified; publication pending.**

## Delivery update after main integration (2026-10-12)

The independent Standards and Spec axes above remain unchanged at their recorded
revisions and times. Their product/local acceptance closures stand; their older
prerequisite, PR and cleanup statements are historical, with current delivery
state recorded here and in the [acceptance report](worker-batch-execution-v1-acceptance.md).

Prerequisite `bc5cff6e` is now in `origin/main` at `68d7aca5`, merged into integration
`6adf618381dd6b34f48ef4d15f339670fbfe6074`. Its 24 incoming changes are confined to
private `applications/news_agent/` files. `main-integration/merge.json` and
`public-source-evidence-carryforward.json` verify the public product tree is
identical to accepted `ed506f44`, preserving the existing 2,410-pass regression,
582-pass acceptance per Python version, 60-pass standalone repo_map per version
and both bounded live campaigns. This is public-product equivalence, not equality
of the complete tree including the changed private application.

The merger's actual Python 3.14.8 NewsAgent-only integration regression on
`6adf6183` reports **360 passed, zero failures/errors/skips, pytest exit 0,
162.8395 seconds wall time**, with tracked source/revision unchanged. Precise
command/environment and original XML/log are retained under root
`.agentloom/validation/worker-batch-delivery-20261012/main-integration/` in
`news-agent-regression-attempt.json`, `news-agent-regression-result.json` and
`news-agent-py314.xml/log`. The initial outer runner returned 90 because tests
generated untracked `applications/news_agent/data/output/`; pytest exit 0 and the
historical dirty-state snapshot remain unchanged. The merger preserved all 20
runtime files (3,715 bytes) under ignored `test-generated-news-output/`, verified
their hashes, then confirmed root clean in
`news-agent-runtime-artifact-preservation.json` and
`news-agent-regression-final-state.json`. `delivery-final-proof.json` verifies
`6adf6183` pushed/read back from origin feature with no public product diff from
`ed506f44`. The following documentation commit still needs merger integration and
push. This documentation update reran no tests or Providers.

All seven old implementation worktrees were normally removed by 04:41:27 UTC,
with their branches preserved and historical evidence archived. The authoritative
delivery-root `worktree-cleanup.json` retains both the earlier four-worktree
snapshot and final `cleanup_history` / `latest_final_checks` with no remaining old
task worktrees. The newly created documentation worktree needs its own removal
after merger handoff and is not included in that completed cleanup count.

Formal main delivery is now draft [PR #12](https://github.com/linora-u/AgentLoom-private/pull/12).
Old [PR #11](https://github.com/linora-u/AgentLoom-private/pull/11) merged only into
temporary `codex/worker-batch-baseline` at `2aba10e6`; it did not merge Worker batch
into main. Required public CI on the delivery revision, public then private main
merges, and #109/#110 closure remain outstanding publication gates. Neither
skipped checks nor a synchronization success proves those gates complete.
The user has confirmed the additional public-sync identity rule: **both Author
and Committer are fixed to `linora-u`**. A separate bootstrap PR based on
`origin/main` must deliver this change because the trusted coordinator uses the
sync script on main. Its affected validation, public CI and public-then-private
main merges precede marking Worker batch PR #12 ready. This additional delivery
requirement is separate from the accepted product scope; its implementation and
publication still need actual receipts. The formal Worker batch PR remains draft.
