# Architecture contract validation

This is the real F1/F2/F9 acceptance Application. It requires the project's
`config/llm.yaml` and calls its `powerful` model profile. It never replaces a
model, Worker, or framework invocation with a canned answer.

The Supervisor calls four typed Workers in order: repository investigation,
change planning (a Markdown definition), repair and test authorship, and
independent verification. Workers use bounded tools for real filesystem access
and real subprocess pytest execution. The host's oracle is separate from those
tools. No tool contains a source repair or test-generation template.

Run from an installed candidate environment, setting an evidence directory
outside the checkout (it retains a private copy of model configuration):

```sh
python -m applications.architecture_contract_validation.run_acceptance \
  --project /absolute/path/to/AgentLoom \
  --output /absolute/private/evidence/architecture --case all --timeout 1200
```

`--case native` and `--case codeact` each run twice by default. `--repeats 1`
is useful for diagnosis but does not satisfy final consecutive-success criteria.
The `all` suite runs both modes twice within one interpreter (`repeat-native` /
`repeat-codeact`), both modes once in nested Application locations, all three
invalid-definition variants, and a real Hook policy block. Same-interpreter
repetitions supply the required two consecutive successes without duplicating
another pair of fresh-process runs. They share loaded framework modules and the Application definition but
use distinct workspace nonces and must have distinct task/run identities.

Nested copies relocate their App-owned, fully qualified Python tool module
names from `applications.architecture_contract_validation.*` to
`applications.nested.suite.architecture_contract_validation.*`. This explicit
path migration is recorded field by field in `namespace_adaptations`, along
with all resulting definition digests. Relative Worker paths, workflow text
and prompt/resource paths stay unchanged. The tool loader must still reject
modules cached from outside the isolated project.

Each attempt has a 1..3600 second wall-clock limit. Timeout sends SIGINT to the
process group, then SIGKILL after 15 seconds if required; it is a failed attempt.
Agent steps and each pytest subprocess are also bounded. The runner does not
retry or erase failures. Rerunning allocates new timestamp/UUID directories and
appends `attempts.jsonl`.

Each attempt contains:

- `request.json`, `receipt.json`, and `summary.json`: candidate revision,
  Application and definition hashes, safe configuration summary, actual model,
  execution mode, times, exit state, and Run/task receipt.
- `project/`: an isolated project with the same Application and copied private
  model config; the candidate implementation is imported from `--project`.
- `workspace/`: the reset fixture, model-authored repair and regressions,
  every Worker pytest JSON/JUnit/log file, and `reports/final.json`.
- `runtime/`, `lifecycle.jsonl`, `process.log`, and `tool-ledger.jsonl`: actual
  framework checkpoints and root/local Worker execution identities.
- `host-validation/`: the 50-check independent behavior oracle, fresh host pytest
  execution, and generated regressions executed against original defective source.
- `validation.json`: assertions and errors; model claims and process exit 0 alone
  cannot pass acceptance.

Artifact checks require unchanged baseline tests/configuration/contract, all
nine named behavior families collected and passing, zero skipped tests, and at
least five failures when generated tests are applied to original source. Trace
checks require all four distinct Worker local runs, actual model usage, persisted
completed Worker checkpoints, intact output-to-input transfers (including a
complete preceding result inside a JSON or JSON-text wrapper, or accompanied by
sibling context fields; original nested values and types must remain exact), real implementer
and verifier pytest calls, an accurately referenced verifier report, and completed
Run state. The `zero_price` family distinguishes an empty cart from a nonempty
zero-valued cart under positive and zero free-shipping thresholds; zero subtotal
does not imply an empty cart. The 50-check oracle remains independent of model
instructions and generated tests.

`reports/final.json.test_report` is a relative-path string from the final verifier's
actual `run_workspace_tests.report` field. The existing writer rejects malformed
final reports before writing: field types and current identity, local generated
files, pytest JSON/JUnit agreement, and an actual test-call ledger entry must match.
It returns an actionable error and never fills in model claims. Trace validation
separately requires the final verifier's own successful call and true verdict.

Worker transfers are matched by receipt Application/task, per-call checkpoint
identity, canonical start/finish order, input hash, and local tool-event identity.
Repeated calls may produce different results; only an intact result completed
before the consuming call can establish an edge. Corrective verifier→repair→verifier
iterations retain their earlier investigation/planning lineage. Validation records
the selected call indices, checkpoint paths, completion/start times and result hashes;
future or foreign-task results cannot satisfy a transfer.

Supervisor Python evidence comes only from the exact Application/task
checkpoint named by the receipt, with matching task and run IDs; other tasks
and Worker Python actions cannot satisfy it. Policy checks read the persisted `ToolCallRecord`, require `blocked`
with the expected reason, and verify the requested file was never written.
Invalid variants require Studio and preflight diagnostics for the same cause,
no model network request, no tool ledger, and no allocated Run.

The original baseline resolves Worker paths only from the project root. For
explicit baseline collection only, `--baseline-project-relative` rewrites copied
Worker references and records that adaptation. Final acceptance must omit it:
canonical definitions intentionally use paths relative to their source, and
nested runs must exercise the shared resolver.

Run deterministic validator tests with:

```sh
python -m pytest tests/test_architecture_validation_application.py
```
