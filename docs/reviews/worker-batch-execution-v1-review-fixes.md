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
- Final delivered-revision gate: **open**. The final acceptance matrix on Python
  3.12/3.14, bounded live Pi/smol Provider campaigns, full CI-equivalent regression,
  PR base/public synchronization and delivery are owned by the integration task.
  These local fixes do not claim historical Runtime successes as final evidence.

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
