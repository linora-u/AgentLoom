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

`review_model` follows the normal deep-merge rules. A global value is inherited
when an Application omits the key; an Application can explicitly opt out with
an empty value:

```yaml
# config/system.yaml: enable completed-run review globally
self_learning:
  memory:
    review_model: summary

# applications/<app>/config/system.yaml: disable it for one Application
self_learning:
  memory:
    review_model: ""
```

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

The ten `review_off_durable` Applications run from a committed nested
AgentLoom fixture whose global `config/system.yaml` enables `summary` review
and whose Application `config/system.yaml` explicitly sets `review_model: ""`.
This exercises the real configuration discovery and deep-merge path: the
Application must make zero review calls even though its global base enables
review. The model-invisible oracle labels all ten rows with this layering
contract.

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

The local, untracked `config/llm.yaml` must provide a tool-capable `summary`
profile with `num_retries: 0` and `parallel_tool_calls: false`. The campaign
rejects `thinking.type: disabled` for the validated endpoint, records only a
secret-free fingerprint of the complete provider behavior config, and fails
before any model call if those invariants are absent. The harness, dataset,
workflows, Application configs, the complete tracked `src/` runtime, and the
Python dependency manifests are also hashed and must exactly match the current
Git commit; unrelated worktree changes do not affect this bound-source check.

Every non-dry campaign re-executes itself from a private detached worktree at
that fixed commit. The worktree is created without checkout; trusted Git
plumbing reads raw tree/blob objects with replace refs and global/system config
disabled, then materializes them without hooks, attributes, or clean/smudge
filters. Replace refs and custom repository filters fail preflight, while
gitlinks and symlinks that escape the capsule are rejected.

The capsule performs `uv sync --locked --all-groups` into a fresh venv. The
ignored credential-bearing model config crosses each parent/runner and
runner/Application boundary through a one-shot inherited pipe; only the file
descriptor number is in the environment, the loader consumes and closes it,
and memory CLI/tool children never receive it. No second plaintext config file
is created. Real credential values, each value's standard and URL-safe Base64
forms (padded and unpadded), and the raw/base64 transport blob remain in-memory
privacy markers; findings persist only their kind and location, never the
value or a publicly verifiable derivative of it.
The complete transport blob is also covered by standard and URL-safe Base64
markers in padded and unpadded forms, so encoding the whole YAML payload cannot
evade scanning through Base64 alignment.

Raw self-learning state and Application runtime files are created only under a
mode-`0700` private temporary root outside the campaign artifact directory.
After all children finish, both the fixed oracle markers and the in-memory
provider markers are scanned. Only clean retry/reproduction snapshots are
copied into a private staging directory, scanned again, and atomically renamed
into the campaign. A finding deletes the temporary root and publishes no raw
snapshot; live state and runtime trees are never campaign artifacts.

Before execution every regular capsule file is moved to a private inode and
the tree is mode-frozen. Each Application/CLI child runs under an OS policy
that denies writes to the capsule, base Python runtime, linked-worktree Git
directory, and common Git directory, and globally denies hardlink and metadata
mutation. Git metadata pointers are parsed and isolation-scanned before any
Git or uv execution; runtime children are also denied outbound Unix-domain
socket connections so a local daemon cannot proxy a protected write. Normal
TCP/HTTPS provider access remains available. Start/end attestations bind the
source, dataset, safe model contract,
lockfile, Git/uv/Python runtimes, standard library, canonical path-free venv
bytes, `loom`, import origins, the raw commit tree, inherited network
environment, and inode isolation; every Application attempt carries the same
capsule ID. Historical reproduction is rejected unless the entire executable
validation surface, `src/` runtime, system config, and dependency manifests
exactly match the currently trusted checkout, so neither an older runner nor
an import-shadow file can run before attestation.

The descriptor is a reproducibility checksum, not a third-party signature. It
detects runtime drift and is independently checked against the recorded Git
commit, but someone who can rewrite every completed artifact can also recompute
its hashes. Artifact authenticity therefore remains the responsibility of the
CI/artifact store.

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
reviewer exceeds four provider requests. The end-of-campaign source, dataset,
and model fingerprints must equal their preflight values. At most one retry is
accepted, and only when the `loom run` subprocess itself reaches the campaign
timeout. Model-visible log text never authorizes a retry.

The outer launcher records `campaign_started_at` before capsule provisioning;
the committed runner records an independent timestamp window for every
Application attempt and `campaign_finished_at` after final evidence capture.
Re-audit requires every attempt to fit inside that persisted envelope. A
100-run release fails when the envelope exceeds eight hours even if all other
gates pass.

Recoverable required-tool protocol misses are counted in the audit and report,
including the number of affected runs. They are not retried as infrastructure
failures and do not by themselves fail memory semantics: an unrecovered miss
already fails the Application or reviewer completion gates.

The runtime telemetry contract is one stable line beginning with
`Memory review:` and containing at least `enabled`, `requested`, `resolved`,
`calls`, `input_tokens`, `output_tokens`, and `actions` as `key=value` fields.

Artifacts are written under
`.agentloom/validation/memory_feature_validation/<campaign_id>/` and include
the plan, environment, results, usage, privacy audit, failures, report, and
`reproduction_commands.json` for any failed runs.
Each entry contains one `uv run python ... --reproduce-campaign ... --run-id`
command. The original campaign's scanned pre-run snapshot is the only raw
state artifact: reproduction restores it into a new mode-`0700` temporary
root, executes the same production workflow, scans and deletes that root, and
publishes only sanitized result, audit, and log artifacts. It never publishes
a second state database, runtime tree, or retry snapshot.

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

The 30-minute release gate applies to the current candidate's complete
100,000-event campaign. Time spent re-auditing and independently replaying the
accepted baseline is excluded from that candidate budget, because it is a
separate 100,000-event workload. Metrics still retain and audit all three
values: `candidate_duration_seconds`, `baseline_validation_duration_seconds`,
and their sum in `duration_seconds`. The measured boundary includes the final
privacy scan of the newly written audit/metrics/report artifacts; the large
databases and prior artifacts were already scanned immediately before those
files were created. Only the last content-free metrics/report rewrite falls
after the measured boundary.

Re-audit an existing campaign without executing production writes:

```bash
uv run python applications/memory_feature_validation/scripts/run_offline_memory_campaign.py \
  --audit .agentloom/validation/memory_feature_validation/<campaign_id>
```

Offline artifacts include `cases.jsonl.gz`, the central `self_learning.db`,
`migration_v4_to_v5.db`, metrics, privacy audit, content-free failures, report,
and single-case reproduction commands. Raw generated secret and injection
markers are never written to case, failure, or report artifacts.
