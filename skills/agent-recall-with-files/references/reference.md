# Runtime reference

## Canonical layout

```text
.agentloom/workspaces/agents/<application_id>/<agent_path>/
├── insights.md
└── tasks/<task_id>/
    ├── context.md
    ├── trace.md
    └── .write_tracker.json
```

`context.md` and `trace.md` belong to one logical `task_id`. A resume preserves
them. A new task receives new files. `insights.md` belongs to one Application and
runtime Agent path and survives across tasks.

## Template detection

A missing, empty, or unchanged template file counts as unwritten. The Hook emits
a short marker instead of injecting placeholder text. Task completion remains
default-allow even when a file is stale.

## Reminder thresholds

- Steps 1–3: no PostToolUse reminder.
- `trace.md`: gentle after 4 stale steps, urgent after 7.
- `context.md`: gentle after 6 stale steps, urgent after 10.
- Reminder cooldown: 3 steps.
- `insights.md`: no staleness reminder; write only durable knowledge.

PreToolUse and PostToolUse match the built-in file, Shell, and search tools named
in `HOOK.yaml`. They do not intercept arbitrary MCP, Worker, Skill, or custom
tools.

## Insight compaction

At TaskCreated, a file over 80 lines keeps its newest 30 non-empty entries under
`## Recent`. Older entries are replaced by one deterministic count grouped by
tag under `## Archive`. This is lossy bounding, not model-generated synthesis.

## Concurrency boundary

Different runtime Agent paths have separate `insights.md` files. Concurrent
tasks using the same Application and Agent path share one file. Serialize those
tasks: both manual edits and automatic TaskCreated compaction use unlocked
read/write cycles, so overlapping writers can lose entries. Safe concurrency
requires a future lock or atomic merge implementation.
