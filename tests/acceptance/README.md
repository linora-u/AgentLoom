# Existing Application architecture acceptance

These manual runners call the configured real models. They do not run under
ordinary pytest. Start them from the checkout under validation with its configured
Python environment and ignored `config/llm.yaml`. A new `--workspace` is required
for each attempt; existing directories are never cleared.

```bash
python tests/acceptance/existing_application_validation.py --case all --workspace /new/evidence
python tests/agent_test/real_checkpoint_validation.py --scenario all --workspace /new/checkpoints
python tests/acceptance/model_protocol_matrix.py --workspace /new/protocol-matrix
```

The first command runs F3, F4, all three F5 cases, both F7 cases and both F8 cases.
F1/F2/F9 belong to `architecture_contract_validation`. Each scenario uses the
public `execute_app` interface. Original Workflows remain intact; required output
path adaptations are copied to ignored `applications/architecture_acceptance_*`
Applications to preserve stable project discovery. Runtime, fixture and result
directories are private to the selected evidence directory.

The protocol matrix reads the ignored `config/llm.yaml` without modifying it.
For each of `openai_chat`, `openai_responses`, and `anthropic_messages`, it
selects one explicitly configured profile and runs the same minimal
smolagents/Tool Gateway/final-answer Application through public `execute_app`.
Missing profiles or credentials are recorded as `NOT-RUN` with a reason; they
are never counted as passed. Runnable failures are recorded as `FAILED`, do not
stop the remaining adapters, and make the command exit nonzero. Evidence under
the requested workspace includes the revision, adapter, profile, reportable
model name, Run manifest and lifecycle identities, completed echo/final-answer
Tool records, and schema-2 canonical checkpoint summary. API keys and base URLs
are excluded or redacted from reports and process logs.

| Case | Independent checks | Wall limit |
| --- | --- | --- |
| `unit` | Five Worker calls, actual tool completions, unchanged two-function fixture, newly generated pytest executed by host, collected > 0 and no failures/errors/skips, manual normal/boundary/exception oracle | 900 s |
| `repo` | Three nested directory levels, known definitions/references, ranking, completed per-directory Worker analysis, resolvable nonempty Skill routes and cross-module dependencies | 900 s |
| `context_text`, `context_json`, `context_multi` | Stored original source/kind/hidden values, independent ContextRefs, matching retrieval event arguments and returned hidden values, actual Worker count | 900 s each |
| `goal_contract` | Completed Goal, four completed Workers, native workflow contract and persisted verified report | 1500 s |
| `goal_parallel` | Six concurrent Workers, verified Goal-bound report and explicit completion | 1200 s |
| `core`, `markdown` | Exact independently read artifacts plus actual completed shell/file/search/Markdown tools, existing default versus explicit replacement toolset Workflows | 900 s each |
| checkpoint `main`, `worker`, `completed` | Signal after committed progress, same task/new run, exactly-once ledger, one completed Worker, complete file manifest, historical ContextRef and file-history recovery | Initial 180/240/240 s; resume 360 s each |

All real scenarios also retain the original model log, canonical Run receipt,
manifest and lifecycle events. Typed `ToolCallRecord` evidence is read from
runtime checkpoints and audit artifacts. Model output and final success text are
never tool-execution evidence. The ContextEngine checker permits full or searched retrieval; it
requires the recorded arguments/event/output to agree and the original hidden
record to be returned from the correct source.

Runs retain failed assertions and exit codes. `--verify-existing --case <case>
--workspace <scenario-directory>` writes a separate timestamped verification
record, for correcting a verifier or auditing retained artifacts without another
model run. Unit rechecks write separately timestamped pytest XML/logs. This never
turns an old failed attempt into a new successful model run.

To split one same-version checkpoint test into prepare and resume commands, use
the same Git revision, interpreter, smolagents runtime version and state schema
for both commands:

```bash
python tests/agent_test/real_checkpoint_validation.py --scenario all --prepare-only --workspace /new/checkpoints
python tests/agent_test/real_checkpoint_validation.py --resume-state /new/checkpoints/main/resume_state.json
python tests/agent_test/real_checkpoint_validation.py --resume-state /new/checkpoints/worker/resume_state.json
python tests/agent_test/real_checkpoint_validation.py --resume-state /new/checkpoints/completed/resume_state.json
```

The resume command rejects a different source revision or runtime contract
before making a model call. It leaves the original task identity, runtime and
effect ledger in place. Configuration secrets are neither copied into evidence
nor reported. Schema 2 checkpoints must contain both `memory_steps` and the
ordered `canonical_model_items` stream.

The `completed` checkpoint case adds one bounded capture/barrier tool to a copied
complex checkpoint Application. It pauses the Supervisor after the original
Worker has committed its full task. On resume the Supervisor calls the identical
Worker input again. The validator requires one durable cache claim, one Worker
call total, byte-identical observed Worker returns, unchanged Worker checkpoint
and token totals, and exactly-once side effects. The barrier captures actual
Worker output; it does not generate Agent responses. Its own duplicate output
writes fail explicitly. A prepared `completed` state remains useful for
same-version prepare/resume validation.
