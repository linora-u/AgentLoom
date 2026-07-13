# Memory Feature Validation

This application validates self-learning memory with the `summary` model for
both the Application agent and asynchronous SessionEnd distiller.

## Offline 100k campaign

Reduced runs are explicitly smoke-only; they report `smoke_passed` and can
never be mistaken for a release result:

```bash
.venv/bin/python applications/memory_feature_validation/scripts/run_offline_mass_validation.py \
  --cases 1000 --seed 20260711 --workers 2
```

The 100,000-case release does not accept a user-supplied baseline JSON or hash.
It creates a temporary clean Git worktree at the fixed reference commit
`5ca02b552e3edd271ccedb0e930abf5a0a9f9993`, then runs the same 10,000-event
benchmark driver against that worktree and the current implementation. The
generated manifest binds the reference commit and full tree, driver and
benchmark-spec hashes, environment, current self-learning source hashes, and
the de-identified source shape. The benchmark subprocess also reports hashes
for the self-learning modules it actually imported; release requires those
hashes to match a stable pre/post working-tree manifest.

The old commit does not need to contain the new harness: the current
orchestrator executes its standalone benchmark driver with the clean worktree
as the import root. Therefore the reference is executable, while there is no
hand-authored performance artifact that can be substituted.

Run the release against the frozen source ledger whose read-only shape is
exactly 82 runs and 1,706 events:

```bash
.venv/bin/python applications/memory_feature_validation/scripts/run_offline_mass_validation.py \
  --cases 100000 --seed 20260711 --workers 4 \
  --source-db /path/to/frozen-source.db
```

The run hard-fails release status unless all 100,000 cases execute with the
literal seven category quotas and the source shape is exactly 82/1,706.
`fixed_point_benchmark/manifest.json`, `reference.json`, and `current.json`
contain the paired benchmark evidence.

The release status enforces the semantic, privacy, SQLite integrity, FTS,
ranking, SessionEnd latency, duration, RSS, artifact-size, and relative
regression gates. The source ledger is opened read-only and immutable; only
schema, counts, text lengths, and aggregate event-type distribution are read.

## Real summary campaign

Plan-only smoke checks (no model call):

```bash
uv run python applications/memory_feature_validation/scripts/run_summary_campaign.py --runs 1 --dry-run
uv run python applications/memory_feature_validation/scripts/run_summary_campaign.py --runs 5 --dry-run
uv run python applications/memory_feature_validation/scripts/run_summary_campaign.py --runs 100 --dry-run
```

Real 100-run campaign:

```bash
uv run python applications/memory_feature_validation/scripts/run_summary_campaign.py --runs 100
```

The runner executes five canaries first, then runs at most two independent
case/cohort groups concurrently. Every Application is a real
`loom run <workflow> --log-to-file` subprocess with a unique runtime root.
Self-learning state is unique except for the exact two-run corroboration and
high-overlap conflict cohorts. The runner refuses the live
`.agentloom/self_learning.db` path and records its before/after SHA-256.

Only explicit transient transport failures (429, 502/503/504, connection reset
or refusal, name-resolution failure, service unavailable) receive one
clean-state retry. Model/configuration, code, and semantic failures are never
retried. After `loom run` exits, the runner waits for every newly committed
learning job to reach `succeeded` or `dead`. If the best-effort detached worker
does not claim the outbox within ten seconds, the runner exercises the hidden
internal recovery path with an explicit isolated `_memory-worker`; this is counted
separately and does not count toward the 95/100 first-completion gate.

The real campaign has a non-extendable eight-hour deadline. It proves the
Application model from runtime resolution logs and the distiller from the
resolved global config plus the committed job result. SessionEnd hook durations
are measured from runtime logs and gated at p95 <100ms and p99 <250ms.

The 9/10 scenario allowance never masks a hard-zero violation: cross-scope or
cross-run leakage, false/contradictory evidence activation, revision lineage
contamination, batch atomicity damage, raw injection/secret persistence,
duplicate or stale-worker effects, SQLite damage, and SessionEnd latency/model
work fail the whole campaign immediately. Ordinary model-format or recall
misses still count against the overall 95/100 and per-scenario 9/10 gates. The
five conflict pairs cover number, path, version, negation, unit, and
punctuation changes.

The subprocess stream stays in memory until model/timing evidence is extracted;
it is then redacted and injection-blocked before the first atomic campaign-log
write. Process file logging and checkpoints are disabled only for this isolated
validation app, and command-hook payloads cross the same boundary before temp
files, environment variables, or visualization artifacts can observe them.
The audit is read-only: it scans every generated file (including runtime logs,
SQLite/WAL/SHM, snapshots, digests, proposals, and learning artifacts) for raw
secret or injection probes. Any hit, or evidence of legacy post-hoc rewriting,
is an immediate privacy failure.

The runner waits for the corresponding outbox job, then immediately scans the
live SQLite DB/WAL/SHM and generated artifacts without resetting a shared WAL
generation. Transient insert-delete-checkpoint forensics run against one-shot
isolated databases in the offline campaign and deterministic contract tests;
a long-lived read snapshot is never held on a live two-phase cohort database
because it would hide the next phase's outbox frames from the runner.

Artifacts land under
`.agentloom/validation/memory_feature_validation/<campaign_id>/`:

- `plan.json` and `results.json`
- `canary_audit.json` for a real run
- `privacy_audit.json` and `failure_cases.jsonl`
- `campaign_timing.json` and resolved model evidence in `environment.json`
- `report.md` and `reproduce_commands.txt`

Re-audit without a model call:

```bash
uv run python applications/memory_feature_validation/scripts/audit_campaign.py \
  .agentloom/validation/memory_feature_validation/<campaign_id>
```
