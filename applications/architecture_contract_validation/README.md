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
eight named behavior families collected and passing, zero skipped tests, and at
least five failures when generated tests are applied to original source. Trace
checks require all four distinct Worker local runs, actual model usage, persisted
completed Worker checkpoints, intact output-to-input transfers (including a
complete preceding result inside a JSON or JSON-text wrapper, or accompanied by
sibling context fields; original nested values and types must remain exact), real implementer
and verifier pytest calls, an accurately referenced verifier report, and completed
Run state. Policy checks read the persisted `ToolCallRecord`, require `blocked`
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
