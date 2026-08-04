# Goal Mode

Goal Mode lets one top-level Supervisor keep advancing a long-running objective.
An ordinary Agent returns after one `final_answer` or `max_steps` segment. A Goal
keeps the same runtime, conversation memory, Worker state, and checkpoint, then
issues bounded continuation prompts until the root Supervisor explicitly commits
completion or the token budget prevents another model request.

## Configuration

`goal` is legal only in a top-level Supervisor YAML:

```yaml
# Enabled with no Goal token ceiling
goal: true
```

```yaml
# Mapping form: enabled is required
goal:
  enabled: true
  token_budget: 120000
```

```yaml
goal: false
```

The value must be a boolean or a mapping containing only `enabled` and optional
`token_budget`. `enabled` must be boolean. `token_budget` must be a positive
integer—not a string, float, zero, or negative value—and is invalid with
`enabled: false`. Omitting it means unlimited; there is no hidden default. Any
`goal` key in Worker YAML, including `false`, is rejected.

The objective is derived deterministically from `description`, `workflow`, and
the runtime task; there is no separate `objective` field. Prefer one multiline
`workflow: |`. Goal Mode accepts `workflow: list[str]`, but merges and numbers the
items into one objective context and performs one initial run. With Goal disabled,
Supervisor lists retain their existing sequential multi-run behavior.

## Lifecycle and tools

One root task owns one Goal with these states:

- `active`: keep working; an ordinary final does not complete the Goal.
- `budget_limited`: cumulative usage reached the soft budget; no new request starts and the checkpoint remains resumable.
- `complete`: the root Supervisor called `update_goal(status="complete", evidence="...")`; this is terminal.

Only the root Supervisor sees `get_goal` and `update_goal`. Workers do not receive
the tools, and the handlers also enforce root run identity at execution time.
Completion evidence must be non-empty. Completed Todos, normal final answers, and
host-side guesses never complete a Goal.

Tool-calling runtimes may need one model response after `update_goal` to deliver
`final_answer`. That in-process settlement request is restricted to the same root
Agent, exposes only `final_answer` when a tool schema is present, is consumed once,
and is never persisted. Scheduled planning is skipped locally before settlement;
a pending smart-summary model call is replaced by deterministic local truncation;
a max-steps prose fallback may consume the same allowance. No allowance is created
if the completing response already exhausted the token budget.

The first segment receives the full objective. Later segments contain only the
stable `goal_id`, current state, used/remaining budget, and continuation
instructions, relying on existing conversation memory instead of reinjecting or
replaying the workflow. The root Agent can call `get_goal` to reread the canonical
objective when needed. `max_steps` ends a continuation segment, not the Goal. If
completion reaches `goal.json` before a process crashes while delivering
the final response, resume does not redo substantive work; persisted evidence is
authoritative.

## Token budget

`token_budget` counts provider-reported `prompt_tokens + completion_tokens` for
the entire root Agent tree, including Supervisor and Worker responses. Usage is
cumulative for the task and is never reset by failures, interruption, or resume.

The limit is soft. A request is fenced using previously committed usage; in-flight
responses and already-started parallel Workers may finish, so actual usage can
overshoot slightly. After a response commits the crossing, both the Goal and run
become `budget_limited`, the checkpoint is saved, and new requests stop.

Resume after increasing `token_budget` above `used_tokens`, or remove the field to
make the remaining Goal unlimited:

```bash
uv run loom run applications/<app>/workflows/<agent>.yaml --resume <task_id>
```

Budgets cannot be reduced. An unchanged exhausted budget remains
`budget_limited` on resume.

## Persistence and observability

With checkpointing enabled, canonical state is stored at:

```text
.agentloom/checkpoints/<application_id>/<task_id>/goal.json
```

It contains the stable `goal_id`, objective fingerprint, status, `goal_started`, budget,
prompt/completion/total usage, evidence, and timestamps. Corruption fails recovery
instead of silently clearing the state. Changing description, normalized workflow,
or runtime task rejects resume; disabling YAML Goal Mode for an active persisted
Goal also rejects resume.

Every run `manifest.json` contains a structured `goal` object. Terminal evidence
is also copied to `audit/goal.json`, so successful checkpoint cleanup does not erase
the audit trail. CLI text shows status and usage. `--output-format json` emits one
terminal event containing the same object; `--output-format jsonl` includes it on
the relevant lifecycle events, including `run.completed`, `run.budget_limited`,
`run.failed`, and `run.interrupted`. TUI run lists/details show the canonical state.
The Python API raises `ApplicationRunBudgetLimited` with the run receipt, Goal
snapshot, and resumable task id.

## Schedules

Schedules execute the same Agent YAML and therefore use identical continuation,
Worker accounting, checkpoint, and resume semantics. Unlimited scheduled Goals
are legal, but unattended runs should set `token_budget`; otherwise continuation
can run until explicit completion, user interruption, or a real failure. The
scheduler recognizes the isolated JSONL lifecycle event and records both the
execution and job `last_status` as `budget_limited`, with Goal usage diagnostics,
rather than misclassifying the outcome as an ordinary failure. Increase or remove
the budget, then resume the event's task id.

Goal Mode is not Todo, a second evaluator model, a generic workflow cursor, or a
Worker-owned Goal. Todo can organize work inside a Goal but cannot complete it.
