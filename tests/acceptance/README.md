# Existing Application architecture acceptance

These manual runners call the configured real models. They do not run under
ordinary pytest. Start them from the candidate checkout with its configured
Python environment and ignored `config/llm.yaml`. A new `--workspace` is required
for each attempt; existing directories are never cleared.

```bash
python tests/acceptance/existing_application_validation.py --case all --workspace /new/evidence
python tests/agent_test/real_checkpoint_validation.py --scenario all --workspace /new/checkpoints
```

The first command runs F3, F4, all three F5 cases, both F7 cases and both F8 cases.
F1/F2/F9 belong to `architecture_contract_validation`. Each scenario uses the
public `execute_app` interface. Original Workflows remain intact; required output
path adaptations are copied to ignored `applications/architecture_acceptance_*`
Applications to preserve baseline project discovery. Runtime, fixture and result
directories are private to the selected evidence directory.

| Case | Independent checks | Wall limit |
| --- | --- | --- |
| `unit` | Five Worker calls, actual tool completions, unchanged two-function fixture, newly generated pytest executed by host, collected > 0 and no failures/errors/skips, manual normal/boundary/exception oracle | 900 s |
| `repo` | Three nested directory levels, known definitions/references, ranking, completed per-directory Worker analysis, resolvable nonempty Skill routes and cross-module dependencies | 900 s |
| `context_text`, `context_json`, `context_multi` | Stored original source/kind/hidden values, independent ContextRefs, matching retrieval event arguments and returned hidden values, actual Worker count | 900 s each |
| `goal_bounded` | Completed finite 600000-token Goal, four completed Workers, cumulative usage, persisted verified report | 1500 s |
| `goal_parallel` | Six concurrent Workers cross 50000-token shared budget, preserved checkpoint/report, increased bounded budget for same-task/new-run resume, same goal_id and cumulative usage, identical report and no additional Worker batch | 1200 s including resume |
| `core`, `markdown` | Exact independently read artifacts plus actual completed shell/file/search/Markdown tools, existing default versus explicit replacement toolset Workflows | 900 s each |
| checkpoint `main`, `worker`, `completed` | Signal after committed progress, same task/new run, exactly-once ledger, one completed Worker, complete file manifest, historical ContextRef and file-history recovery | Initial 180/240/240 s; resume 360 s each |

All real scenarios also retain the original model log, canonical Run receipt,
manifest and lifecycle events. CodeAct actual tool events are read from the
runtime's durable session recorder; native typed tool records are read from
checkpoints. Model-generated code and final success text are never tool-execution
evidence. The ContextEngine checker permits full or searched retrieval; it
requires the recorded arguments/event/output to agree and the original hidden
record to be returned from the correct source.

Runs retain failed assertions and exit codes. `--verify-existing --case <case>
--workspace <scenario-directory>` writes a separate timestamped verification
record, for correcting a verifier or auditing retained artifacts without another
model run. Unit rechecks write separately timestamped pytest XML/logs. This never
turns an old failed attempt into a new candidate-revision model run.

For historical checkpoint compatibility, prepare with the baseline and resume
with the candidate interpreter/check-out:

```bash
python tests/agent_test/real_checkpoint_validation.py --scenario all --prepare-only --workspace /new/baseline
python tests/agent_test/real_checkpoint_validation.py --resume-state /new/baseline/main/resume_state.json
python tests/agent_test/real_checkpoint_validation.py --resume-state /new/baseline/worker/resume_state.json
python tests/agent_test/real_checkpoint_validation.py --resume-state /new/baseline/completed/resume_state.json
```

Freeze the prepared directory before consuming it. Preserve the source baseline
Application copy until candidate resume: the runner copies that Application to
the candidate under the same identity, adjusting only its absolute Worker
reference. It leaves the original task identity, runtime and effect ledger in
place. Configuration secrets are neither copied into evidence nor reported.

The `completed` checkpoint case adds one bounded capture/barrier tool to a copied
complex checkpoint Application. It pauses the Supervisor after the original
Worker has committed its full task. On resume the Supervisor calls the identical
Worker input again. The validator requires one durable cache claim, one Worker
call total, byte-identical observed Worker returns, unchanged Worker checkpoint
and token totals, and exactly-once side effects. The barrier captures actual
Worker output; it does not generate Agent responses. Its own duplicate output
writes fail explicitly. A prepared `completed` state can also be created by the
baseline framework and resumed by a later candidate.
