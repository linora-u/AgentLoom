# Checkpoint, Resume, and Runtime Storage

## Overview

AgentLoom separates one execution attempt from the logical task being recovered:

- `run_id` identifies one execution attempt. Every normal run and every resume gets a new `run_id` and a new run directory.
- `task_id` identifies one logical task. Resume keeps the same `task_id` and continues the same checkpoint directory.

This boundary prevents log rotation or run cleanup from damaging resumable state. It also keeps concurrent Applications and tasks from sharing logger or artifact paths.

## Storage Layout

`runtime.root_dir` is the only framework runtime root. Its default is `.agentloom`:

```text
.agentloom/
├── runs/<application_id>/<run_id>/
│   ├── manifest.json
│   ├── logs/
│   │   ├── runtime.log
│   │   └── runtime.log.1 ... runtime.log.3
│   ├── audit/
│   │   ├── shell.jsonl
│   │   ├── shell.jsonl.1 ... shell.jsonl.2
│   │   ├── task_tree.json
│   │   ├── task_events.jsonl
│   │   └── goal.json            # Goal audit evidence, when present
│   └── artifacts/
│       ├── result.txt
│       ├── shell/
│       ├── background/
│       └── skills/
├── checkpoints/<application_id>/<task_id>/
│   ├── task_events.jsonl
│   ├── task_tree.json
│   ├── checkpoint.json
│   ├── heartbeat.json
│   ├── todos.json
│   ├── todos.lock
│   ├── goal.json                # canonical Goal state, when present
│   ├── goal.lock
│   ├── workers/<worker_name>/
│   │   ├── calls/<call_index>/checkpoint.json
│   │   └── heartbeat.json
│   ├── context_store/
│   └── file-history/
├── sessions/
├── learning/
├── self_learning.db
└── legacy/logs-v1-<timestamp>/
```

Important boundaries:

- `manifest.json` links the attempt back to its `task_id`; checkpoint run events and heartbeat records include the current `run_id`.
- `task_events.jsonl` is the durable task/Worker event source. `task_tree.json` is its inspectable projection.
- A successful result is copied to `artifacts/result.txt`. When checkpoint evidence exists, the run also receives `audit/task_tree.json` and `audit/task_events.jsonl`. The manifest records `result_artifact`, `task_tree_artifact`, `task_events_artifact`, and `task_events_complete` only when the corresponding evidence exists.
- These compact evidence files remain inspectable after `cleanup_on_success` removes the resumable checkpoint. Raw artifact retention can still clean bulk shell/background/skill artifacts.
- Checkpoint lookup uses the canonical `<application_id>/<task_id>` path. It does not depend on a log directory, `.task_index.json`, or a scan of historical runs.
- User deliverables remain under the Application's configured `output_dir`. Runtime cleanup never traverses Application output directories.
- Persistent Agent recall uses application-scoped `insights.md` under `.agentloom/workspaces/agents/<application_id>/<agent_path>/`. Current-task Todo state follows the checkpoint lifecycle in `<application_id>/<task_id>/todos.json` when checkpointing is enabled; otherwise it remains in run-scoped memory. It is neither a run artifact nor long-term project state.
- Goal Mode stores the objective fingerprint, `goal_started`, state, cumulative tokens, budget, and evidence in task-scoped `goal.json`. `active`, `budget_limited`, interrupted, failed, and crashed work retains the checkpoint. Only explicit Goal completion enters normal success cleanup, after copying Goal state into the run manifest and `audit/goal.json`.

### Terminal lifecycle ownership

One Application Run owner settles the terminal outcome. The Supervisor reports
its output, memory snapshot, error, and optional Goal projection; it does not
independently commit a second Application-level terminal state. The owner then
applies one ordered transaction:

```text
Supervisor report
  -> terminal checkpoint/task event
  -> result and audit evidence
  -> terminal manifest
  -> optional successful-checkpoint deletion
  -> resource cleanup
```

If evidence or manifest finalization fails before checkpoint deletion, the same
owner replaces provisional success with `failed` or `interrupted` and keeps the
checkpoint. If interruption occurs after deletion has started, the run reports
`resumable: false` and never recreates a skeletal checkpoint.

## Configuration

Configure runtime storage, bounded logs, and resume behavior in `config/system.yaml`:

```yaml
runtime:
  root_dir: ".agentloom"
  successful_run_retention_days: 7
  failed_run_retention_days: 30
  artifact_retention_days: 3
  cleanup_interval_hours: 24

logging:
  level: "INFO"
  console_enabled: true
  file_enabled: true
  max_file_bytes: 26214400  # 25 MiB per runtime.log segment
  backup_count: 3

checkpoint:
  enabled: true
  cleanup_on_success: true
  max_resume_age: 604800
  heartbeat_interval: 5
```

File logging is enabled by default and bounded. The runtime log keeps the active 25 MiB segment plus three backups. Shell audit uses an independent 10 MiB segment with two backups.

`runtime.root_dir` may be absolute or relative. A relative value resolves from the AgentLoom project root. For isolated validation, point this global key at a temporary absolute directory in the validation checkout.

## Running and Resuming

```bash
# New logical task and new execution attempt
loom run applications/<app>/workflows/<agent>.yaml

# Disable only the file runtime log for this attempt; checkpoint remains available
loom run applications/<app>/workflows/<agent>.yaml --no-file-log

# New execution attempt, same logical task/checkpoint
loom run applications/<app>/workflows/<agent>.yaml --resume task_xxx
```

Goal resume also verifies the description, normalized workflow, and original
runtime-task fingerprint. A `budget_limited` Goal resumes only after raising
`token_budget` above cumulative usage or removing the limit; usage never resets.
Disabling an active Goal, changing its objective, or reducing its budget rejects
resume. See [Goal Mode](goal_mode.md).

There is no `--log-to-file` flag. File logging follows `logging.file_enabled` by default, and `--no-file-log` is the per-run opt-out.

To list or remove checkpoint state:

```bash
loom list-tasks
loom list-tasks --detail
loom clean-tasks
loom clean-tasks --before 3
loom clean-tasks --all
```

When `cleanup_on_success: true`, a successfully completed task's entire checkpoint directory is removed. With cleanup disabled, a completed checkpoint remains visible for inspection but is terminal and cannot be resumed. Failed, interrupted, or crashed tasks remain recoverable until `max_resume_age`; their ContextStore and file history share the same task directory and lifecycle. `loom list-tasks` lists every retained checkpoint and its status.

## Recovery Behavior

Resume performs this recovery pipeline:

```text
load persisted MemorySteps
  -> remove unresolved tool uses and orphaned/empty steps
  -> restore task-scoped ContextStore and file history
  -> restore incomplete Worker call memory under the existing call_index
  -> reuse completed Worker results with matching input hashes
  -> continue execution under a new run_id
```

Each Worker call has its own `workers/<worker>/calls/<call_index>/checkpoint.json`, so concurrent calls do not overwrite one another. Resume reuses the incomplete call index; it does not create a duplicate call for the same interrupted work.

The Supervisor and Worker heartbeat payloads include the active `run_id`. Crash detection considers missing/stopped heartbeats, dead PIDs, and stale timestamps. Resume writes the next heartbeat to the same task checkpoint with the new attempt's `run_id`.

Malformed or half-written trailing JSONL lines are skipped during replay, so a crash-tail record does not make the earlier durable task events unreadable.

## Runtime Retention

Automatic runtime cleanup is throttled to at most once per `runtime.cleanup_interval_hours` (24 hours by default). It only traverses `.agentloom/runs`:

- completed runs: 7 days by default;
- failed/interrupted runs: 30 days by default;
- raw `artifacts/`: 3 days by default;
- running or unknown-status manifests: preserved;
- `.agentloom/legacy/`, checkpoints, workspaces, and Application outputs: never deleted by run cleanup.

Run the same policy explicitly with:

```bash
loom clean-runtime
```

Checkpoint expiry and deletion remain task-scoped: `max_resume_age` controls whether a task may resume, `cleanup_on_success` removes completed task state, and `loom clean-tasks` provides explicit checkpoint cleanup.

## One-Time Migration from `.logs`

Preview the migration first:

```bash
loom migrate-runtime --dry-run
```

The scan ignores every legacy `.task_index.json`. It reads real checkpoint directories and their task events/tree, excludes tests and expired tasks, and selects only tasks with resumable memory, ContextStore, or file-history progress.

Apply after reviewing the candidates:

```bash
loom migrate-runtime --apply
```

Apply copies each candidate through staging, verifies checksums and resumable progress, and atomically renames it into `.agentloom/checkpoints/<application_id>/<task_id>/`. After all candidates validate, the complete old `.logs` tree is atomically archived under `.agentloom/legacy/logs-v1-<timestamp>/`. New runtime code does not dual-read that archive.

After migration, run a real resume for each important task and verify both an old ContextRef retrieval and file-history state before treating the migration as accepted.

## Inspecting Real Run Evidence

Do not treat an exit code or final answer alone as proof. Read the attempt manifest, runtime log, and Shell audit:

```bash
manifest=$(find .agentloom/runs -name manifest.json -type f -print | sort | tail -1)
run_dir=$(dirname "$manifest")

sed -n '1,160p' "$manifest"
tail -n 100 "$run_dir/logs/runtime.log"
tail -n 100 "$run_dir/audit/shell.jsonl"
```

For a resumed task, confirm that the new manifest has a new `run_id` and the original `task_id`, then inspect:

```bash
find .agentloom/checkpoints/<application_id>/<task_id> -maxdepth 5 -type f -print
```

## Troubleshooting

### Resume fails: `No checkpoint found`

Run `loom list-tasks --detail` and confirm the task is under the same `application_id` as the workflow being resumed. Checkpoint lookup is Application-scoped.

### Resume fails: `Checkpoint expired`

The logical task's original `created_at` exceeds `checkpoint.max_resume_age`. Migration and resume do not rewrite `created_at` to revive expired work.

### `runtime.log` is absent

Check `logging.file_enabled` and whether the attempt used `--no-file-log`. A missing file log does not disable checkpoints or the Shell audit.

### `.logs` still exists

New runs do not write `.logs`. Use `loom migrate-runtime --dry-run`, review the result, then `--apply` to archive the legacy tree. Do not delete it before validating resumable tasks.
