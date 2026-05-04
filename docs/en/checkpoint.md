# Checkpoint & Resume

## Overview

AgentLoom supports **Checkpoint & Resume** for multi-Agent tasks. When a long-running task is interrupted (user Ctrl-C, process crash, machine restart, etc.), it can continue from where it left off without starting over.

Core components:

| Component | Description |
|-----------|-------------|
| **CheckpointManager** | Persistence layer: atomic JSON writes, task tree management, checkpoint file organization |
| **CheckpointCoordinator** | Coordination layer: ContextVar singleton, manages checkpoint lifecycle |
| **CheckpointSerializer** | Serialization layer: smolagents MemoryStep serialization/deserialization |
| **ConversationRecovery** | Recovery pipeline: filters unresolved tool calls, orphaned thinking steps, empty steps |
| **FileHistoryManager** | File history: pre-edit backup, step snapshots, rewind to any step |
| **SupervisorHeartbeat** | Heartbeat monitor: Supervisor process liveness detection |
| **WorkerHeartbeat** | Worker heartbeat: per-call liveness detection |

## Storage Layout

Checkpoint data is written inside each run's timestamp log directory, co-located with the run log:

```
.logs/{agent_name}/
├── .task_index.json                        # Index: task_id → timestamp mapping (for fast --resume lookup)
├── 20260413_104447/                        # Per-run timestamp directory
│   ├── {agent_name}.log                    # Run log
│   └── checkpoints/{task_id}/              # Checkpoint data for this run
│       ├── task_tree.json                  # Task metadata (status, worker call records)
│       ├── checkpoint.json                 # Supervisor Agent memory snapshot
│       ├── heartbeat.json                  # Supervisor heartbeat (PID, timestamp)
│       ├── file-history/                   # File edit history backups
│       └── workers/{worker_name}/
│           ├── checkpoint.json             # Worker Agent memory snapshot
│           └── heartbeat.json              # Worker heartbeat
└── 20260413_104837/
    ├── {agent_name}.log
    └── checkpoints/{task_id}/
        └── ...
```

**Key design decisions**:
- Checkpoints always remain in the timestamp directory where they were first created; they are not migrated on resume
- `.task_index.json` records the `task_id → timestamp` mapping for O(1) lookup during `--resume`
- If the index is lost, the system automatically scans all timestamp directories as a degraded fallback

## Configuration

Configure in `config/system.yaml`:

```yaml
checkpoint:
  enabled: true              # Global switch
  cleanup_on_success: true   # Auto-delete checkpoint after successful task completion
  max_resume_age: 604800     # Max checkpoint retention (seconds), default 7 days
  heartbeat_interval: 5      # Heartbeat write interval (seconds)
```

## CLI Commands

### Running Tasks

```bash
# Normal run
loom run applications/<app>/workflows/<agent>.yaml

# Enable log file writing
loom run applications/<app>/workflows/<agent>.yaml --log-to-file

# Resume from checkpoint
loom run applications/<app>/workflows/<agent>.yaml --resume task_xxx
```

### Listing Resumable Tasks

```bash
loom list-tasks
loom list-tasks --detail
```

### Cleaning Old Checkpoints

```bash
loom clean-tasks          # Clean expired checkpoints
loom clean-tasks --all    # Clean all checkpoints
```

## Recovery Pipeline

When resuming with `--resume`, the system executes the following pipeline (ported from reference implementation):

```
Load persisted MemorySteps
  → filter_unresolved_tool_uses()    # Remove incomplete tool calls
  → filter_orphaned_thinking()       # Remove orphaned thinking steps
  → filter_empty_steps()             # Remove completely blank steps
  → detect_turn_interruption()       # Detect interruption type
  → Inject cleaned steps into Agent memory
  → Continue execution
```

### Interruption Detection

| Interruption Type | Description |
|-------------------|-------------|
| `none` | Task completed normally (has final_answer or completed tool calls) |
| `interrupted_turn` | Task was interrupted mid-execution (has tool_calls but no observations) |

## File History

The file history feature automatically creates backups before Agent edits files, supporting rewind to any step:

- **Auto-triggered**: Intercepted via `PRE_TOOL_USE` hook for `edit_file`, `write_file`, `create_file`, etc.
- **Three-phase lock safety**: Check → I/O → Commit, minimizing lock hold time
- **Snapshot management**: Up to 100 snapshots retained; oldest are automatically evicted
- **Null-backup**: Non-existent files get a null backup; on rewind the file is deleted

Storage structure:

```
.logs/{agent_name}/{timestamp}/checkpoints/{task_id}/file-history/
    {sha256_hash}@v1    # Pre-edit backup
    {sha256_hash}@v2    # Post-step snapshot
    snapshots.json       # Persisted index
```

## Worker Skip Mechanism

In multi-Agent tasks, already-completed Workers are automatically skipped on resume:

1. Worker computes SHA256 hash of input at startup
2. On completion, result and hash are stored in checkpoint
3. On resume, hash is checked — if matched, cached result is returned directly

## Crash Detection

Crash detection is implemented via heartbeat files:

- Supervisor and Worker write heartbeat files every 5 seconds (PID + timestamp)
- On resume, the following checks are performed:
  - Heartbeat file missing → crashed
  - Heartbeat status is stopped/exited → crashed
  - PID no longer exists → crashed
  - Timestamp older than 30 seconds → crashed

## Troubleshooting

### Resume fails: "No checkpoint found"

Confirm that a `task_id` directory exists under `.logs/{agent_name}/{timestamp}/checkpoints/`. Use `loom list-tasks` to see available tasks.

### Resume fails: "Checkpoint expired"

The checkpoint has exceeded the `max_resume_age` retention period (default 7 days).

### Resume fails: "checkpoint is disabled"

Check that `checkpoint.enabled` is set to `true` in `config/system.yaml`.
