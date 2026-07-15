# Memory Feature Validation

This application validates the optional completed-run memory review contract
with real `loom run` subprocesses and the configured `summary` model.

The campaign proves the two supported modes:

- absent or empty `memory.review_model` makes no completed-run model call or
  distillation; foreground `memory` calls still work;
- `memory.review_model: summary` synchronously reviews a successfully
  completed run after SessionEnd is persisted and before `loom run` returns;
- reviewer may copy at most one complete fact exactly from an unblocked, tool-bound
  trusted-evidence envelope whose extractor explicitly classified it as
  `kind="durable_fact"` with an exact `project` or `application` scope;
  shortening, scope changes, ordinary result fields, paraphrases, unsupported
  claims, `replace`, and `remove` are rejected;
- a final summary by itself cannot authorize a write; the add is staged in
  process, a validated add terminates the review, and memory plus its terminal
  audit commit atomically once per root run; result data cannot make itself
  eligible by choosing a field name or by spoofing the internal envelope key;
- `memory.write_approval: false` writes active memory, while `true` stages a
  pending memory for `loom memory pending`, `approve`, or `reject`;
- only active memory appears in a later run's frozen snapshot, with project and
  Application scope isolation.

The model-visible dataset is [`data/cases.jsonl`](data/cases.jsonl) plus the
de-identified fixtures under `data/fixtures/`. Expected status, scope, recall,
and security markers live in the separate model-invisible
[`oracle/cases.jsonl`](oracle/cases.jsonl). Natural tasks never script a
`memory(...)` call.

The 100 real Applications cover disabled review, durable review and recall,
temporary progress, ordinary unverified claims, mixed durable/transient input,
secret and injection payloads, foreground writes, approval/rejection, and
project/Application scope isolation. Application-scope writers use a
reviewer-enabled Application with no foreground `memory` tool: only its
trusted `durable_fact` evidence can create the scoped item, the same
Application must recall it, and a distinct Application must not. Configured final-only runs still execute
the reviewer, but repeating an unsupported claim in the final answer must
produce zero memory writes. Raw progress and completion claims remain history:
they cannot qualify through ordinary event/JSONL data, and the framework does
not use a progress keyword list or semantic regex to reclassify them.

Validate the fixed five-canary and 100-run plans without a model call:

```bash
uv run python applications/memory_feature_validation/scripts/run_memory_review_campaign.py \
  --runs 5 --dry-run
uv run python applications/memory_feature_validation/scripts/run_memory_review_campaign.py \
  --runs 100 --dry-run
```

Run five real canaries, then the full campaign with at most two independent
cohorts in parallel. The fixed canary includes the reviewer-driven
Application-scope writer:

```bash
uv run python applications/memory_feature_validation/scripts/run_memory_review_campaign.py \
  --runs 5 --max-workers 1
uv run python applications/memory_feature_validation/scripts/run_memory_review_campaign.py \
  --runs 100 --max-workers 2
```

Re-audit captured evidence without another provider call:

```bash
uv run python applications/memory_feature_validation/scripts/audit_memory_review_campaign.py \
  .agentloom/validation/memory_feature_validation/<campaign_id>
```

Every real run uses an isolated runtime root; each logical case uses an
isolated self-learning root. Multi-phase cohorts deliberately share only their
writer and recall/decision state. The report separates Application and
reviewer model calls and token usage, records SQLite and CLI evidence, and
fails closed if it cannot prove whether review ran or if a completed-run
reviewer exceeds four provider requests.

The runtime telemetry contract is one stable line beginning with
`Memory review:` and containing at least `enabled`, `requested`, `resolved`,
`calls`, `input_tokens`, `output_tokens`, and `actions` as `key=value` fields.

Artifacts are written under
`.agentloom/validation/memory_feature_validation/<campaign_id>/` and include
the plan, environment, results, usage, privacy audit, failures, report, and
`reproduction_commands.json` for any failed runs.

## Offline v5 campaign

The deterministic offline campaign validates the current v5 surface without
calling a model. It writes exactly 100,000 canonical events with seed
`20260711`: 50,000 ledger/FTS/search/scroll events, 20,000
redaction/injection events, 20,000 root-isolation events, and 10,000 events
paired with active/pending memory operations. A separate literal v4 fixture
validates migration to v5; the campaign does not restore the removed outbox,
evidence-voting, revision/trust, or ranking state machines.

The default release run also opens the current `.agentloom/self_learning.db`
with SQLite `mode=ro&immutable=1`. It records only run/event counts, byte-length
percentiles, and hashed event-type distribution; event/task/final text is
never selected or copied into the campaign.

Run a small smoke first. The first release-sized run establishes a candidate
baseline; a second run compares append latency and bytes/event against it:

```bash
uv run python applications/memory_feature_validation/scripts/run_offline_memory_campaign.py \
  --events 100 --migration-events 100
uv run python applications/memory_feature_validation/scripts/run_offline_memory_campaign.py
uv run python applications/memory_feature_validation/scripts/run_offline_memory_campaign.py \
  --baseline-metrics \
  .agentloom/validation/memory_feature_validation/<baseline-campaign>/metrics.json
```

Only the default 100,000-event, 10,000-migration-event shape can become a
baseline candidate. It reports `release_passed` only on the comparison run,
when append latency/event and bytes/event are each no more than 20% above the
accepted candidate. Candidate loading first re-audits its immutable databases
and requires every bound harness/production file in its source manifest to
match a frozen Git commit. It then checks out that commit in a detached
temporary worktree and independently reruns the fixed 100,000-event append and
migration workload; the candidate's reported latency and physical bytes are
not used as baseline operands.
Re-audit repeats the frozen probe with a 20% reproducibility bound. Reduced and
`--only-case` runs report only a smoke result. Uncommitted changes to a bound
source file prevent a baseline candidate; unrelated user-owned worktree changes
are recorded as `worktree_dirty` but do not change release eligibility.
Re-audit an existing campaign without executing production writes:

```bash
uv run python applications/memory_feature_validation/scripts/run_offline_memory_campaign.py \
  --audit .agentloom/validation/memory_feature_validation/<campaign_id>
```

Offline artifacts include `cases.jsonl.gz`, the central `self_learning.db`,
`migration_v4_to_v5.db`, metrics, privacy audit, content-free failures, report,
and single-case reproduction commands. Raw generated secret and injection
markers are never written to case, failure, or report artifacts.
